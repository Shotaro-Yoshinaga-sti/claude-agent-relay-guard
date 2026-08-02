#!/usr/bin/env bash
# PreToolUse hook (matcher: Agent): サブエージェントの多段リレー
# (前段の結果を丸ごと次のエージェントへ再委譲すること)を機械的に拒否する。
#
# 判定ロジック本体は Python で実装する(呼び出し履歴のJSONL永続化・行単位の
# 類似度計算はjq単体では現実的でないため。既存hookのjq-or-python3フォールバック
# 方針とは異なり、抽出〜判定〜記録までを1つのpython3プロセスに一本化することで
# ロジックの二重管理を避ける。bash-guard.shと同様、PYSCRIPTを変数化してstdinを
# hook入力のまま python3 -c に渡す)。
#
# バイパス: 環境変数 AGENT_RELAY_GUARD_DISABLE=1 で全チェック・記録をスキップする。
# 異常系(JSON不正・空入力・状態ファイル書き込み失敗等)は許可側に倒す(fail-open)。
set -u

if [ "${AGENT_RELAY_GUARD_DISABLE:-}" = "1" ]; then
  exit 0
fi

# 既定の状態ディレクトリは hook 自身の位置(<root>/.claude/hooks/)から導出し、
# agent-turn-reset.sh (リセット側) と一致させる。/workspace 決め打ちだと、
# チェックアウト先が異なる環境で makedirs が失敗してフェイルオープン(ガード無効化)する。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENT_RELAY_GUARD_STATE_DIR="${AGENT_RELAY_GUARD_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

PYSCRIPT=$(
  cat <<'PYEOF'
import hashlib
import json
import os
import re
import sys
import time


def allow() -> None:
    sys.exit(0)


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


BYPASS_HINT = "誤検知の場合は環境変数 AGENT_RELAY_GUARD_DISABLE=1 で一時的に無効化できます。"
ROLE_NAMES = {"investigator", "implementer", "reviewer"}
SIMILARITY_THRESHOLD = 0.7

# 履歴の有効期間(秒)。禁止したいのは「1つのタスク内での多段リレー」なので、
# 履歴は UserPromptSubmit hook (agent-turn-reset.sh) がタスク境界でリセットする。
# TTL はそのリセットが動かなかった場合のフォールバック(古い履歴を引きずらない)。
try:
    HISTORY_TTL_SEC = float(os.environ.get("AGENT_RELAY_GUARD_TTL_SEC", "1800"))
except ValueError:
    HISTORY_TTL_SEC = 1800.0


def normalize_lines(text: str) -> list[str]:
    return [s for s in (line.strip() for line in text.splitlines()) if s]


def line_similarity(a_lines: list[str], b_lines: list[str]) -> float:
    set_a, set_b = set(a_lines), set(b_lines)
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


try:
    raw = sys.stdin.read()
    if not raw.strip():
        allow()
    data = json.loads(raw)
    if not isinstance(data, dict):
        allow()
except Exception:
    allow()

tool_input = data.get("tool_input")
if not isinstance(tool_input, dict):
    tool_input = {}

session_id = tool_input.get("session_id", data.get("session_id", ""))
subagent_type = tool_input.get("subagent_type", data.get("subagent_type", ""))
prompt = tool_input.get("prompt", data.get("prompt", ""))

session_id = session_id if isinstance(session_id, str) else str(session_id or "")
subagent_type = subagent_type if isinstance(subagent_type, str) else str(subagent_type or "")
prompt = prompt if isinstance(prompt, str) else str(prompt or "")

if not session_id:
    # セッション単位で状態を分離できない場合はフェイルオープン。
    allow()

# 状態ディレクトリは呼び出し元の bash が REPO_ROOT から導出して export 済み。
# 万一未設定なら状態を分離できないためフェイルオープン。
state_dir = os.environ.get("AGENT_RELAY_GUARD_STATE_DIR", "")
if not state_dir:
    allow()
try:
    os.makedirs(state_dir, exist_ok=True)
except Exception:
    allow()

safe_session = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id) or "unknown"
state_file = os.path.join(state_dir, f"{safe_session}.jsonl")

norm_lines = normalize_lines(prompt)
norm_text = "\n".join(norm_lines)
prompt_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()

history: list[dict] = []
try:
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    history.append(json.loads(line))
                except Exception:
                    continue
except Exception:
    history = []

# TTL を過ぎた履歴は「別タスクのもの」とみなして判定対象から外す。
now = time.time()


def _is_fresh(entry: dict) -> bool:
    try:
        return (now - float(entry.get("time", 0))) <= HISTORY_TTL_SEC
    except (TypeError, ValueError):
        return False


history = [e for e in history if _is_fresh(e)]

reason = None

if subagent_type in ROLE_NAMES:
    for entry in history:
        if entry.get("subagent_type") == subagent_type:
            reason = (
                f"同一タスク内で役割 '{subagent_type}' への2回目以降のAgent呼び出しは"
                f"サブエージェントの多段リレー防止のため拒否します。"
                f"独立した別タスクなら、ユーザーの次の指示以降は再び呼び出せます。{BYPASS_HINT}"
            )
            break

if reason is None and history:
    last = history[-1]
    last_lines = last.get("norm_lines") or []
    similarity = line_similarity(norm_lines, last_lines)
    if similarity > SIMILARITY_THRESHOLD:
        reason = (
            "直前のAgent呼び出し(役割: "
            f"{last.get('subagent_type', '?')})とpromptの類似度が高く"
            f"({similarity:.2f} > {SIMILARITY_THRESHOLD})、前段の結果の丸ごと再送とみなし拒否します。"
            f"{BYPASS_HINT}"
        )

record = {
    "subagent_type": subagent_type,
    "prompt_hash": prompt_hash,
    "prompt_lines": len(norm_lines),
    "prompt_len": len(prompt),
    "norm_lines": norm_lines,
    "time": time.time(),
    "denied": reason is not None,
}
try:
    with open(state_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
except Exception:
    pass

if reason:
    deny(reason)
allow()
PYEOF
)

exec python3 -c "$PYSCRIPT"
