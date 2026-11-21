"""paadm_client 单测：假 paadm 脚本覆盖成功/失败/非法 JSON/超时/不存在/缓存。"""

from __future__ import annotations

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
import paadm_client  # noqa: E402
from interaction_store import InteractionStore  # noqa: E402


class ClientTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "paa.db")
        self.store = InteractionStore(
            os.path.join(self.tmp.name, "interactions.db"),
            stdout_limit=100,
        )

    def make_client(self, mode="success", **kwargs):
        script, calls = _support.write_fake_paadm(self.tmp.name, mode, name=f"fake-{mode}")
        client = paadm_client.PaadmClient(
            script, self.db_path, timeout_seconds=kwargs.pop("timeout_seconds", 5),
            store=kwargs.pop("store", self.store), **kwargs
        )
        return client, calls


class SuccessTest(ClientTestBase):
    def test_success_and_logged(self):
        client, calls = self.make_client()
        result = client.run(["version"], source="command", action="version")
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.data, {"name": "paadm", "version": "0.1.0"})
        self.assertEqual(result.error_code, None)
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertEqual(len(_support.read_calls(calls)), 1)
        self.assertEqual(self.store.count(), 1)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["action"], "version")
        self.assertEqual(row["source"], "command")
        self.assertEqual(row["success"], 1)
        self.assertEqual(row["exit_code"], 0)

    def test_db_path_passed_via_env(self):
        client, _ = self.make_client()
        result = client.run(["init"], source="startup", action="init")
        self.assertTrue(result.success)
        self.assertEqual(result.data["db_path"], self.db_path)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["source"], "startup")

    def test_user_id_injected_before_business_args(self):
        client, calls = self.make_client()
        result = client.run(
            ["task", "create", "--title", "写周报"],
            source="command",
            action="task.create",
            user_id="paa-user-1",
            platform="aiocqhttp",
            sender_id="123",
        )
        self.assertTrue(result.success)
        argv = _support.read_calls(calls)[0]
        self.assertEqual(
            argv,
            ["--json", "--user-id", "paa-user-1", "task", "create", "--title", "写周报"],
        )
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["paadm_user_id"], "paa-user-1")
        self.assertEqual(row["platform"], "aiocqhttp")
        self.assertEqual(row["sender_id"], "123")
        self.assertEqual(row["argv"][1:4], ["--json", "--user-id", "paa-user-1"])

    def test_args_are_stringified(self):
        client, calls = self.make_client()
        # 避免直接调用内部接口，只验证 list 型参数被 str 化
        result = client.run(["task", "list"], source="command", action="task.list")
        self.assertTrue(result.success)
        for token in _support.read_calls(calls)[0]:
            self.assertIsInstance(token, str)


class UserScopeTest(ClientTestBase):
    def test_rejects_user_id_argument_without_spawning(self):
        client, calls = self.make_client()
        result = client.run(
            ["task", "list", "--user-id", "other-user"],
            source="command",
            action="task.list",
            user_id="paa-user-1",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "USER_SCOPE_VIOLATION")
        self.assertEqual(_support.read_calls(calls), [])
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["action"], "task.list")
        self.assertEqual(row["error_code"], "USER_SCOPE_VIOLATION")

    def test_rejects_db_argument_equals_form(self):
        client, calls = self.make_client()
        result = client.run(
            ["task", "list", "--db=/tmp/other.db"],
            source="command",
            action="task.list",
            user_id="paa-user-1",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "USER_SCOPE_VIOLATION")
        self.assertEqual(_support.read_calls(calls), [])

    def test_allows_business_args(self):
        client, calls = self.make_client()
        result = client.run(
            ["task", "list", "--status", "pending"],
            source="command",
            action="task.list",
            user_id="paa-user-1",
        )
        self.assertTrue(result.success)
        self.assertEqual(
            _support.read_calls(calls)[0],
            ["--json", "--user-id", "paa-user-1", "task", "list", "--status", "pending"],
        )


class FailureTest(ClientTestBase):
    def test_business_failure_maps_error_code(self):
        client, _ = self.make_client("fail")
        result = client.run(["task", "get", "--id", "x"], source="command", action="task.get")
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.error_code, "TASK_NOT_FOUND")
        self.assertIn("task not found", result.error_message)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["success"], 0)
        self.assertEqual(row["error_code"], "TASK_NOT_FOUND")

    def test_invalid_json(self):
        client, _ = self.make_client("invalid")
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_INVALID_OUTPUT)
        self.assertIn("JSON", result.error_message)

    def test_timeout(self):
        client, _ = self.make_client("timeout", timeout_seconds=0.3)
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_TIMEOUT)
        self.assertTrue(result.timed_out)
        self.assertIn("超时", result.error_message)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["error_code"], paadm_client.ERROR_TIMEOUT)
        self.assertEqual(row["success"], 0)

    def test_missing_binary(self):
        client = paadm_client.PaadmClient(
            os.path.join(self.tmp.name, "no-such-paadm"),
            self.db_path,
            timeout_seconds=2,
            store=self.store,
        )
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_NOT_FOUND)
        row = self.store.fetch_recent(1)[0]
        self.assertEqual(row["error_code"], paadm_client.ERROR_NOT_FOUND)
        self.assertIsNone(row["exit_code"])

    @unittest.skipIf(os.geteuid() == 0, "root 无视文件权限")
    def test_permission_denied_is_spawn_error(self):
        script, _ = _support.write_fake_paadm(self.tmp.name, "success", name="noexec")
        os.chmod(script, 0o000)
        self.addCleanup(lambda: os.chmod(script, 0o755))
        client = paadm_client.PaadmClient(script, self.db_path, timeout_seconds=2, store=self.store)
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_SPAWN)

    def test_invalid_output_logs_stdout(self):
        client, _ = self.make_client("invalid")
        client.run(["version"], source="command", action="version")
        row = self.store.fetch_recent(1)[0]
        self.assertIn("not a json", row["stdout"])


class ResolveCacheTest(ClientTestBase):
    def test_resolve_cached_and_normalized(self):
        client, calls = self.make_client()
        first = client.resolve_user(
            " AIOCQHTTP ", "123", source="command", platform="aiocqhttp", sender_id="123"
        )
        self.assertTrue(first.ok)
        self.assertEqual(first.user_id, "paa-user-1")
        self.assertFalse(first.from_cache)
        self.assertEqual(first.provider, "aiocqhttp")
        self.assertEqual(self.store.count(), 1)

        second = client.resolve_user(
            "aiocqhttp", "123", source="command", platform="aiocqhttp", sender_id="123"
        )
        self.assertTrue(second.ok)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.user_id, "paa-user-1")
        # 只真正调用过一次 paadm
        self.assertEqual(len(_support.read_calls(calls)), 1)
        self.assertEqual(self.store.count(), 1)
        # 请求参数正确
        argv = _support.read_calls(calls)[0]
        self.assertEqual(
            argv,
            ["--json", "user", "resolve", "--provider", "aiocqhttp",
             "--external-user-id", "123"],
        )

    def test_resolve_requires_identity(self):
        client, calls = self.make_client()
        result = client.resolve_user("", "123")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_ARGUMENT")
        self.assertEqual(_support.read_calls(calls), [])

    def test_resolve_failure_propagates_code(self):
        client, _ = self.make_client("fail")
        result = client.resolve_user("qq", "123", source="command")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TASK_NOT_FOUND")
        self.assertIsNotNone(result.result)

    def test_clear_cache(self):
        client, calls = self.make_client()
        client.resolve_user("qq", "1")
        client.clear_user_cache()
        client.resolve_user("qq", "1")
        self.assertEqual(len(_support.read_calls(calls)), 2)


class ParseResultEdgeTest(ClientTestBase):
    def test_success_true_with_nonzero_exit_is_invalid(self):
        script = os.path.join(self.tmp.name, "weird.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "print(json.dumps({'success': True, 'data': {}, 'error': None}))\n"
                "sys.exit(1)\n"
            )
        os.chmod(script, 0o755)
        client = paadm_client.PaadmClient(script, self.db_path, timeout_seconds=2, store=self.store)
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_INVALID_OUTPUT)

    def test_missing_success_field(self):
        script = os.path.join(self.tmp.name, "nosuccess.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/usr/bin/env python3\n"
                "print('{\"data\": {}}')\n"
            )
        os.chmod(script, 0o755)
        client = paadm_client.PaadmClient(script, self.db_path, timeout_seconds=2, store=self.store)
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_INVALID_OUTPUT)

    def test_empty_stdout(self):
        script = os.path.join(self.tmp.name, "empty.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        os.chmod(script, 0o755)
        client = paadm_client.PaadmClient(script, self.db_path, timeout_seconds=2, store=self.store)
        result = client.run(["version"], source="command", action="version")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, paadm_client.ERROR_INVALID_OUTPUT)
        self.assertIn("没有输出", result.error_message)


if __name__ == "__main__":
    unittest.main()
