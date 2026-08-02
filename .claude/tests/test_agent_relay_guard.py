"""agent-relay-guard.sh (PreToolUse hook) のテスト。

subprocess で hook スクリプトに JSON を流し込み、許可/拒否の分岐を検証する。
状態ファイルは環境変数 AGENT_RELAY_GUARD_STATE_DIR で一時ディレクトリに差し替える。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid

HOOKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"
)
HOOK_PATH = os.path.join(HOOKS_DIR, "agent-relay-guard.sh")
RESET_HOOK_PATH = os.path.join(HOOKS_DIR, "agent-turn-reset.sh")


def run_hook(
    payload: dict,
    *,
    state_dir: str,
    disable: bool = False,
    ttl_sec: str | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AGENT_RELAY_GUARD_STATE_DIR"] = state_dir
    if disable:
        env["AGENT_RELAY_GUARD_DISABLE"] = "1"
    else:
        env.pop("AGENT_RELAY_GUARD_DISABLE", None)
    if ttl_sec is not None:
        env["AGENT_RELAY_GUARD_TTL_SEC"] = ttl_sec
    return subprocess.run(
        [HOOK_PATH],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def run_reset_hook(session_id: str, *, state_dir: str) -> subprocess.CompletedProcess:
    """UserPromptSubmit hook (タスク境界での履歴リセット) を実行する。"""
    env = dict(os.environ)
    env["AGENT_RELAY_GUARD_STATE_DIR"] = state_dir
    return subprocess.run(
        [RESET_HOOK_PATH],
        input=json.dumps({"session_id": session_id, "prompt": "次のタスクです"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def make_payload(session_id: str, subagent_type: str, prompt: str) -> dict:
    return {
        "session_id": session_id,
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt},
    }


def is_deny(result: subprocess.CompletedProcess) -> bool:
    out = result.stdout.strip()
    if not out:
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    return data.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class AgentRelayGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = self._tmp.name

    def test_first_calls_for_each_role_are_allowed(self) -> None:
        session = str(uuid.uuid4())
        for role in ("investigator", "implementer", "reviewer"):
            payload = make_payload(
                session,
                role,
                f"{role} 用の全く異なる調査・実装・レビュー依頼テキストです。",
            )
            result = run_hook(payload, state_dir=self.state_dir)
            self.assertEqual(result.returncode, 0)
            self.assertFalse(is_deny(result), f"role={role}: {result.stdout}")

    def test_same_role_is_allowed_again_after_turn_reset(self) -> None:
        """ユーザーの次の指示(= 別タスク)以降は同じ役割を再び呼び出せる。"""
        session = str(uuid.uuid4())
        first = make_payload(
            session,
            "implementer",
            "課題Aの実装をお願いします。ファイルXを編集してください。",
        )
        second = make_payload(
            session,
            "implementer",
            "課題Cの別の実装をお願いします。ファイルYを編集してください。",
        )
        self.assertFalse(is_deny(run_hook(first, state_dir=self.state_dir)))

        reset = run_reset_hook(session, state_dir=self.state_dir)
        self.assertEqual(reset.returncode, 0)

        result2 = run_hook(second, state_dir=self.state_dir)
        self.assertFalse(is_deny(result2), result2.stdout)

    def test_reset_hook_does_not_clear_other_sessions(self) -> None:
        session_a = str(uuid.uuid4())
        session_b = str(uuid.uuid4())
        payload_a = make_payload(
            session_a, "reviewer", "セッションAのレビュー依頼です。"
        )
        payload_b = make_payload(
            session_b, "reviewer", "セッションBのレビュー依頼です。"
        )
        self.assertFalse(is_deny(run_hook(payload_a, state_dir=self.state_dir)))
        self.assertFalse(is_deny(run_hook(payload_b, state_dir=self.state_dir)))

        run_reset_hook(session_a, state_dir=self.state_dir)

        # A はリセットされたので許可、B は履歴が残っているので拒否。
        self.assertFalse(is_deny(run_hook(payload_a, state_dir=self.state_dir)))
        self.assertTrue(is_deny(run_hook(payload_b, state_dir=self.state_dir)))

    def test_expired_history_is_ignored(self) -> None:
        """TTL を過ぎた履歴は別タスクのものとみなし、同一役割の再呼び出しを許可する。"""
        session = str(uuid.uuid4())
        first = make_payload(session, "implementer", "課題Aの実装をお願いします。")
        second = make_payload(session, "implementer", "課題Bの実装をお願いします。")
        self.assertFalse(
            is_deny(run_hook(first, state_dir=self.state_dir, ttl_sec="0"))
        )
        result2 = run_hook(second, state_dir=self.state_dir, ttl_sec="0")
        self.assertFalse(is_deny(result2), result2.stdout)

    def test_second_call_to_same_role_in_same_task_is_denied(self) -> None:
        session = str(uuid.uuid4())
        first = make_payload(
            session,
            "implementer",
            "課題Aの実装をお願いします。ファイルXを編集してください。",
        )
        second = make_payload(
            session,
            "implementer",
            "課題Cの別の実装をお願いします。ファイルYを編集してください。",
        )
        result1 = run_hook(first, state_dir=self.state_dir)
        self.assertFalse(is_deny(result1))
        result2 = run_hook(second, state_dir=self.state_dir)
        self.assertTrue(is_deny(result2), result2.stdout)

    def test_different_session_id_is_allowed(self) -> None:
        session_a = str(uuid.uuid4())
        session_b = str(uuid.uuid4())
        payload_a = make_payload(session_a, "reviewer", "レビュー依頼その1です。")
        payload_b = make_payload(session_b, "reviewer", "レビュー依頼その1です。")
        result_a = run_hook(payload_a, state_dir=self.state_dir)
        self.assertFalse(is_deny(result_a))
        result_b = run_hook(payload_b, state_dir=self.state_dir)
        self.assertFalse(is_deny(result_b), result_b.stdout)

    def test_similar_prompt_resend_is_denied(self) -> None:
        session = str(uuid.uuid4())
        prompt1 = "\n".join(
            [
                "調査対象: src/payments/retry.py",
                "手順1: リトライロジックを確認する",
                "手順2: エラーハンドリングを確認する",
                "手順3: テストの有無を確認する",
            ]
        )
        # 前段の結果をほぼ丸ごと再送したケースを模した、共通行の多いprompt。
        prompt2 = prompt1 + "\n手順4: 以上を踏まえて実装する"
        result1 = run_hook(
            make_payload(session, "general-purpose", prompt1), state_dir=self.state_dir
        )
        self.assertFalse(is_deny(result1))
        result2 = run_hook(
            make_payload(session, "general-purpose", prompt2), state_dir=self.state_dir
        )
        self.assertTrue(is_deny(result2), result2.stdout)

    def test_dissimilar_prompt_is_allowed(self) -> None:
        session = str(uuid.uuid4())
        prompt1 = "決済モジュールのリトライ処理を調査してください。"
        prompt2 = "ダッシュボードの SSE ストリーム実装を新規に作成してください。"
        result1 = run_hook(
            make_payload(session, "general-purpose", prompt1), state_dir=self.state_dir
        )
        self.assertFalse(is_deny(result1))
        result2 = run_hook(
            make_payload(session, "general-purpose", prompt2), state_dir=self.state_dir
        )
        self.assertFalse(is_deny(result2), result2.stdout)

    def test_general_purpose_multiple_calls_allowed(self) -> None:
        session = str(uuid.uuid4())
        prompts = [
            "画像リンクの一覧をExcelに出力するスクリプトを書いてください。",
            "ダッシュボードのグラフ色を調整してください。",
            "README のインストール手順を更新してください。",
        ]
        for prompt in prompts:
            result = run_hook(
                make_payload(session, "general-purpose", prompt),
                state_dir=self.state_dir,
            )
            self.assertFalse(is_deny(result), result.stdout)

    def test_disable_env_var_always_allows(self) -> None:
        session = str(uuid.uuid4())
        payload = make_payload(session, "implementer", "同じ実装依頼です。")
        result1 = run_hook(payload, state_dir=self.state_dir, disable=True)
        result2 = run_hook(payload, state_dir=self.state_dir, disable=True)
        self.assertFalse(is_deny(result1))
        self.assertFalse(is_deny(result2))
        # バイパス中は記録も行われないため状態ファイルは作られない。
        self.assertEqual(os.listdir(self.state_dir), [])

    def test_broken_json_is_allowed(self) -> None:
        env = dict(os.environ)
        env["AGENT_RELAY_GUARD_STATE_DIR"] = self.state_dir
        env.pop("AGENT_RELAY_GUARD_DISABLE", None)
        result = subprocess.run(
            [HOOK_PATH],
            input="{ this is not valid json",
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(is_deny(result))

    def test_empty_input_is_allowed(self) -> None:
        env = dict(os.environ)
        env["AGENT_RELAY_GUARD_STATE_DIR"] = self.state_dir
        env.pop("AGENT_RELAY_GUARD_DISABLE", None)
        result = subprocess.run(
            [HOOK_PATH],
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(is_deny(result))


class AgentRelayGuardDefaultStateDirTests(unittest.TestCase):
    """既定の状態ディレクトリが hook 自身の位置(<root>/.claude/agent-calls)から
    導出され、書く側(guard)とリセット側(reset)で一致することの検証。

    /workspace 決め打ちだと、チェックアウト先が異なる環境で guard は makedirs 失敗で
    フェイルオープン(ガード無効化)し、reset はディレクトリ不在で何もしない。
    env で state_dir を明示しない実運用パスを、別ルートに両hookを複製して検証する。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.realpath(self._tmp.name)
        hooks_dir = os.path.join(self.root, ".claude", "hooks")
        os.makedirs(hooks_dir)
        self.guard = os.path.join(hooks_dir, os.path.basename(HOOK_PATH))
        self.reset = os.path.join(hooks_dir, os.path.basename(RESET_HOOK_PATH))
        shutil.copy2(HOOK_PATH, self.guard)
        shutil.copy2(RESET_HOOK_PATH, self.reset)
        self.default_state_dir = os.path.join(self.root, ".claude", "agent-calls")

    def _env_without_state_dir(self) -> dict:
        env = dict(os.environ)
        env.pop("AGENT_RELAY_GUARD_STATE_DIR", None)
        env.pop("AGENT_RELAY_GUARD_DISABLE", None)
        return env

    def test_guard_defaults_state_dir_to_the_root_containing_the_hook(self) -> None:
        session = "sess-default"
        payload = make_payload(session, "implementer", "実装してください。")
        result = subprocess.run(
            [self.guard],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=self._env_without_state_dir(),
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            os.path.isdir(self.default_state_dir),
            f"guard が hook 位置基準の既定ディレクトリを作っていません: {self.default_state_dir}",
        )
        self.assertEqual(
            os.listdir(self.default_state_dir),
            [f"{session}.jsonl"],
            "guard が既定ディレクトリに履歴を記録していません",
        )

    def test_reset_clears_history_in_the_same_default_dir_the_guard_wrote(self) -> None:
        # guard が既定ディレクトリに書いた履歴を、reset が同じ既定ディレクトリで消せること
        # (書く側と読む側の既定が一致していることの behavioral な確認)。
        session = "sess-shared"
        payload = make_payload(session, "reviewer", "レビューしてください。")
        subprocess.run(
            [self.guard],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            env=self._env_without_state_dir(),
            timeout=20,
        )
        state_file = os.path.join(self.default_state_dir, f"{session}.jsonl")
        self.assertTrue(
            os.path.exists(state_file), "前提: guard が履歴を書けていません"
        )

        subprocess.run(
            [self.reset],
            input=json.dumps({"session_id": session, "prompt": "次のタスク"}),
            capture_output=True,
            text=True,
            env=self._env_without_state_dir(),
            timeout=20,
        )
        self.assertFalse(
            os.path.exists(state_file),
            "reset が guard と同じ既定ディレクトリの履歴を消せていません(既定がすれ違っています)",
        )


if __name__ == "__main__":
    unittest.main()
