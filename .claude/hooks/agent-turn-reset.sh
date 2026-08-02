#!/usr/bin/env bash
# UserPromptSubmit hook: ユーザーの新しい発言 = 新しいタスクの開始とみなし、
# agent-relay-guard.sh が使う「そのタスク内のAgent呼び出し履歴」をリセットする。
#
# 多段リレーの禁止は CLAUDE.md 上「1つのタスクで各役割のエージェントを使うのは
# 最大1回ずつ」という *タスク単位* のルールであり、セッション単位ではない。
# セッションが長く続くほど誤検知するのを防ぐため、ここで履歴を切る。
#
# ノンブロッキング: 何が起きても常に exit 0(判定側は履歴なし=許可)。
set -u

# リポジトリルートは hook 自身の位置(<root>/.claude/hooks/)から導出する。
# 既定の状態ディレクトリは agent-relay-guard.sh (読む側/書く側) と一致させる契約。
# /workspace 決め打ちだと、チェックアウト先が異なる環境で書く側と読む側がすれ違い、
# リセットが効かず誤検知(正当なAgent呼び出しの拒否)を招く。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${AGENT_RELAY_GUARD_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

input=$(cat 2>/dev/null || true)

session_id=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    sid = data.get("session_id", "")
    print(sid if isinstance(sid, str) else "")
except Exception:
    print("")
' 2>/dev/null)

[ -d "$STATE_DIR" ] || exit 0

# 古い(7日以上前の)セッション状態ファイルを掃除する。
find "$STATE_DIR" -maxdepth 1 -name '*.jsonl' -mtime +7 -delete 2>/dev/null || true

[ -n "$session_id" ] || exit 0

safe_session=$(printf '%s' "$session_id" | tr -c 'A-Za-z0-9_.-' '_')
rm -f "$STATE_DIR/$safe_session.jsonl" 2>/dev/null || true

exit 0
