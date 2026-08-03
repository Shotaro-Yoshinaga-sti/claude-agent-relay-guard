"""agent-relay-guard.sh (PreToolUse hook) と、その状態を書く記録側hookのテスト。

subprocess で hook スクリプトに JSON を流し込み、許可/確認(ask)/拒否(deny) の
分岐を検証する。状態ディレクトリは環境変数 AGENT_RELAY_GUARD_STATE_DIR で一時
ディレクトリに差し替える。

このhook群が守っている性質(いずれも過去に壊れたことのある回帰):

1. 1メッセージで同時発行した並列のAgent呼び出しは常に許可される。
   判定対象を「完了済み(= SubagentStop が来た)呼び出し」だけに限ることで
   構造的に保証している。PostToolUse は *完了ではない* (サブエージェントは
   既定でバックグラウンド実行され、起動が返った時点で発火する)。
2. 同一役割を1タスク内で複数回呼べる。禁止しているのは内容の使い回しであって
   回数ではない。
3. 拒否された呼び出しが後続を巻き込むカスケードが起きない。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

HOOKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
GUARD_HOOK = os.path.join(HOOKS_DIR, "agent-relay-guard.sh")
RECORD_HOOK = os.path.join(HOOKS_DIR, "agent-call-record.sh")
COMPLETE_HOOK = os.path.join(HOOKS_DIR, "agent-call-complete.sh")
GC_HOOK = os.path.join(HOOKS_DIR, "agent-calls-gc.sh")

ALL_HOOKS = (GUARD_HOOK, RECORD_HOOK, COMPLETE_HOOK, GC_HOOK)


def _env(state_dir: str, *, disable: bool = False, **overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["AGENT_RELAY_GUARD_STATE_DIR"] = state_dir
    if disable:
        env["AGENT_RELAY_GUARD_DISABLE"] = "1"
    else:
        env.pop("AGENT_RELAY_GUARD_DISABLE", None)
    env.update(overrides)
    return env


def _run(
    hook: str,
    payload: object,
    *,
    state_dir: str,
    disable: bool = False,
    **overrides: str,
) -> subprocess.CompletedProcess:
    stdin = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return subprocess.run(
        [hook],
        input=stdin,
        capture_output=True,
        text=True,
        env=_env(state_dir, disable=disable, **overrides),
        timeout=20,
    )


def make_payload(
    session_id: str,
    subagent_type: str,
    prompt: str,
    *,
    prompt_id: str = "prompt-1",
    tool_use_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    payload: dict = {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "tool_use_id": tool_use_id or f"toolu_{uuid.uuid4().hex[:16]}",
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
        payload["agent_type"] = subagent_type
    return payload


def run_guard(
    payload: dict,
    *,
    state_dir: str,
    disable: bool = False,
    **overrides: str,
) -> subprocess.CompletedProcess:
    return _run(GUARD_HOOK, payload, state_dir=state_dir, disable=disable, **overrides)


def run_record(
    session_id: str,
    agent_id: str,
    subagent_type: str,
    prompt: str,
    *,
    state_dir: str,
    prompt_id: str = "prompt-1",
    tool_use_id: str = "toolu_x",
) -> subprocess.CompletedProcess:
    """PostToolUse (matcher: Agent) — 起動が返った時点の記録。完了ではない。"""
    payload = {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "tool_use_id": tool_use_id,
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
        "tool_response": {"isAsync": True, "agentId": agent_id},
    }
    return _run(RECORD_HOOK, payload, state_dir=state_dir)


def run_complete(
    session_id: str,
    agent_id: str,
    output: str,
    *,
    state_dir: str,
    prompt_id: str = "prompt-1",
    agent_type: str = "implementer",
) -> subprocess.CompletedProcess:
    """SubagentStop — 本当の完了。"""
    payload = {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "hook_event_name": "SubagentStop",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "last_assistant_message": output,
    }
    return _run(COMPLETE_HOOK, payload, state_dir=state_dir)


def decision_of(result: subprocess.CompletedProcess) -> str:
    """hookの決定を返す。無出力(=通常の許可フローに委ねる)は "allow"。"""
    out = result.stdout.strip()
    if not out:
        return "allow"
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "allow"
    return data.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


class _StateDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = self._tmp.name
        self.session = str(uuid.uuid4())

    def assertAllowed(self, result: subprocess.CompletedProcess) -> None:
        """許可されたこと(= 何も出力せず正常終了)を厳密に検証する。

        decision_of が "allow" なだけでは、hookがクラッシュして stdout が空の
        場合も通ってしまう。stdout が空であることと終了コード0を明示的に確認する。
        """
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "", f"想定外の出力: {result.stdout}")

    def task_dir(self, prompt_id: str = "prompt-1") -> str:
        return os.path.join(self.state_dir, self.session, prompt_id)

    def complete_call(
        self,
        agent_id: str,
        subagent_type: str,
        prompt: str,
        output: str,
        *,
        prompt_id: str = "prompt-1",
    ) -> None:
        """1本のAgent呼び出しを「起動 → 完了」まで進めた状態を作る。"""
        run_record(
            self.session,
            agent_id,
            subagent_type,
            prompt,
            state_dir=self.state_dir,
            prompt_id=prompt_id,
        )
        run_complete(
            self.session,
            agent_id,
            output,
            state_dir=self.state_dir,
            prompt_id=prompt_id,
            agent_type=subagent_type,
        )


class ParallelDispatchTests(_StateDirTestCase):
    """本件の中核: 1メッセージ内の並列呼び出しが拒否されないこと。"""

    def test_parallel_batch_of_same_role_is_all_allowed(self) -> None:
        """同一役割 × 5本を、共通の前置きを持つ高類似promptで同時発行しても全部通る。

        完了記録 (.done.json) が1件も無いので比較対象がゼロになる、という
        構造的な保証を検証している。
        """
        preamble = [
            "あなたはこのプロジェクトのリポジトリで作業しています。",
            "リポジトリルートは /workspace です。",
            "テストは uv run python -m unittest で実行してください。",
            "lint は bash tools/ci-checks.sh --lint です。",
        ]
        for index in range(5):
            prompt = "\n".join([*preamble, f"対象ファイル{index} を修正してください。"])
            result = run_guard(
                make_payload(self.session, "implementer", prompt),
                state_dir=self.state_dir,
            )
            self.assertAllowed(result)

    def test_post_tool_use_record_alone_does_not_block_siblings(self) -> None:
        """PostToolUse だけ来た(= 起動しただけ)状態は「完了」とみなさない。

        サブエージェントは既定でバックグラウンド実行され、PostToolUse は並列
        2本目の PreToolUse より前に発火する。ここを完了とみなすと並列が壊れる。
        """
        prompt = "\n".join(f"共通の作業指示の行 {i} です。ここは十分に長い行です。" for i in range(8))
        run_record(self.session, "agent-A", "implementer", prompt, state_dir=self.state_dir)

        result = run_guard(make_payload(self.session, "implementer", prompt), state_dir=self.state_dir)
        self.assertAllowed(result)

    def test_concurrent_calls_do_not_race(self) -> None:
        """同時起動しても取りこぼし・誤判定が起きない(ロック無しでの並列安全性)。"""
        payloads = [make_payload(self.session, "implementer", f"独立した作業 {i} の実装依頼です。") for i in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda p: run_guard(p, state_dir=self.state_dir), payloads))

        for result in results:
            self.assertAllowed(result)
        starts = [n for n in os.listdir(self.task_dir()) if n.endswith(".start.json")]
        self.assertEqual(len(starts), 8, starts)


class RelayDetectionTests(_StateDirTestCase):
    """完了済みの呼び出しに対してのみリレー判定が働くこと。"""

    def _report_lines(self) -> list[str]:
        return [f"調査結果 {i}: モジュール module_{i}.py に該当の処理が見つかりました。" for i in range(20)]

    def test_relay_after_completion_is_denied(self) -> None:
        report = "\n".join(self._report_lines())
        self.complete_call("agent-A", "investigator", "リトライ処理を調査してください。", report)

        # 前段の報告を丸ごと貼り付けて次段へ投げる = 禁止したい多段リレー。
        relay_prompt = "以下の調査結果を踏まえて実装してください。\n" + report
        result = run_guard(
            make_payload(self.session, "implementer", relay_prompt),
            state_dir=self.state_dir,
        )
        self.assertEqual(decision_of(result), "deny", result.stdout)

    def test_prompt_similarity_after_completion_asks(self) -> None:
        """promptの使い回しは deny ではなく ask(誤検知なら承認して続行できる)。"""
        prompt1 = "\n".join(
            [
                "調査対象: src/payments/retry.py の実装",
                "手順1: リトライロジックを確認してください",
                "手順2: エラーハンドリングを確認してください",
                "手順3: テストの有無を確認してください",
            ]
        )
        self.complete_call("agent-A", "investigator", prompt1, "調査が完了しました。")

        prompt2 = prompt1 + "\n手順4: 以上を踏まえて実装してください"
        result = run_guard(
            make_payload(self.session, "general-purpose", prompt2),
            state_dir=self.state_dir,
        )
        self.assertEqual(decision_of(result), "ask", result.stdout)

    def test_second_call_to_same_role_with_different_prompt_is_allowed(self) -> None:
        """前段が完了していても、内容が違えば同じ役割をもう一度呼べる。

        旧実装は「1タスクで各役割1回まで」で無条件に拒否していた(本件の要件)。
        """
        self.complete_call(
            "agent-A",
            "implementer",
            "課題Aの実装をお願いします。ファイルXを編集してください。",
            "課題Aの実装が完了しました。",
        )
        result = run_guard(
            make_payload(self.session, "implementer", "課題Cの別の実装をお願いします。ファイルYを編集してください。"),
            state_dir=self.state_dir,
        )
        self.assertAllowed(result)

    def test_denied_call_never_becomes_a_candidate(self) -> None:
        """拒否された呼び出しは完了しないので、後続を連鎖的に巻き込まない。

        旧実装は拒否した呼び出しも履歴に積み、それが次の比較対象になっていた。
        """
        report = "\n".join(self._report_lines())
        self.complete_call("agent-A", "investigator", "調査してください。", report)

        denied = run_guard(
            make_payload(self.session, "implementer", "以下を踏まえて実装してください。\n" + report),
            state_dir=self.state_dir,
        )
        self.assertEqual(decision_of(denied), "deny", denied.stdout)

        # 拒否された呼び出しと同じ内容でも、それ自体は候補にならない。
        # (依然 agent-A の出力とは一致するので deny のままだが、理由は agent-A 側)
        # 無関係な内容の後続は素直に通ること。
        follow_up = run_guard(
            make_payload(self.session, "reviewer", "ダッシュボードの色設計をレビューしてください。"),
            state_dir=self.state_dir,
        )
        self.assertAllowed(follow_up)

    def test_short_prompt_is_not_flagged(self) -> None:
        """短いprompt同士は MIN_PROMPT_LINES により類似判定の対象外。"""
        self.complete_call("agent-A", "implementer", "直して", "直しました")
        result = run_guard(make_payload(self.session, "implementer", "直して"), state_dir=self.state_dir)
        self.assertAllowed(result)

    def test_boilerplate_lines_do_not_trigger_echo(self) -> None:
        """重複が短い定型行だけのときは出力再送とみなさない(MIN_LINE_CHARS)。"""
        boilerplate = "\n".join(["- はい", "```", "## 概要", "OK", "---"] * 4)
        self.complete_call("agent-A", "investigator", "調査してください。", boilerplate)
        result = run_guard(
            make_payload(self.session, "implementer", boilerplate + "\n新しく独立した実装作業の依頼です。"),
            state_dir=self.state_dir,
        )
        self.assertAllowed(result)

    def test_dissimilar_prompt_is_allowed(self) -> None:
        self.complete_call(
            "agent-A",
            "general-purpose",
            "決済モジュールのリトライ処理を調査してください。",
            "調査が完了しました。",
        )
        result = run_guard(
            make_payload(self.session, "general-purpose", "ダッシュボードの SSE ストリーム実装を新規に作成してください。"),
            state_dir=self.state_dir,
        )
        self.assertAllowed(result)

    def test_multiple_distinct_calls_allowed(self) -> None:
        prompts = [
            "画像リンクの一覧をExcelに出力するスクリプトを書いてください。",
            "ダッシュボードのグラフ色を調整してください。",
            "README のインストール手順を更新してください。",
        ]
        for index, prompt in enumerate(prompts):
            result = run_guard(make_payload(self.session, "general-purpose", prompt), state_dir=self.state_dir)
            self.assertAllowed(result)
            self.complete_call(f"agent-{index}", "general-purpose", prompt, f"{index} 番の作業が完了しました。")


class ScopingTests(_StateDirTestCase):
    """状態の分離(タスク境界・セッション境界・サブエージェント内)。"""

    def test_new_prompt_id_starts_fresh_history(self) -> None:
        """ユーザーの次の発言(= 別 prompt_id)では履歴がリセットされる。"""
        report = "\n".join(f"前段の報告 {i}: 十分に長い内容の行です。" for i in range(20))
        self.complete_call("agent-A", "investigator", "調査してください。", report)
        relay = "以下を踏まえて実装してください。\n" + report

        denied = run_guard(make_payload(self.session, "implementer", relay), state_dir=self.state_dir)
        self.assertEqual(decision_of(denied), "deny", denied.stdout)

        allowed = run_guard(
            make_payload(self.session, "implementer", relay, prompt_id="prompt-2"),
            state_dir=self.state_dir,
        )
        self.assertAllowed(allowed)

    def test_other_session_history_is_not_visible(self) -> None:
        report = "\n".join(f"前段の報告 {i}: 十分に長い内容の行です。" for i in range(20))
        self.complete_call("agent-A", "investigator", "調査してください。", report)
        relay = "以下を踏まえて実装してください。\n" + report

        denied = run_guard(make_payload(self.session, "implementer", relay), state_dir=self.state_dir)
        self.assertEqual(decision_of(denied), "deny", denied.stdout)

        other_session = run_guard(make_payload(str(uuid.uuid4()), "implementer", relay), state_dir=self.state_dir)
        self.assertAllowed(other_session)

    def test_call_from_inside_subagent_is_skipped(self) -> None:
        """サブエージェント内からの呼び出し(agent_id あり)は判定も記録もしない。"""
        report = "\n".join(f"前段の報告 {i}: 十分に長い内容の行です。" for i in range(20))
        self.complete_call("agent-A", "investigator", "調査してください。", report)

        result = run_guard(
            make_payload(
                self.session,
                "implementer",
                "以下を踏まえて実装してください。\n" + report,
                agent_id="agent-Z",
            ),
            state_dir=self.state_dir,
        )
        self.assertAllowed(result)
        starts = [n for n in os.listdir(self.task_dir()) if n.endswith(".start.json")]
        self.assertEqual(starts, [])

    def test_expired_history_is_ignored(self) -> None:
        """TTL を過ぎた完了記録は判定対象から外れる(prompt_id が無い場合の保険)。"""
        report = "\n".join(f"前段の報告 {i}: 十分に長い内容の行です。" for i in range(20))
        self.complete_call("agent-A", "investigator", "調査してください。", report)
        result = run_guard(
            make_payload(self.session, "implementer", "以下を踏まえて実装してください。\n" + report),
            state_dir=self.state_dir,
            AGENT_RELAY_GUARD_TTL_SEC="0",
        )
        self.assertAllowed(result)

    def test_missing_prompt_id_falls_back(self) -> None:
        payload = make_payload(self.session, "implementer", "実装をお願いします。")
        del payload["prompt_id"]
        result = run_guard(payload, state_dir=self.state_dir)
        self.assertAllowed(result)
        self.assertTrue(os.path.isdir(self.task_dir("no-prompt-id")))

    def test_missing_tool_use_id_does_not_collide(self) -> None:
        for _ in range(3):
            payload = make_payload(self.session, "implementer", "実装をお願いします。")
            del payload["tool_use_id"]
            self.assertAllowed(run_guard(payload, state_dir=self.state_dir))
        starts = [n for n in os.listdir(self.task_dir()) if n.endswith(".start.json")]
        self.assertEqual(len(starts), 3, starts)


class RobustnessTests(_StateDirTestCase):
    def test_disable_env_var_always_allows(self) -> None:
        report = "\n".join(f"前段の報告 {i}: 十分に長い内容の行です。" for i in range(20))
        self.complete_call("agent-A", "investigator", "調査してください。", report)
        payload = make_payload(self.session, "implementer", "以下を踏まえて実装してください。\n" + report)
        self.assertAllowed(run_guard(payload, state_dir=self.state_dir, disable=True))

    def test_disable_env_var_skips_recording(self) -> None:
        payload = make_payload(self.session, "implementer", "実装をお願いします。")
        run_guard(payload, state_dir=self.state_dir, disable=True)
        self.assertEqual(os.listdir(self.state_dir), [])

    def test_broken_json_is_allowed(self) -> None:
        for hook in ALL_HOOKS:
            with self.subTest(hook=os.path.basename(hook)):
                result = _run(hook, "{ this is not valid json", state_dir=self.state_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_empty_input_is_allowed(self) -> None:
        for hook in ALL_HOOKS:
            with self.subTest(hook=os.path.basename(hook)):
                result = _run(hook, "", state_dir=self.state_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_missing_session_id_is_allowed(self) -> None:
        payload = make_payload(self.session, "implementer", "実装をお願いします。")
        del payload["session_id"]
        result = run_guard(payload, state_dir=self.state_dir)
        self.assertAllowed(result)


class RecordHookTests(_StateDirTestCase):
    def test_record_hook_writes_call_record(self) -> None:
        run_record(self.session, "agent-A", "implementer", "実装依頼\n2行目です", state_dir=self.state_dir)
        path = os.path.join(self.task_dir(), "agent-A.call.json")
        self.assertTrue(os.path.exists(path), os.listdir(self.task_dir()))
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["agent_id"], "agent-A")
        self.assertEqual(record["subagent_type"], "implementer")
        self.assertEqual(record["norm_lines"], ["実装依頼", "2行目です"])

    def test_record_hook_without_agent_id_writes_nothing(self) -> None:
        payload = {
            "session_id": self.session,
            "prompt_id": "prompt-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "implementer", "prompt": "実装依頼"},
            "tool_response": {"isAsync": True},
        }
        result = _run(RECORD_HOOK, payload, state_dir=self.state_dir)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(os.listdir(self.state_dir), [])


class CompleteHookTests(_StateDirTestCase):
    def test_complete_hook_writes_done_record(self) -> None:
        run_complete(self.session, "agent-A", "報告です\n\n2行目", state_dir=self.state_dir)
        path = os.path.join(self.task_dir(), "agent-A.done.json")
        self.assertTrue(os.path.exists(path), os.listdir(self.task_dir()))
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["agent_id"], "agent-A")
        self.assertEqual(record["output_lines"], ["報告です", "2行目"])
        self.assertFalse(record["output_truncated"])

    def test_complete_hook_truncates_long_output(self) -> None:
        output = "\n".join(f"報告の行 {i} です。" for i in range(1000))
        run_complete(self.session, "agent-A", output, state_dir=self.state_dir)
        with open(os.path.join(self.task_dir(), "agent-A.done.json"), encoding="utf-8") as f:
            record = json.load(f)
        self.assertLessEqual(len(record["output_lines"]), 400)
        self.assertTrue(record["output_truncated"])

    def test_complete_hook_never_blocks_stop(self) -> None:
        """SubagentStop hook は exit 2 を返さない(サブエージェントの停止を妨げない)。"""
        result = run_complete(self.session, "agent-A", "報告", state_dir=self.state_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


class GcHookTests(_StateDirTestCase):
    def _run_gc(self) -> subprocess.CompletedProcess:
        return _run(GC_HOOK, {"session_id": self.session}, state_dir=self.state_dir)

    def test_gc_keeps_fresh_session_dirs(self) -> None:
        self.complete_call("agent-A", "implementer", "実装依頼", "完了しました")
        self._run_gc()
        self.assertTrue(os.path.isdir(os.path.join(self.state_dir, self.session)))

    def test_gc_removes_old_session_dirs(self) -> None:
        self.complete_call("agent-A", "implementer", "実装依頼", "完了しました")
        stale = os.path.join(self.state_dir, self.session)
        old = time.time() - 8 * 24 * 3600
        os.utime(stale, (old, old))
        self._run_gc()
        self.assertFalse(os.path.exists(stale), os.listdir(self.state_dir))

    def test_gc_removes_legacy_jsonl_state(self) -> None:
        """旧実装 (<session>.jsonl 単一ファイル) の残骸も同じ基準で片付ける。"""
        legacy = os.path.join(self.state_dir, "old-session.jsonl")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("{}\n")
        old = time.time() - 8 * 24 * 3600
        os.utime(legacy, (old, old))
        self._run_gc()
        self.assertFalse(os.path.exists(legacy))

    def test_gc_on_missing_state_dir_is_noop(self) -> None:
        missing = os.path.join(self.state_dir, "does-not-exist")
        result = _run(GC_HOOK, {"session_id": self.session}, state_dir=missing)
        self.assertEqual(result.returncode, 0, result.stderr)


class DefaultStateDirTests(unittest.TestCase):
    """既定の状態ディレクトリが hook 自身の位置(<root>/.claude/agent-calls)から
    導出され、読む側(guard)と書く側(record/complete)とGCで一致することの検証。

    状態ディレクトリを固定パス（例: /workspace）で決め打ちにすると、チェックアウト先が
    異なる環境で読む側と書く側がすれ違い、ガードが常時フェイルオープンになりうる。
    それを防ぐため、両者が hook 自身の位置から同じ規則で導出することを検証する。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        hooks_dir = os.path.join(self.root, ".claude", "hooks")
        os.makedirs(hooks_dir)
        self.hooks = {}
        for hook in ALL_HOOKS:
            dest = os.path.join(hooks_dir, os.path.basename(hook))
            shutil.copy2(hook, dest)
            self.hooks[os.path.basename(hook)] = dest
        self.session = str(uuid.uuid4())
        # 状態ディレクトリを env で差し替えず、hook 自身の既定値を使わせる。
        self.env = dict(os.environ)
        self.env.pop("AGENT_RELAY_GUARD_STATE_DIR", None)
        self.env.pop("AGENT_RELAY_GUARD_DISABLE", None)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)

    def _run(self, name: str, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.hooks[name]],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.outside.name,
            timeout=20,
        )

    @property
    def default_dir(self) -> str:
        return os.path.join(self.root, ".claude", "agent-calls")

    def test_guard_defaults_state_dir_to_the_root_containing_the_hook(self) -> None:
        self._run("agent-relay-guard.sh", make_payload(self.session, "implementer", "実装依頼"))
        task_dir = os.path.join(self.default_dir, self.session, "prompt-1")
        self.assertTrue(os.path.isdir(task_dir), os.listdir(self.default_dir))
        self.assertTrue(any(n.endswith(".start.json") for n in os.listdir(task_dir)))

    def test_record_and_complete_write_into_the_dir_the_guard_reads(self) -> None:
        self._run(
            "agent-call-record.sh",
            {
                "session_id": self.session,
                "prompt_id": "prompt-1",
                "tool_use_id": "toolu_x",
                "tool_input": {"subagent_type": "investigator", "prompt": "調査依頼"},
                "tool_response": {"agentId": "agent-A"},
            },
        )
        self._run(
            "agent-call-complete.sh",
            {
                "session_id": self.session,
                "prompt_id": "prompt-1",
                "agent_id": "agent-A",
                "agent_type": "investigator",
                "last_assistant_message": "報告です",
            },
        )
        task_dir = os.path.join(self.default_dir, self.session, "prompt-1")
        names = sorted(os.listdir(task_dir))
        self.assertIn("agent-A.call.json", names)
        self.assertIn("agent-A.done.json", names)

    def test_gc_targets_the_same_default_dir(self) -> None:
        self._run("agent-relay-guard.sh", make_payload(self.session, "implementer", "実装依頼"))
        stale = os.path.join(self.default_dir, self.session)
        old = time.time() - 8 * 24 * 3600
        os.utime(stale, (old, old))
        self._run("agent-calls-gc.sh", {"session_id": self.session})
        self.assertFalse(os.path.exists(stale), os.listdir(self.default_dir))


if __name__ == "__main__":
    unittest.main()
