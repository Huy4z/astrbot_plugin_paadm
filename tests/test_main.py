"""main.py handler 单测：/paa 命令、LLM 工具、启动自检、provider 映射、交互日志。

用 sys.modules 注入 astrbot stub + 假 paadm 脚本，不依赖真实 AstrBot/paadm。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import _support  # noqa: E402

MODULE = _support.load_plugin()


class MainTestBase(unittest.TestCase):
    MODE = "success"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script, self.calls = _support.write_fake_paadm(
            self.tmp.name, self.MODE, name=f"paadm-{self.MODE}"
        )
        self.plugin = _support.make_plugin(
            MODULE,
            self.tmp.name,
            paadm_bin=self.script,
        )
        self.addCleanup(self._close)

    def _close(self):
        try:
            asyncio.run(self.plugin.terminate())
        except Exception:  # noqa: BLE001
            pass

    def run_paa(self, text: str, event=None):
        event = event or _support.FakeEvent()
        results = _support.run_handler(self.plugin, event, text)
        self.assertEqual(len(results), 1, "命令应只 yield 一条回复")
        return results[0].text, event

    def rows(self):
        return self.plugin.store.fetch_recent(20)

    def calls_list(self):
        return _support.read_calls(self.calls)


class HelpTest(MainTestBase):
    def test_empty_shows_help(self):
        text, _ = self.run_paa("")
        self.assertIn("task add", text)
        self.assertIn("tx add", text)
        self.assertIn("note get", text)

    def test_help_command(self):
        text, _ = self.run_paa("help")
        self.assertIn("/paa help", text)

    def test_no_paadm_call_for_help(self):
        self.run_paa("help")
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])


class UserTest(MainTestBase):
    def test_user_resolve_and_display(self):
        text, _ = self.run_paa("user")
        self.assertIn("paa-user-1", text)
        self.assertIn("provider", text)
        calls = self.calls_list()
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            ["--json", "user", "resolve", "--provider", "qq",
             "--external-user-id", "2856307114"],
        )
        self.assertEqual(self.rows()[0]["action"], "user.resolve")
        self.assertEqual(self.rows()[0]["source"], "command")
        self.assertEqual(self.rows()[0]["platform"], "aiocqhttp")
        self.assertEqual(self.rows()[0]["sender_id"], "2856307114")

    def test_webchat_provider(self):
        event = _support.FakeEvent(platform="webchat", sender_id="web-1")
        self.run_paa("user", event=event)
        self.assertIn("--provider", self.calls_list()[0])
        self.assertIn("webchat", self.calls_list()[0])

    def test_provider_override(self):
        plugin = _support.make_plugin(
            MODULE, self.tmp.name, paadm_bin=self.script, provider_override="CUSTOM"
        )
        event = _support.FakeEvent(platform="aiocqhttp")
        _support.run_handler(plugin, event, "user")
        self.assertTrue(any("custom" in call for call in _support.read_calls(self.calls)))


class TaskCommandTest(MainTestBase):
    def test_task_add_builds_argv_and_logs(self):
        text, _ = self.run_paa(
            'task add 写周报 --due 2026-11-20T18:00:00+08:00 --priority 2 --desc "周五交"'
        )
        self.assertIn("已创建待办", text)
        calls = self.calls_list()
        self.assertEqual(len(calls), 2)  # user.resolve + task.create
        self.assertEqual(
            calls[1],
            [
                "--json", "--user-id", "paa-user-1",
                "task", "create", "--title", "写周报",
                "--due-at", "2026-11-20T18:00:00+08:00",
                "--priority", "2", "--description", "周五交",
            ],
        )
        rows = self.rows()
        self.assertEqual(rows[0]["action"], "task.create")
        self.assertEqual(rows[0]["paadm_user_id"], "paa-user-1")
        self.assertEqual(rows[0]["success"], 1)
        self.assertEqual(rows[0]["exit_code"], 0)
        self.assertEqual(rows[1]["action"], "user.resolve")

    def test_task_list_limit(self):
        text, _ = self.run_paa("task list --limit 1")
        self.assertIn("共 2 条待办", text)
        self.assertIn("仅显示前 1 条", text)
        self.assertEqual(text.count("\n- "), 1)

    def test_task_done(self):
        text, _ = self.run_paa("task done item-1")
        self.assertIn("已完成待办", text)
        self.assertIn("item-1", text)
        self.assertEqual(
            self.calls_list()[1][-4:],
            ["task", "complete", "--id", "item-1"],
        )

    def test_task_del(self):
        text, _ = self.run_paa("task del item-1")
        self.assertIn("已删除待办", text)
        self.assertEqual(self.calls_list()[1][3:], ["task", "delete", "--id", "item-1"])

    def test_schedule_add(self):
        text, _ = self.run_paa(
            "schedule add 组会 --start 2026-11-20T10:00:00+08:00 "
            "--end 2026-11-20T11:00:00+08:00 --location 实验室 --all-day 0"
        )
        self.assertIn("已创建日程", text)
        argv = self.calls_list()[1]
        self.assertEqual(argv[-8:], [
            "--start-at", "2026-11-20T10:00:00+08:00",
            "--end-at", "2026-11-20T11:00:00+08:00",
            "--location", "实验室", "--all-day", "0",
        ])

    def test_reminder_add(self):
        text, _ = self.run_paa(
            "reminder add 交作业 --at 2026-11-17T20:00:00+08:00 --item item-1"
        )
        self.assertIn("已创建提醒", text)
        self.assertIn("--remind-at", self.calls_list()[1])
        self.assertIn("--item-id", self.calls_list()[1])

    def test_tx_add(self):
        text, _ = self.run_paa(
            "tx add --type expense --amount 18.50 --category food "
            "--desc 午饭 --at 2026-11-17T12:00:00+08:00"
        )
        self.assertIn("已记账", text)
        self.assertIn("18.50 元", text)
        self.assertEqual(self.calls_list()[1][-10:], [
            "--type", "expense",
            "--amount", "1850", "--category", "food",
            "--occurred-at", "2026-11-17T12:00:00+08:00",
            "--description", "午饭",
        ])

    def test_tx_sum(self):
        text, _ = self.run_paa("tx sum")
        self.assertIn("收支汇总", text)
        self.assertIn("收入：10.00 元", text)
        self.assertIn("支出：18.50 元", text)
        self.assertIn("净额：-8.50 元", text)

    def test_note_add_and_get(self):
        text, _ = self.run_paa("note add 记一个点子 --title 想法")
        self.assertIn("已创建笔记", text)
        self.assertEqual(
            self.calls_list()[1][3:7], ["note", "create", "--content", "记一个点子"]
        )
        text, _ = self.run_paa("note get note-1")
        self.assertIn("正文内容", text)

    def test_raw_command_removed(self):
        text, _ = self.run_paa("raw task list --status pending")
        self.assertIn("未知命令", text)
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])

    def test_parse_error_does_not_call_paadm(self):
        text, _ = self.run_paa("task add --priority 2")
        self.assertIn("参数错误", text)
        self.assertIn("标题", text)
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])

    def test_unknown_command(self):
        text, _ = self.run_paa("nonsense")
        self.assertIn("未知命令", text)
        self.assertIn("/paa help", text)

    def test_quoted_title(self):
        self.run_paa('task add "写 周报"')
        self.assertIn("写 周报", self.calls_list()[1])


class CommandFallbackTest(MainTestBase):
    def test_args_fallback_from_event_message_str(self):
        event = _support.FakeEvent(message_str="/paa task list")
        results = _support.run_handler(self.plugin, event, "")
        self.assertIn("共 2 条待办", results[0].text)
        self.assertEqual(self.calls_list()[-1][3:], ["task", "list"])

    def test_unrelated_message_str_does_not_run_command(self):
        event = _support.FakeEvent(message_str="你好呀")
        results = _support.run_handler(self.plugin, event, "")
        self.assertIn("task add", results[0].text)
        self.assertEqual(self.calls_list(), [])


class FailureTest(MainTestBase):
    MODE = "fail_after_resolve"

    def test_business_error_reply(self):
        text, _ = self.run_paa("task list")
        self.assertIn("操作失败", text)
        self.assertIn("INVALID_ARGUMENT", text)
        self.assertIn("bad arguments", text)
        rows = self.rows()
        self.assertEqual(rows[0]["action"], "task.list")
        self.assertEqual(rows[0]["success"], 0)
        self.assertEqual(rows[0]["error_code"], "INVALID_ARGUMENT")
        self.assertEqual(rows[0]["exit_code"], 2)

    def test_llm_tool_returns_error_json(self):
        event = _support.FakeEvent()
        text = _support.run(
            _support.call_llm_tool(self.plugin, event, "task.list", "{}")
        )
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")


class LlmToolTest(MainTestBase):
    def test_task_create_success(self):
        event = _support.FakeEvent()
        text = _support.run(
            _support.call_llm_tool(
                self.plugin,
                event,
                "task.create",
                json.dumps({"title": "写周报", "due_at": "2026-11-20T18:00:00+08:00"}),
            )
        )
        payload = json.loads(text)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["id"], "item-1")
        calls = self.calls_list()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][:5], ["--json", "--user-id", "paa-user-1", "task", "create"])
        rows = self.rows()
        self.assertEqual(rows[0]["source"], "llm_tool")
        self.assertEqual(rows[0]["action"], "task.create")
        self.assertEqual(rows[1]["action"], "user.resolve")

    def test_user_actions_removed_from_llm_tool(self):
        event = _support.FakeEvent(platform="webchat", sender_id="web-1")
        for action in ("user.resolve", "user.get"):
            with self.subTest(action=action):
                text = _support.run(
                    _support.call_llm_tool(
                        self.plugin,
                        event,
                        action,
                        json.dumps({"provider": "webchat", "external_user_id": "web-1"}),
                    )
                )
                payload = json.loads(text)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])

    def test_llm_tool_rejects_user_id_argument(self):
        event = _support.FakeEvent()
        text = _support.run(
            _support.call_llm_tool(
                self.plugin,
                event,
                "task.list",
                json.dumps({"user_id": "other-user"}),
            )
        )
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])

    def test_business_call_requires_user_context(self):
        result = _support.run(
            self.plugin._run_paadm(
                ["task", "list"],
                source="command",
                action="task.list",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "USER_SCOPE_VIOLATION")
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.rows(), [])

    def test_bad_json(self):
        event = _support.FakeEvent()
        text = _support.run(_support.call_llm_tool(self.plugin, event, "task.create", "{not json"))
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_unknown_action(self):
        event = _support.FakeEvent()
        text = _support.run(_support.call_llm_tool(self.plugin, event, "task.unknown", "{}"))
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.calls_list(), [])

    def test_unknown_field(self):
        text = _support.run(
            _support.call_llm_tool(
                self.plugin, _support.FakeEvent(), "task.create", '{"title":"x","bad":1}'
            )
        )
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertIn("bad", payload["error"]["message"])

    def test_tool_registered_in_main_module(self):
        names = [name for name, _func, _module in _support.LLM_TOOL_REGISTRY]
        self.assertIn("paadm", names)
        for name, func_name, module_name in _support.LLM_TOOL_REGISTRY:
            if name == "paadm":
                self.assertEqual(func_name, "paadm_tool")
                self.assertEqual(module_name, MODULE.__name__)


class StartupTest(MainTestBase):
    def test_startup_writes_two_rows(self):
        _support.run(self.plugin.on_astrbot_loaded())
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["action"] for row in rows], ["init", "version"])
        for row in rows:
            self.assertEqual(row["source"], "startup")
            self.assertEqual(row["success"], 1)
        calls = self.calls_list()
        self.assertEqual(calls, [["--json", "version"], ["--json", "init"]])

    def test_startup_is_idempotent(self):
        _support.run(self.plugin.on_astrbot_loaded())
        _support.run(self.plugin.initialize())
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.calls_list()), 2)

    def test_startup_does_not_raise_when_binary_missing(self):
        plugin = _support.make_plugin(
            MODULE,
            self.tmp.name,
            paadm_bin=os.path.join(self.tmp.name, "missing-paadm"),
        )
        try:
            _support.run(plugin.on_astrbot_loaded())
        finally:
            _support.run(plugin.terminate())
        rows = plugin.store.fetch_recent(10)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source"], "startup")
            self.assertEqual(row["success"], 0)
            self.assertEqual(row["error_code"], "PAADM_NOT_FOUND")

    def test_startup_handles_invalid_output(self):
        script, _calls = _support.write_fake_paadm(self.tmp.name, "invalid", name="invalid-paadm")
        plugin = _support.make_plugin(MODULE, self.tmp.name, paadm_bin=script)
        try:
            _support.run(plugin.on_astrbot_loaded())
        finally:
            _support.run(plugin.terminate())
        rows = plugin.store.fetch_recent(10)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["error_code"], "INVALID_OUTPUT")


class ReplyConfigTest(MainTestBase):
    def test_reply_max_rows_config(self):
        plugin = _support.make_plugin(
            MODULE, self.tmp.name, paadm_bin=self.script, reply_max_rows=1
        )
        event = _support.FakeEvent()
        results = _support.run_handler(plugin, event, "task list")
        text = results[0].text
        self.assertEqual(text.count("\n- "), 1)
        self.assertIn("仅显示前 1 条", text)

    def test_help_and_reply_length_bounded(self):
        text, _ = self.run_paa("help")
        self.assertLessEqual(len(text), 1600)


if __name__ == "__main__":
    unittest.main()

class PlatformAllowlistTest(MainTestBase):
    """平台白名单：一个实例挂多个机器人时，只让部分机器人拥有 /paa 与 paadm 工具。"""

    def test_empty_allowlist_keeps_all_platforms(self):
        event = _support.FakeEvent(platform="qq-snowluma")
        text, _ = self.run_paa("help", event=event)
        self.assertIn("/paa help", text)

    def test_command_blocked_on_other_platform(self):
        self.plugin.allowed_platforms = ["QQbot-test"]
        event = _support.FakeEvent(platform="qq-snowluma")
        results = _support.run_handler(self.plugin, event, "help")
        self.assertEqual(results, [], "不在白名单的平台应静默忽略 /paa")

    def test_command_allowed_on_listed_platform(self):
        self.plugin.allowed_platforms = ["QQbot-test"]
        event = _support.FakeEvent(platform="QQbot-test")
        text, _ = self.run_paa("help", event=event)
        self.assertIn("/paa help", text)

    def test_llm_tool_blocked_on_other_platform(self):
        self.plugin.allowed_platforms = ["QQbot-test"]
        event = _support.FakeEvent(platform="qq-snowluma")
        out = _support.run(
            _support.call_llm_tool(self.plugin, event, "task.list")
        )
        self.assertIn("PLATFORM_DISABLED", out)

    def test_llm_tool_allowed_on_listed_platform(self):
        self.plugin.allowed_platforms = ["QQbot-test"]
        event = _support.FakeEvent(platform="QQbot-test")
        out = _support.run(_support.call_llm_tool(self.plugin, event, "task.list"))
        self.assertNotIn("PLATFORM_DISABLED", out)

