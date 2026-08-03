# claude-agent-relay-guard

Claude Code のマルチエージェント運用で起きる **「再委譲」と「多段リレー」を、プロンプトと
hooks の多層防御で機械的に防ぐ** ための最小構成サンプルです。

自分のプロジェクトの `.claude/` に組み込めるよう、エージェント定義・hooks・テストを
まとめてあります。設計の背景と経緯はブログ記事にまとめています。

## 防ぎたい2つの問題

|            | 再委譲                                                                              | 多段リレー                                             |
| ---------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------ |
| 問題の軸   | **構造・深さ**（誰が呼ぶか）                                                        | **内容・流れ**（何を渡すか）                           |
| 起きること | サブエージェントが自分でさらにサブエージェントを呼び、ネストが深くなる              | メインセッションが前段の結果を咀嚼せず丸ごと次段へ流す |
| 主に防ぐ層 | **エージェント定義**（`tools` 制限）＋公式の `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` | **hook**（`agent-relay-guard`）                        |

似て見えますが原因が違うため、防御層を分けています。

## ファイル構成

```text
.claude/
├── agents/
│   ├── investigator.md       # 調査担当（haiku / 読み取り専用・tools制限で Agent 不可）
│   ├── implementer.md        # 実装担当（sonnet）
│   └── reviewer.md           # レビュー担当（opus / 読み取り専用・tools制限で Agent 不可）
├── hooks/
│   ├── agent-relay-guard.sh  # PreToolUse(Agent): リレーを検出して deny / ask
│   ├── agent-call-record.sh  # PostToolUse(Agent): 起動時の prompt と agent_id を記録
│   ├── agent-call-complete.sh # SubagentStop: 完了と最終報告テキストを記録
│   └── agent-calls-gc.sh     # UserPromptSubmit: 古いセッションの状態を掃除
├── tests/
│   └── test_agent_relay_guard.py  # hook 群の挙動テスト（unittest / 外部依存なし）
├── agent-calls/              # 実行時に生成される呼び出し状態（gitignore 対象）
└── settings.json             # 上記 hook の登録＋ SPAWN_DEPTH の設定
CLAUDE.md                     # エージェント委譲ルール（プロンプト側の第1防御）
```

## 多層防御のしくみ

1. **CLAUDE.md の運用原則** — 役割の対応表は固定パイプラインではないと明言し、
   「リレー禁止」「独立タスクは並列実行」「不要フェーズのスキップ」を明文化する。
2. **エージェント定義の制約** — 各エージェント本文に「他のサブエージェントを呼び出さない
   (Agent ツール使用禁止)」を明記。`investigator` / `reviewer` は `tools` を読み取り系に
   絞り、Agent ツール自体を持たせない（再委譲を構造的に不可能にする）。
3. **再委譲の深さは公式の仕組みに委ねる** — 入れ子の深さ制限は Claude Code 公式の環境変数
   `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`（`settings.json` の `env` で `1` = 入れ子を無効化）に
   任せる。この設定では、メインから呼ばれたサブエージェントは深さ制限に達しているため
   **Claude Code が `Agent` ツールを自動的に取り上げ**、再委譲（ネスト）は構造的に発生しない。
   項目2のエージェント定義の制約は、これが未対応の古いバージョン向けのフォールバックとして
   二重にかけている。なおサブエージェント内からの Agent 呼び出し（入力に `agent_id` が入る）は、
   後述の hook の判定対象外にしている。
   （`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` は Claude Code v2.1.217 以降で有効。それ以前の
   バージョンでは設定しても無視されるが、項目2のプロンプト制約が再委譲の抑止を担う。）
4. **`agent-relay-guard.sh`（PreToolUse hook）** — Agent 呼び出し前に、**完了済み**の
   呼び出しとだけ内容を突き合わせて判定する。判定を回数ではなく「完了 × 内容」にするのが
   要で、これにより1メッセージで同時発行した**並列呼び出しは構造的に常に許可**される
   （並列の兄弟はまだ完了しておらず、比較対象がゼロになるため）。
   - **deny**: 完了済みエージェントの**出力**を丸ごと貼り付けて再送している（出力行の
     一致割合が高い）→ 多段リレーの強い証拠として拒否
   - **ask**: 完了済みエージェントの **prompt** の使い回し（行集合の Jaccard 係数が高い）
     → グレーなので確認を求める。独立した別作業なら承認して続行できる

「完了」の記録には **PostToolUse ではなく `SubagentStop`** を使います。サブエージェントは
既定でバックグラウンド実行されるため、PostToolUse は*起動が返った時点*で発火してしまい、
並列2本目の PreToolUse より前に来てしまうためです。役割分担は次のとおりです。

- `agent-call-record.sh`（PostToolUse）… 起動時の prompt と `agent_id` の紐付けを記録
- `agent-call-complete.sh`（SubagentStop）… 完了と最終報告テキストを記録
- `agent-relay-guard.sh`（PreToolUse）… 上記が書いた「完了済み」の記録だけを読んで判定

状態は `<session>/<prompt_id>/<id>.{start,call,done}.json` の**1呼び出し1ファイル**で、
ロックなしでも並列安全です。**タスク境界はユーザーの1発言＝ `prompt_id`** が表現するため、
新しい発言では自動的に別ディレクトリになり履歴がリセットされます（削除用の hook は不要で、
`agent-calls-gc.sh` は古いセッションの掃除だけを担います）。

異常系はすべて許可側に倒す **fail-open** 設計で、開発を止めません。誤検知時は環境変数
`AGENT_RELAY_GUARD_DISABLE=1` で全チェックをバイパスできます。`prompt_id` が取得できない
環境向けの保険として、状態には TTL（既定30分 / `AGENT_RELAY_GUARD_TTL_SEC`）もあります。

## 使い方

### 1. 自分のプロジェクトに組み込む

`.claude/` 配下（`hooks/` `settings.json`、必要なら `agents/`）を自分のリポジトリにコピーし、
hook に実行権限を付けます。実行時に生成される状態ディレクトリ `.claude/agent-calls/` は
`.gitignore` に追加してください。

```bash
cp -r .claude/hooks .claude/settings.json <your-project>/.claude/
chmod +x <your-project>/.claude/hooks/*.sh
echo '.claude/agent-calls/' >> <your-project>/.gitignore
```

すでに `.claude/settings.json` がある場合は、`hooks` の各イベント
（`UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `SubagentStop`）と `env` の
`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` をマージしてください。

**hook はエージェント名に依存しません。** リレー判定は「完了済み呼び出しの内容の再送」だけを
見るため、`agents/` の3役（`investigator` / `implementer` / `reviewer`）はあくまで例です。
自分のプロジェクトの役割に置き換えても、hook 側の変更は不要です。`agents/` と `CLAUDE.md` の
委譲ルールは、そのまま使ってもよいですし、自分の運用に合わせて書き換えても構いません。

### 2. テストを実行する

外部依存はありません。`uv` でも素の `python3` でも動きます。

```bash
uv run python -m unittest discover -s .claude/tests -t .claude/tests -v
# または
python3 -m unittest discover -s .claude/tests -t .claude/tests -v
```

### 3. hook 単体の挙動を手元で確認する

リレーの判定は「**完了済み**の呼び出しとの突き合わせ」なので、deny を再現するには
「起動記録（PostToolUse）→ 完了記録（SubagentStop）→ 次の Agent 呼び出し（PreToolUse）」の
順に流します。まず前段を完了させます。

```bash
STATE=$(mktemp -d)
echo '{"session_id":"demo","prompt_id":"p1","tool_use_id":"t1","tool_input":{"subagent_type":"investigator","prompt":"リトライ処理を調査してください。"},"tool_response":{"agentId":"a1"}}' \
  | AGENT_RELAY_GUARD_STATE_DIR=$STATE .claude/hooks/agent-call-record.sh
echo '{"session_id":"demo","prompt_id":"p1","agent_id":"a1","agent_type":"investigator","last_assistant_message":"調査結果1: ... \n調査結果2: ..."}' \
  | AGENT_RELAY_GUARD_STATE_DIR=$STATE .claude/hooks/agent-call-complete.sh
```

その完了報告を丸ごと貼り付けて次段へ再送しようとすると deny が返ります
（`permissionDecisionReason` に理由と回避策が入る）。

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "完了済みのAgent呼び出し(役割: investigator)の出力を、このpromptが..."
  }
}
```

一方、同じ prompt_id で並列に同時発行した呼び出し（まだ完了記録が無い状態）は、
何本投げても許可されます。

## 環境変数

| 変数                               | 既定                         | 説明                                                       |
| ---------------------------------- | ---------------------------- | ---------------------------------------------------------- |
| `AGENT_RELAY_GUARD_DISABLE`        | （未設定）                   | `1` で全チェック・記録をスキップ（誤検知時のバイパス）     |
| `AGENT_RELAY_GUARD_ECHO_RATIO`     | `0.5`                        | 完了済み出力の再送とみなす一致割合のしきい値（deny 判定）  |
| `AGENT_RELAY_GUARD_ECHO_MIN_LINES` | `10`                         | 出力再送とみなす最小一致行数（短文での誤検知防止）         |
| `AGENT_RELAY_GUARD_SIMILARITY`     | `0.7`                        | prompt 使い回しとみなす Jaccard 係数のしきい値（ask 判定） |
| `AGENT_RELAY_GUARD_MIN_LINES`      | `3`                          | 類似判定の対象にする prompt の最小行数                     |
| `AGENT_RELAY_GUARD_TTL_SEC`        | `1800`                       | 状態の有効期間（秒）。`prompt_id` が無い環境向けの保険     |
| `AGENT_RELAY_GUARD_STATE_DIR`      | `<repo>/.claude/agent-calls` | 状態ファイルの保存先。記録側と判定側で一致させる契約       |

## 必要環境

- Claude Code
- Python 3.11 以上（hook の判定ロジックと unittest に使用。追加パッケージ不要）
