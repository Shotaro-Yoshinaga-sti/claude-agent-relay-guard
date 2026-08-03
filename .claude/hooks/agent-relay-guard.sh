#!/usr/bin/env bash
# PreToolUse hook (matcher: Agent): サブエージェントの多段リレー
# (前段の結果を丸ごと次のエージェントへ再委譲すること)を機械的に検査する。
#
# 判定の中核: 「本物のリレーは前段が *完了* していることを必要とする」。
# メインセッションは前段の結果を受け取って初めて次段へ転送できる。したがって
# 比較対象を「完了済みのAgent呼び出し」だけに限れば、1メッセージで同時発行した
# 並列呼び出しは構造的に必ず許可される(まだ誰も完了していないため)。
# 役割ごとの回数制限は行わない。禁止しているのは内容の使い回しであって回数ではない。
#
# 「完了」の判定には SubagentStop を使う。PostToolUse ではない点に注意:
# サブエージェントは既定でバックグラウンド実行されるため、PostToolUse は
# *起動が返った時点*（サブエージェントの完了を待たず、非同期の起動が返った時点）で発火し、
# 並列2本目の PreToolUse より前に来てしまう(実測: PostToolUse は起動の0.4秒後、
# SubagentStop は約6秒後、並列呼び出しの間隔は0.8秒)。
#
# 状態は agent-call-record.sh (PostToolUse) と agent-call-complete.sh (SubagentStop)
# が書き、このhookだけが読む。1呼び出し1ファイルなのでロックなしで並列安全。
#
# 判定ロジック本体は Python で実装する(状態ファイルの読み込み・行単位の類似度計算は
# jq単体では現実的でないため。bash-guard.shと同様、PYSCRIPTを変数化してstdinを
# hook入力のまま python3 -c に渡す)。
#
# バイパス: 環境変数 AGENT_RELAY_GUARD_DISABLE=1 で全チェック・記録をスキップする。
# 異常系(JSON不正・空入力・状態ファイル書き込み失敗等)は許可側に倒す(fail-open)。
set -u

if [ "${AGENT_RELAY_GUARD_DISABLE:-}" = "1" ]; then
  exit 0
fi

# 既定の状態ディレクトリは hook 自身の位置(<root>/.claude/hooks/)から導出し、
# 記録側 (agent-call-record.sh / agent-call-complete.sh) と一致させる。
# /workspace 決め打ちだと、チェックアウト先が異なる環境で読む側と書く側がすれ違う。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENT_RELAY_GUARD_STATE_DIR="${AGENT_RELAY_GUARD_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

PYSCRIPT=$(
  cat <<'PYEOF'
import glob
import hashlib
import json
import os
import re
import sys
import time


def allow() -> None:
    sys.exit(0)


def decide(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


BYPASS_HINT = "誤検知の場合は環境変数 AGENT_RELAY_GUARD_DISABLE=1 で一時的に無効化できます。"


def _num_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# 前段の「出力」を丸ごと貼り付けて再送しているか(強い証拠 → deny)。
OUTPUT_ECHO_RATIO = _num_env("AGENT_RELAY_GUARD_ECHO_RATIO", 0.5)
OUTPUT_ECHO_MIN_LINES = int(_num_env("AGENT_RELAY_GUARD_ECHO_MIN_LINES", 10))
# 前段の「prompt」の使い回しか(グレー → ask)。
SIMILARITY_THRESHOLD = _num_env("AGENT_RELAY_GUARD_SIMILARITY", 0.7)
# 短いプロンプト/短い定型行だけで偶然重なる誤検知を防ぐための下限。
MIN_PROMPT_LINES = int(_num_env("AGENT_RELAY_GUARD_MIN_LINES", 3))
MIN_LINE_CHARS = 12

# 履歴の有効期間(秒)。タスク境界は prompt_id によるディレクトリ分離で表現されるため
# 通常は効かない。prompt_id が取得できなかった場合のフォールバック。
HISTORY_TTL_SEC = _num_env("AGENT_RELAY_GUARD_TTL_SEC", 1800)


def normalize_lines(text: str) -> list[str]:
    return [s for s in (line.strip() for line in text.splitlines()) if s]


def line_similarity(a_lines: list[str], b_lines: list[str]) -> float:
    set_a, set_b = set(a_lines), set(b_lines)
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def significant(lines: list[str]) -> set[str]:
    """短い定型行(箇条書き記号・コードフェンス等)を除いた比較用の行集合。"""
    return {s for s in lines if len(s) >= MIN_LINE_CHARS}


def safe_name(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def load_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


try:
    raw = sys.stdin.read()
    if not raw.strip():
        allow()
    data = json.loads(raw)
    if not isinstance(data, dict):
        allow()
except Exception:
    allow()

# サブエージェント内からの Agent 呼び出し(agent_id が入る)は対象外。
# 委譲ルールはメインセッションの規律であり、入れ子の深さ制限は
# CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH に任せる。
if data.get("agent_id"):
    allow()

tool_input = data.get("tool_input")
if not isinstance(tool_input, dict):
    tool_input = {}


def field(key: str) -> str:
    value = tool_input.get(key, data.get(key, ""))
    return value if isinstance(value, str) else str(value or "")


session_id = field("session_id")
prompt_id = field("prompt_id")
tool_use_id = field("tool_use_id")
subagent_type = field("subagent_type")
prompt = field("prompt")

if not session_id:
    # セッション単位で状態を分離できない場合はフェイルオープン。
    allow()

# 状態ディレクトリは呼び出し元の bash が REPO_ROOT から導出して export 済み。
# 万一未設定なら状態を分離できないためフェイルオープン。
state_root = os.environ.get("AGENT_RELAY_GUARD_STATE_DIR", "")
if not state_root:
    allow()

# タスク境界 = ユーザーの1発言 = prompt_id。次の発言では別ディレクトリになるので
# 履歴は自動的にリセットされ、削除hookの発火に依存しない。
state_dir = os.path.join(
    state_root,
    safe_name(session_id, "unknown-session"),
    safe_name(prompt_id, "no-prompt-id"),
)
try:
    os.makedirs(state_dir, exist_ok=True)
except Exception:
    allow()

norm_lines = normalize_lines(prompt)
norm_text = "\n".join(norm_lines)
prompt_hash = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
now = time.time()


def is_fresh(entry: dict) -> bool:
    try:
        return (now - float(entry.get("time", 0))) <= HISTORY_TTL_SEC
    except (TypeError, ValueError):
        return False


# --- 比較対象は「完了済み」だけ ---
# .done.json は SubagentStop (= 本当の完了) でのみ書かれる。並列兄弟はまだ完了して
# いないので存在せず、比較対象がゼロになる = 並列は構造的に常に許可される。
dones: dict[str, dict] = {}
calls: dict[str, dict] = {}
try:
    for path in glob.glob(os.path.join(state_dir, "*.done.json")):
        rec = load_json(path)
        if rec and is_fresh(rec):
            dones[str(rec.get("agent_id", os.path.basename(path)))] = rec
    for path in glob.glob(os.path.join(state_dir, "*.call.json")):
        rec = load_json(path)
        if rec and is_fresh(rec):
            calls[str(rec.get("agent_id", os.path.basename(path)))] = rec
except Exception:
    dones, calls = {}, {}

decision = "allow"
reason = None
sig_new = significant(norm_lines)

# (1) 強い証拠: 完了済みエージェントの「出力」を丸ごと貼り付けて再送している。
#     .call.json との紐付けが無くても判定できるので、こちらを先に見る。
for agent_id, done in sorted(dones.items(), key=lambda kv: kv[1].get("time", 0), reverse=True):
    out_lines = done.get("output_lines")
    if not isinstance(out_lines, list) or not sig_new:
        continue
    hit = sig_new & significant([s for s in out_lines if isinstance(s, str)])
    ratio = len(hit) / len(sig_new)
    if len(hit) >= OUTPUT_ECHO_MIN_LINES and ratio >= OUTPUT_ECHO_RATIO:
        role = (calls.get(agent_id) or {}).get("subagent_type", done.get("agent_type", "?"))
        decision = "deny"
        reason = (
            f"完了済みのAgent呼び出し(役割: {role})の出力を、このpromptが"
            f"{ratio:.0%}({len(hit)}行)そのまま含んでいます。前段の結果を丸ごと次段へ"
            f"再送する多段リレーは禁止です。メインセッションで結果を読んで判断し、"
            f"次段には必要な指示だけを新しく書いて渡してください。{BYPASS_HINT}"
        )
        break

# (2) グレー: 完了済みエージェントの「prompt」の使い回し。回数ではなく内容を見る。
if decision == "allow" and len(norm_lines) >= MIN_PROMPT_LINES:
    completed = [
        (agent_id, calls[agent_id]) for agent_id in dones if agent_id in calls
    ]
    for agent_id, call in sorted(completed, key=lambda kv: kv[1].get("time", 0), reverse=True):
        past_lines = call.get("norm_lines")
        if not isinstance(past_lines, list):
            continue
        similarity = line_similarity([s for s in past_lines if isinstance(s, str)], norm_lines)
        if similarity > SIMILARITY_THRESHOLD:
            decision = "ask"
            reason = (
                f"完了済みのAgent呼び出し(役割: {call.get('subagent_type', '?')})のpromptと"
                f"類似度が高く({similarity:.2f} > {SIMILARITY_THRESHOLD})、前段の課題の"
                f"使い回しの可能性があります。独立した別の作業であれば承認して続行してください。"
                f"{BYPASS_HINT}"
            )
            break

# 監査用の記録。判定には .call.json / .done.json のみを使うため、この start 記録が
# 後続の判定に影響することはない(拒否された呼び出しが次を巻き込むカスケードは起きない)。
record = {
    "tool_use_id": tool_use_id,
    "subagent_type": subagent_type,
    "prompt_hash": prompt_hash,
    "prompt_lines": len(norm_lines),
    "time": now,
    "decision": decision,
}
try:
    name = safe_name(tool_use_id, f"anon-{time.time_ns()}-{os.getpid()}")
    with open(os.path.join(state_dir, f"{name}.start.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
except Exception:
    pass

if reason:
    decide(decision, reason)
allow()
PYEOF
)

exec python3 -c "$PYSCRIPT"
