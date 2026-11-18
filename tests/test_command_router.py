"""command_router 单测：参数解析、金额换算、必填校验、argv 构造、格式化。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import command_router as router  # noqa: E402


class FakeResult:
    def __init__(self, success=True, data=None, error_code=None, error_message=""):
        self.success = success
        self.data = data
        self.error_code = error_code
        self.error_message = error_message


class AmountTest(unittest.TestCase):
    def test_yuan_to_cents(self):
        self.assertEqual(router.parse_amount_yuan("18.50"), 1850)
        self.assertEqual(router.parse_amount_yuan("18.5"), 1850)
        self.assertEqual(router.parse_amount_yuan("18"), 1800)
        self.assertEqual(router.parse_amount_yuan("0.01"), 1)
        self.assertEqual(router.parse_amount_yuan(" 100.00 "), 10000)

    def test_invalid_amounts(self):
        for value in ("0", "-1", "abc", "", "18.505", "1e-3"):
            with self.assertRaises(router.CommandError, msg=value):
                router.parse_amount_yuan(value)

    def test_format_cents(self):
        self.assertEqual(router.format_cents(1850), "18.50 元")
        self.assertEqual(router.format_cents(-850), "-8.50 元")
        self.assertEqual(router.format_cents(5), "0.05 元")


class IsoTimeTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            router.parse_iso("2026-11-17T23:59:00+08:00", "--due"),
            "2026-11-17T23:59:00+08:00",
        )
        self.assertEqual(
            router.parse_iso("2026-11-17T15:59:00Z", "--due"),
            "2026-11-17T15:59:00Z",
        )

    def test_invalid(self):
        for value in ("2026-11-17", "2026-11-17T23:59:00", "not-a-time", ""):
            with self.assertRaises(router.CommandError, msg=value):
                router.parse_iso(value, "--due")


class ParseCommandTest(unittest.TestCase):
    def test_empty_and_help(self):
        self.assertEqual(router.parse_command([]).kind, "help")
        self.assertEqual(router.parse_command(["help"]).kind, "help")
        self.assertEqual(router.parse_command(["paa", "help"]).kind, "help")

    def test_task_add(self):
        plan = router.parse_command(
            ["task", "add", "写", "周报", "--due", "2026-11-20T18:00:00+08:00",
             "--priority", "2", "--desc", "周五交"]
        )
        self.assertEqual(plan.action, "task.create")
        self.assertEqual(
            plan.argv,
            [
                "task", "create", "--title", "写 周报",
                "--due-at", "2026-11-20T18:00:00+08:00",
                "--priority", "2", "--description", "周五交",
            ],
        )

    def test_task_add_multiword_desc(self):
        """回归：--desc 的多词取值一直取到下一个 -- 参数为止。"""
        plan = router.parse_command(
            ["task", "add", "完成", "验收", "--due", "2026-11-20T18:00:00+08:00",
             "--desc", "在", "AstrBot", "中", "通过", "paadm", "创建"]
        )
        self.assertEqual(plan.action, "task.create")
        self.assertEqual(
            plan.argv,
            [
                "task", "create", "--title", "完成 验收",
                "--due-at", "2026-11-20T18:00:00+08:00",
                "--description", "在 AstrBot 中 通过 paadm 创建",
            ],
        )

    def test_tx_add_multiword_desc_before_next_flag(self):
        """--desc 多词取值不能吞掉后面的 --at。"""
        plan = router.parse_command(
            ["tx", "add", "--type", "expense", "--amount", "18.50",
             "--category", "food", "--desc", "午饭", "和", "咖啡",
             "--at", "2026-11-18T12:00:00+08:00"]
        )
        self.assertEqual(plan.action, "transaction.create")
        self.assertIn("--description", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("--description") + 1], "午饭 和 咖啡")
        self.assertIn("--occurred-at", plan.argv)

    def test_task_add_equals_form(self):
        plan = router.parse_command(
            ["task", "add", "买菜", "--due=2026-11-20T18:00:00+08:00"]
        )
        self.assertEqual(plan.argv[-2:], ["--due-at", "2026-11-20T18:00:00+08:00"])

    def test_task_add_requires_title(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.parse_command(["task", "add", "--priority", "2"])
        self.assertIn("标题", ctx.exception.message)

    def test_unknown_flag(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.parse_command(["task", "add", "x", "--nope", "1"])
        self.assertIn("--nope", ctx.exception.message)

    def test_missing_flag_value(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.parse_command(["task", "add", "x", "--due"])
        self.assertIn("缺少取值", ctx.exception.message)

    def test_task_list(self):
        plan = router.parse_command(["task", "list", "--status", "pending", "--limit", "5"])
        self.assertEqual(plan.action, "task.list")
        self.assertEqual(plan.argv, ["task", "list", "--status", "pending"])
        self.assertEqual(plan.limit, 5)

    def test_task_list_invalid_status(self):
        with self.assertRaises(router.CommandError):
            router.parse_command(["task", "list", "--status", "doing"])

    def test_task_done_and_del(self):
        self.assertEqual(
            router.parse_command(["task", "done", "abc"]).argv,
            ["task", "complete", "--id", "abc"],
        )
        self.assertEqual(
            router.parse_command(["task", "del", "abc"]).argv,
            ["task", "delete", "--id", "abc"],
        )
        with self.assertRaises(router.CommandError):
            router.parse_command(["task", "done"])

    def test_schedule_add(self):
        plan = router.parse_command(
            [
                "schedule", "add", "组会",
                "--start", "2026-11-20T10:00:00+08:00",
                "--end", "2026-11-20T11:00:00+08:00",
                "--location", "实验室", "--all-day", "0",
            ]
        )
        self.assertEqual(plan.action, "schedule.create")
        self.assertEqual(
            plan.argv,
            [
                "schedule", "create", "--title", "组会",
                "--start-at", "2026-11-20T10:00:00+08:00",
                "--end-at", "2026-11-20T11:00:00+08:00",
                "--location", "实验室", "--all-day", "0",
            ],
        )

    def test_schedule_add_requires_start_end(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.parse_command(["schedule", "add", "组会"])
        self.assertIn("--start", ctx.exception.message)
        with self.assertRaises(router.CommandError) as ctx:
            router.parse_command(
                ["schedule", "add", "组会", "--start", "2026-11-20T10:00:00+08:00"]
            )
        self.assertIn("--end", ctx.exception.message)

    def test_reminder_add(self):
        plan = router.parse_command(
            ["reminder", "add", "交作业", "--at", "2026-11-17T20:00:00+08:00", "--item", "item-1"]
        )
        self.assertEqual(
            plan.argv,
            [
                "reminder", "create", "--content", "交作业",
                "--remind-at", "2026-11-17T20:00:00+08:00",
                "--item-id", "item-1",
            ],
        )

    def test_tx_add(self):
        plan = router.parse_command(
            [
                "tx", "add", "--type", "expense", "--amount", "18.50",
                "--category", "food", "--desc", "午饭",
                "--at", "2026-11-17T12:00:00+08:00",
            ]
        )
        self.assertEqual(plan.action, "transaction.create")
        self.assertEqual(
            plan.argv,
            [
                "transaction", "create", "--type", "expense",
                "--amount", "1850", "--category", "food",
                "--occurred-at", "2026-11-17T12:00:00+08:00",
                "--description", "午饭",
            ],
        )

    def test_tx_add_defaults_occurred_at_to_now(self):
        plan = router.parse_command(
            ["tx", "add", "--type", "支出", "--amount", "1", "--category", "food"]
        )
        self.assertIn("--occurred-at", plan.argv)
        occurred = plan.argv[plan.argv.index("--occurred-at") + 1]
        self.assertTrue(occurred.endswith("+08:00"), occurred)

    def test_tx_add_requires_fields(self):
        for tokens in (
            ["tx", "add", "--amount", "1", "--category", "food"],
            ["tx", "add", "--type", "expense", "--category", "food"],
            ["tx", "add", "--type", "expense", "--amount", "1"],
        ):
            with self.assertRaises(router.CommandError):
                router.parse_command(tokens)

    def test_tx_sum_and_list(self):
        self.assertEqual(
            router.parse_command(["tx", "sum", "--from", "2026-11-01T00:00:00+08:00"]).argv,
            ["transaction", "summary", "--from", "2026-11-01T00:00:00+08:00"],
        )
        self.assertEqual(
            router.parse_command(["tx", "list"]).argv,
            ["transaction", "list"],
        )

    def test_note_commands(self):
        plan = router.parse_command(["note", "add", "记一个", "点子", "--title", "想法"])
        self.assertEqual(
            plan.argv,
            ["note", "create", "--content", "记一个 点子", "--title", "想法"],
        )
        plan = router.parse_command(["note", "list", "--q", "点子", "--limit", "3"])
        self.assertEqual(plan.argv, ["note", "list", "--keyword", "点子"])
        self.assertEqual(plan.limit, 3)
        self.assertEqual(
            router.parse_command(["note", "get", "note-1"]).argv,
            ["note", "get", "--id", "note-1"],
        )

    def test_user_and_raw(self):
        self.assertEqual(router.parse_command(["user"]).action, "user.resolve")
        plan = router.parse_command(["raw", "task", "list", "--status", "pending"])
        self.assertEqual(plan.action, "raw")
        self.assertEqual(plan.argv, ["task", "list", "--status", "pending"])
        with self.assertRaises(router.CommandError):
            router.parse_command(["raw"])

    def test_unknown_command(self):
        with self.assertRaises(router.CommandError):
            router.parse_command(["nope"])


class BuildActionTest(unittest.TestCase):
    def test_task_create(self):
        plan = router.build_action(
            "task.create", {"title": "写周报", "due_at": "2026-11-20T18:00:00+08:00", "priority": 3}
        )
        self.assertEqual(
            plan.argv,
            [
                "task", "create", "--title", "写周报",
                "--priority", "3", "--due-at", "2026-11-20T18:00:00+08:00",
            ],
        )

    def test_unknown_action(self):
        with self.assertRaises(router.CommandError):
            router.build_action("task.unknown", {})

    def test_unknown_field(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.build_action("task.create", {"title": "x", "bad": 1})
        self.assertIn("bad", ctx.exception.message)

    def test_missing_required(self):
        with self.assertRaises(router.CommandError) as ctx:
            router.build_action("task.create", {})
        self.assertIn("title", str(ctx.exception))

    def test_amount_is_cents_for_llm(self):
        plan = router.build_action(
            "transaction.create",
            {"type": "expense", "amount": 1850, "category": "food",
             "occurred_at": "2026-11-17T12:00:00+08:00"},
        )
        self.assertIn("--amount", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("--amount") + 1], "1850")
        with self.assertRaises(router.CommandError):
            router.build_action(
                "transaction.create", {"type": "expense", "amount": 18.5}
            )

    def test_transaction_create_defaults_time(self):
        plan = router.build_action(
            "transaction.create", {"type": "expense", "amount": 100}
        )
        occurred = plan.argv[plan.argv.index("--occurred-at") + 1]
        self.assertTrue(occurred.endswith("+08:00"))

    def test_summary(self):
        plan = router.build_action(
            "transaction.summary",
            {"from": "2026-11-01T00:00:00+08:00", "to": "2026-12-01T00:00:00+08:00"},
        )
        self.assertEqual(
            plan.argv,
            [
                "transaction", "summary",
                "--from", "2026-11-01T00:00:00+08:00",
                "--to", "2026-12-01T00:00:00+08:00",
            ],
        )

    def test_note_create(self):
        plan = router.build_action("note.create", {"content": "记一下"})
        self.assertEqual(plan.argv, ["note", "create", "--content", "记一下"])

    def test_schedule_and_reminder(self):
        plan = router.build_action(
            "schedule.create",
            {"title": "组会", "start_at": "2026-11-20T10:00:00+08:00", "all_day": 1},
        )
        self.assertEqual(
            plan.argv,
            [
                "schedule", "create", "--title", "组会",
                "--start-at", "2026-11-20T10:00:00+08:00",
                "--all-day", "1",
            ],
        )
        plan = router.build_action(
            "reminder.create",
            {"content": "喝水", "remind_at": "2026-11-20T09:00:00+08:00"},
        )
        self.assertEqual(
            plan.argv,
            [
                "reminder", "create", "--content", "喝水",
                "--remind-at", "2026-11-20T09:00:00+08:00",
            ],
        )


class FormattingTest(unittest.TestCase):
    def test_format_error_includes_code_and_hint(self):
        text = router.format_error("TASK_NOT_FOUND", "missing")
        self.assertIn("TASK_NOT_FOUND", text)
        self.assertIn("missing", text)
        self.assertIn("提示", text)

    def test_format_item_list_limits_rows(self):
        plan = router.CommandPlan(
            action="task.list", argv=[], kind="item_list", verb="待办"
        )
        data = {
            "items": [
                {"id": f"id-{i}", "title": f"任务{i}", "status": "pending"} for i in range(30)
            ],
            "count": 30,
        }
        text = router.format_reply(plan, FakeResult(data=data), reply_max_rows=20)
        self.assertIn("共 30 条待办", text)
        self.assertEqual(text.count("\n- "), 20)  # header 之后最多 reply_max_rows 行
        self.assertIn("仅显示前 20 条", text)

    def test_format_reply_truncates_total_length(self):
        plan = router.CommandPlan(action="note.get", argv=[], kind="note_get")
        text = router.format_reply(
            plan, FakeResult(data={"id": "n1", "title": "t", "content": "x" * 5000})
        )
        self.assertLessEqual(len(text), router.MAX_REPLY_CHARS)
        self.assertTrue(text.endswith("…"))

    def test_format_failure(self):
        plan = router.CommandPlan(action="task.list", argv=[], kind="item_list")
        text = router.format_reply(
            plan, FakeResult(success=False, error_code="TASK_NOT_FOUND", error_message="no")
        )
        self.assertIn("操作失败", text)
        self.assertIn("TASK_NOT_FOUND", text)

    def test_tool_result_json(self):
        text = router.tool_result_json(FakeResult(data={"id": "x"}))
        payload = json.loads(text)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"], {"id": "x"})
        self.assertIsNone(payload["error"])

        text = router.tool_result_json(
            FakeResult(success=False, error_code="TASK_NOT_FOUND", error_message="missing")
        )
        payload = json.loads(text)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "TASK_NOT_FOUND")

    def test_tool_error_json(self):
        payload = json.loads(router.tool_error_json("INVALID_ARGUMENT", "bad"))
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"]["code"], "INVALID_ARGUMENT")

    def test_format_user(self):
        resolution = router.UserResolution(
            ok=True,
            user_id="u1",
            provider="qq",
            external_user_id="123",
            result=FakeResult(data={"id": "u1", "provider": "qq", "external_user_id": "123"}),
        )
        text = router.format_user(resolution)
        self.assertIn("u1", text)
        self.assertIn("qq", text)
        self.assertIn("123", text)


if __name__ == "__main__":
    unittest.main()
