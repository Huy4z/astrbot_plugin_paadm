"""可选的真实 paadm 端到端测试（本机/CI 有二进制时才运行，否则 skip）。

用途：用真实 paadm debug 二进制验证插件生成的 argv（clap 参数名）与 JSON
契约确实匹配，避免「单测全绿但容器里参数不存在」的部署事故。

查找顺序：
1. 环境变量 ``PAADM_TEST_BIN``；
2. 工作区相对路径 ``<workspace>/paadm/target/debug/paadm``；
3. 固定本机路径（仅在原开发机存在）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
WORKSPACE = PLUGIN_DIR.parent.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import command_router  # noqa: E402
import paadm_client  # noqa: E402


def _find_paadm_bin() -> str:
    candidates = [
        os.environ.get("PAADM_TEST_BIN", ""),
        str(WORKSPACE / "paadm" / "target" / "debug" / "paadm"),
        "/home/huy/Projects/paa-demo/paadm/target/debug/paadm",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return ""


PAADM_BIN = _find_paadm_bin()


@unittest.skipUnless(PAADM_BIN, "本机没有可执行的真实 paadm 二进制（设置 PAADM_TEST_BIN 可启用）")
class RealPaadmEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "paa.db")
        cls.client = paadm_client.PaadmClient(PAADM_BIN, cls.db_path, timeout_seconds=10)
        resolution = cls.client.resolve_user("qq", "integration-user", source="command")
        assert resolution.ok, resolution.error_message
        cls.user_id = resolution.user_id

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_command(self, text: str) -> command_router.CommandPlan:
        plan = command_router.parse_command(text.split())
        result = self.client.run(
            plan.argv,
            source="command",
            action=plan.action,
            user_id=self.user_id,
        )
        self.assertTrue(
            result.success,
            f"{text} 失败: code={result.error_code} msg={result.error_message} "
            f"stdout={result.stdout} stderr={result.stderr}",
        )
        reply = command_router.format_reply(plan, result, reply_max_rows=20)
        self.assertTrue(reply)
        return plan

    def run_llm_action(self, action: str, payload: dict) -> dict:
        plan = command_router.build_action(action, payload)
        use_user = None if action.startswith("user.") else self.user_id
        result = self.client.run(
            plan.argv, source="llm_tool", action=plan.action, user_id=use_user
        )
        self.assertTrue(
            result.success,
            f"{action} 失败: code={result.error_code} msg={result.error_message} "
            f"stdout={result.stdout} stderr={result.stderr}",
        )
        payload_json = json.loads(command_router.tool_result_json(result))
        self.assertTrue(payload_json["success"])
        return payload_json["data"]

    def test_user_and_task_lifecycle(self):
        data = self.run_llm_action("task.create", {"title": "集成测试待办", "priority": 2})
        task_id = data["id"]
        self.run_command("task list --status pending")
        self.run_command(f"task done {task_id}")
        self.run_command(f"task del {task_id}")

    def test_transaction(self):
        self.run_command(
            "tx add --type expense --amount 18.50 --category food --desc 午饭 "
            "--at 2026-11-17T12:00:00+08:00"
        )
        self.run_command(
            "tx list --from 2026-11-01T00:00:00+08:00 --to 2026-12-01T00:00:00+08:00"
        )
        self.run_command(
            "tx sum --from 2026-11-01T00:00:00+08:00 --to 2026-12-01T00:00:00+08:00"
        )

    def test_schedule_and_reminder(self):
        self.run_command(
            "schedule add 组会 --start 2026-11-20T10:00:00+08:00 "
            "--end 2026-11-20T11:00:00+08:00 --location 实验室 --all-day 0"
        )
        self.run_command(
            "schedule list --from 2026-11-20T00:00:00+08:00 --to 2026-11-21T00:00:00+08:00"
        )
        self.run_command("reminder add 交作业 --at 2026-11-19T20:00:00+08:00")
        self.run_command("reminder list --status pending")

    def test_note(self):
        data = self.run_llm_action("note.create", {"content": "集成测试笔记", "title": "集成"})
        note_id = data["id"]
        self.run_command("note list --q 集成测试")
        self.run_command(f"note get {note_id}")

    def test_error_code_mapping(self):
        plan = command_router.parse_command(["task", "done", "no-such-id"])
        result = self.client.run(
            plan.argv, source="command", action=plan.action, user_id=self.user_id
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "TASK_NOT_FOUND")
        self.assertEqual(result.exit_code, 1)

    def test_llm_actions(self):
        cases = [
            ("task.create", {"title": "LLM 待办"}),
            ("task.list", {"status": "pending", "limit": 5}),
            ("schedule.create", {
                "title": "LLM 日程",
                "start_at": "2026-11-21T10:00:00+08:00",
                "end_at": "2026-11-21T11:00:00+08:00",
            }),
            ("schedule.list", {"limit": 5}),
            ("reminder.create", {
                "content": "LLM 提醒",
                "remind_at": "2026-11-21T09:00:00+08:00",
            }),
            ("reminder.list", {"status": "pending"}),
            ("transaction.create", {"type": "income", "amount": 10000, "category": "salary"}),
            ("transaction.summary", {"from": "2026-11-01T00:00:00+08:00"}),
            ("note.create", {"content": "LLM 笔记"}),
            ("note.list", {"keyword": "LLM"}),
            ("user.get", {"id": self.user_id}),
        ]
        for action, payload in cases:
            with self.subTest(action=action):
                self.run_llm_action(action, payload)


if __name__ == "__main__":
    unittest.main()
