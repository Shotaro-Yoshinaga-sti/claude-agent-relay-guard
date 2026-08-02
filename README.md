# claude-agent-relay-guard

Claude Code のマルチエージェント運用で起きる **「再委譲」と「多段リレー」を、プロンプトと
hooks の多層防御で機械的に防ぐ** ための最小構成サンプルです。

自分のプロジェクトの `.claude/` に組み込めるよう、エージェント定義・hooks・テストを
まとめてあります。設計の背景と経緯はブログ記事にまとめています。

## 防ぎたい2つの問題

| | 再委譲 | 多段リレー |
|---|---|---|
| 問題の軸 | **構造・深さ**（誰が呼ぶか） | **内容・流れ**（何を渡すか） |
| 起きること | サブエージェントが自分でさらにサブエージェントを呼び、ネストが深くなる | メインセッションが前段の結果を咀嚼せず丸ごと次段へ流す |
| 主に防ぐ層 | **エージェント定義**（`tools` 制限＋プロンプトの Agent 禁止） | **hook**（`agent-relay-guard`） |

似て見えますが原因が違うため、防御層を分けています。

## ファイル構成

```text
.claude/
├── agents/
│   ├── investigator.md      # 調査担当（haiku / 読み取り専用・tools制限で Agent 不可）
│   ├── implementer.md       # 実装担当（sonnet / Agent 保持。再委譲は hook がバックストップ）
│   └── reviewer.md          # レビュー担当（opus / 読み取り専用・tools制限で Agent 不可）
├── hooks/
│   ├── agent-relay-guard.sh # PreToolUse(Agent): リレー/再委譲ネストを検出して deny
│   └── agent-turn-reset.sh  # UserPromptSubmit: タスク境界で呼び出し履歴をリセット
├── tests/
│   └── test_agent_relay_guard.py  # hook の挙動テスト（unittest / 外部依存なし）
├── agent-calls/             # 実行時に生成される呼び出し履歴（gitignore 対象）
└── settings.json            # 上記2 hook の登録
CLAUDE.md                    # エージェント委譲ルール（プロンプト側の第1防御）
```

## 多層防御のしくみ

1. **CLAUDE.md の運用原則** — 役割の対応表は固定パイプラインではないと明言し、
   「リレー禁止」「独立タスクは並列実行」「不要フェーズのスキップ」を明文化する。
2. **エージェント定義の制約** — 各エージェント本文に「他のサブエージェントを呼び出さない
   (Agent ツール使用禁止)」を明記。`investigator` / `reviewer` は `tools` を読み取り系に
   絞り、Agent ツール自体を持たせない（再委譲を構造的に不可能にする）。
3. **`agent-relay-guard.sh`（PreToolUse hook）** — Agent 呼び出し前に、タスク内の呼び出し
   履歴と突き合わせて次の2判定で拒否する:
   - **判定1**: `investigator` / `implementer` / `reviewer` への同一役割2回目以降を拒否
     （`implementer` の再委譲ネストのバックストップも兼ねる）
   - **判定2**: 直前の呼び出しと prompt の**行集合の Jaccard 係数 > 0.7** なら「丸ごと再送」
     とみなして拒否（`general-purpose` 等すべての subagent_type に有効）
4. **`agent-turn-reset.sh`（UserPromptSubmit hook）** — 「ユーザーの新しい発言＝新しいタスク」
   とみなし、そのセッションの呼び出し履歴を削除。判定単位を**セッションではなくタスク**に
   揃える。

異常系はすべて許可側に倒す **fail-open** 設計で、開発を止めません。誤検知時は環境変数
`AGENT_RELAY_GUARD_DISABLE=1` で全チェックをバイパスできます。リセット漏れに備えて履歴には
TTL（既定30分 / `AGENT_RELAY_GUARD_TTL_SEC`）もあります。

## 使い方

### 1. 自分のプロジェクトに組み込む

`.claude/` 配下（`agents/` `hooks/` `settings.json`）を自分のリポジトリにコピーし、hook に
実行権限を付けます。

```bash
cp -r .claude/agents .claude/hooks .claude/settings.json <your-project>/.claude/
chmod +x <your-project>/.claude/hooks/*.sh
```

すでに `.claude/settings.json` がある場合は、`hooks.UserPromptSubmit` と
`hooks.PreToolUse`（matcher: `Agent`）の項目をマージしてください。エージェント名
（`investigator` / `implementer` / `reviewer`）を変える場合は、`agent-relay-guard.sh` 内の
`ROLE_NAMES` も合わせて更新します。

### 2. テストを実行する

外部依存はありません。`uv` でも素の `python3` でも動きます。

```bash
uv run python -m unittest discover -s .claude/tests -t .claude/tests -v
# または
python3 -m unittest discover -s .claude/tests -t .claude/tests -v
```

### 3. hook 単体の挙動を手元で確認する

JSON を stdin に流すだけで確認できます。同じセッション ID で `implementer` を2回呼ぶと、
2回目が deny されます。

```bash
echo '{"session_id":"demo","tool_name":"Agent","tool_input":{"subagent_type":"implementer","prompt":"課題Aの実装をお願いします。"}}' \
  | .claude/hooks/agent-relay-guard.sh
```

deny 時は次のような JSON が返ります（`permissionDecisionReason` に理由と回避策が入る）。

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "同一タスク内で役割 'implementer' への2回目以降のAgent呼び出しはサブエージェントの多段リレー防止のため拒否します。..."
  }
}
```

## 環境変数

| 変数 | 既定 | 説明 |
|---|---|---|
| `AGENT_RELAY_GUARD_DISABLE` | （未設定） | `1` で全チェック・記録をスキップ（誤検知時のバイパス） |
| `AGENT_RELAY_GUARD_TTL_SEC` | `1800` | 呼び出し履歴の有効期間（秒）。リセット漏れのフォールバック |
| `AGENT_RELAY_GUARD_STATE_DIR` | `<repo>/.claude/agent-calls` | 履歴 JSONL の保存先。guard と reset で一致させる契約 |

## 必要環境

- Claude Code
- Python 3.11 以上（hook の判定ロジックと unittest に使用。追加パッケージ不要）
