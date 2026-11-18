"""interaction_store 单测：建表列名/索引、写入、截断、读取、失败降级。"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import interaction_store  # noqa: E402


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "interactions.db")
        self.store = interaction_store.InteractionStore(self.db_path, stdout_limit=50)


class SchemaTest(StoreTestBase):
    def test_table_columns_exact(self):
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute("PRAGMA table_info(interactions)").fetchall()
        finally:
            conn.close()
        self.assertEqual([row[1] for row in rows], list(interaction_store.INTERACTION_COLUMNS))
        # 关键列类型
        types = {row[1]: row[2] for row in rows}
        self.assertEqual(types["success"], "INTEGER")
        self.assertEqual(types["ts"], "TEXT")
        self.assertEqual(types["action"], "TEXT")

    def test_indexes_exist(self):
        conn = sqlite3.connect(self.db_path)
        try:
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(interactions)").fetchall()
            }
        finally:
            conn.close()
        self.assertIn("idx_interactions_ts", indexes)
        self.assertIn("idx_interactions_action", indexes)

    def test_wal_enabled(self):
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_nested_directory_created(self):
        nested = os.path.join(self.tmp.name, "a", "b", "interactions.db")
        store = interaction_store.InteractionStore(nested)
        self.assertTrue(store.enabled)
        self.assertTrue(os.path.exists(nested))


class RecordTest(StoreTestBase):
    def test_record_and_fetch(self):
        ok = self.store.record(
            source="command",
            action="task.create",
            success=1,
            platform="aiocqhttp",
            sender_id="123",
            paadm_user_id="u1",
            argv=["--json", "task", "create", "--title", "写周报"],
            exit_code=0,
            duration_ms=42,
            stdout='{"success": true}',
            stderr="",
        )
        self.assertTrue(ok)
        self.assertEqual(self.store.count(), 1)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["action"], "task.create")
        self.assertEqual(row["source"], "command")
        self.assertEqual(row["platform"], "aiocqhttp")
        self.assertEqual(row["sender_id"], "123")
        self.assertEqual(row["paadm_user_id"], "u1")
        self.assertEqual(row["success"], 1)
        self.assertEqual(row["exit_code"], 0)
        self.assertEqual(row["duration_ms"], 42)
        self.assertEqual(row["argv"][0], "--json")
        self.assertTrue(row["ts"].endswith("+08:00"), row["ts"])
        self.assertIsNotNone(
            re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", row["ts"])
        )

    def test_failure_row(self):
        self.store.record(
            source="llm_tool",
            action="task.list",
            success=0,
            error_code="TIMEOUT",
            exit_code=None,
            stdout="partial",
            stderr="stderr text",
        )
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["success"], 0)
        self.assertEqual(row["error_code"], "TIMEOUT")
        self.assertIsNone(row["exit_code"])

    def test_truncation(self):
        long_text = "x" * 500
        self.store.record(
            source="command",
            action="raw",
            success=1,
            stdout=long_text,
            stderr=long_text,
        )
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(len(row["stdout"]), 50)
        self.assertEqual(len(row["stderr"]), 50)

        store = interaction_store.InteractionStore(
            os.path.join(self.tmp.name, "other.db"),
            stdout_limit=10,
            stderr_limit=5,
        )
        store.record(source="startup", action="version", success=1, stdout=long_text, stderr=long_text)
        row = store.fetch_recent(1)[0]
        self.assertEqual(len(row["stdout"]), 10)
        self.assertEqual(len(row["stderr"]), 5)

    def test_order_is_desc(self):
        for index in range(3):
            self.store.record(
                source="command", action=f"task.list{index}", success=1
            )
        rows = self.store.fetch_recent(10)
        self.assertEqual([row["action"] for row in rows], ["task.list2", "task.list1", "task.list0"])

    def test_argv_roundtrip(self):
        self.store.record(
            source="command",
            action="transaction.create",
            success=1,
            argv=["--json", "--user-id", "u1", "transaction", "create"],
        )
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(
            row["argv"],
            ["--json", "--user-id", "u1", "transaction", "create"],
        )


class DisabledTest(unittest.TestCase):
    def test_empty_path_disables_but_does_not_raise(self):
        store = interaction_store.InteractionStore("")
        self.assertFalse(store.enabled)
        self.assertFalse(store.record(source="startup", action="version", success=1))
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.fetch_recent(5), [])

    def test_unwritable_path_disables(self):
        store = interaction_store.InteractionStore("/proc/definitely-not-writable/paa/interactions.db")
        self.assertFalse(store.enabled)
        self.assertFalse(store.record(source="startup", action="version", success=1))


if __name__ == "__main__":
    unittest.main()
