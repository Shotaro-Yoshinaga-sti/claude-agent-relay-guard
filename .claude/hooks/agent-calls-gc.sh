#!/usr/bin/env bash
# UserPromptSubmit hook: agent-relay-guard.sh の状態ディレクトリを掃除する。
#
# タスク境界（「ユーザーの新しい発言 = 新しいタスク」）は、状態を
# <session_id>/<prompt_id>/ に分離することで prompt_id が表現している。
# 新しい発言では自動的に別ディレクトリになるため、境界での明示的な履歴削除は不要。
# したがってこの hook の役割は、古いセッションの状態を掃除する GC だけ。
#
# ノンブロッキング: 何が起きても常に exit 0。
set -u

# リポジトリルートは hook 自身の位置(<root>/.claude/hooks/)から導出する。
# 既定の状態ディレクトリは agent-relay-guard.sh 等と一致させる契約。
# /workspace 決め打ちだと、チェックアウト先が異なる環境ですれ違う。
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE_DIR="${AGENT_RELAY_GUARD_STATE_DIR:-$REPO_ROOT/.claude/agent-calls}"

# stdin は読み捨てる(このhookは入力を必要としない)。
cat >/dev/null 2>&1 || true

[ -d "$STATE_DIR" ] || exit 0

# 古い(7日以上前の)セッション状態を掃除する。
# 旧実装が作っていた <session>.jsonl も同じ基準で片付ける。
find "$STATE_DIR" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null || true
find "$STATE_DIR" -mindepth 1 -maxdepth 1 -name '*.jsonl' -mtime +7 -delete 2>/dev/null || true

exit 0
