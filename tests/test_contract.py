"""交付契约测试：metadata.yaml / _conf_schema.json / main.py 关键约定 / 仅 stdlib。"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import _support  # noqa: E402

MODULE = _support.load_plugin()

REQUIRED_SCHEMA_KEYS = (
    "paadm_bin",
    "paadm_db",
    "interactions_db",
    "timeout_seconds",
    "reply_max_rows",
    "log_stdout_chars",
    "provider_override",
)

ALLOWED_CONFIG_TYPES = {"string", "int", "bool", "float", "text"}

# 插件源文件允许的顶层 import 根（stdlib + astrbot + 本地模块）
ALLOWED_IMPORT_ROOTS = {
    "astrbot",
    "__future__",
    "asyncio",
    "dataclasses",
    "datetime",
    "decimal",
    "importlib",
    "json",
    "os",
    "shlex",
    "sqlite3",
    "subprocess",
    "sys",
    "tempfile",
    "threading",
    "time",
    "types",
    "typing",
    "unittest",
    "pathlib",
    "re",
    "command_router",
    "interaction_store",
    "paadm_client",
    "_support",
}


def _parse_simple_yaml(text: str) -> dict:
    """只解析 metadata.yaml 的「key: value」行（足够契约检查，避免依赖 PyYAML）。"""
    data = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        data[key.strip()] = value
    return data


class MetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
        cls.metadata = _parse_simple_yaml(cls.raw)

    def test_required_fields(self):
        for field in ("name", "desc", "version", "author"):
            self.assertIn(field, self.metadata)
            self.assertTrue(self.metadata[field].strip(), field)

    def test_name_matches_directory_and_importable(self):
        self.assertEqual(self.metadata["name"], PLUGIN_DIR.name)
        self.assertTrue(self.metadata["name"].isidentifier())

    def test_version_matches_main(self):
        self.assertEqual(self.metadata["version"], MODULE.PLUGIN_VERSION)
        self.assertEqual(self.metadata["name"], MODULE.PLUGIN_NAME)

    def test_astrbot_version_specifier(self):
        self.assertEqual(self.metadata["astrbot_version"], ">=4.29,<5")

    def test_display_fields_present(self):
        self.assertTrue(self.metadata.get("display_name", "").strip())
        self.assertTrue(self.metadata.get("short_desc", "").strip())


class SchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        )

    def test_keys_exact(self):
        self.assertEqual(set(self.schema), set(REQUIRED_SCHEMA_KEYS))
        self.assertEqual(tuple(self.schema.keys()), REQUIRED_SCHEMA_KEYS)

    def test_types_and_descriptions(self):
        for key, item in self.schema.items():
            self.assertIn(item.get("type"), ALLOWED_CONFIG_TYPES, key)
            self.assertTrue(str(item.get("description", "")).strip(), key)
            self.assertIn("default", item, key)

    def test_defaults_match_main(self):
        for key, item in self.schema.items():
            self.assertEqual(item["default"], MODULE.DEFAULT_CONFIG[key], key)

    def test_deployment_defaults(self):
        self.assertEqual(self.schema["paadm_bin"]["default"], "/AstrBot/data/paa/bin/paadm")
        self.assertEqual(self.schema["paadm_db"]["default"], "/AstrBot/data/paa/paa.db")
        self.assertEqual(
            self.schema["interactions_db"]["default"], "/AstrBot/data/paa/interactions.db"
        )
        self.assertEqual(self.schema["timeout_seconds"]["default"], 15)
        self.assertEqual(self.schema["reply_max_rows"]["default"], 20)
        self.assertEqual(self.schema["log_stdout_chars"]["default"], 4000)
        self.assertEqual(self.schema["provider_override"]["default"], "")


class MainContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")

    def test_source_compiles(self):
        compile(self.source, "main.py", "exec")

    def test_astrbot_imports(self):
        self.assertIn("from astrbot.api import logger", self.source)
        self.assertIn("from astrbot.api.event import AstrMessageEvent, filter", self.source)
        self.assertIn("from astrbot.api.star import Context, Star, register", self.source)

    def test_register_decorator(self):
        self.assertIn('@register(PLUGIN_NAME, "Huy4z", PLUGIN_DESC, PLUGIN_VERSION)', self.source)

    def test_handlers_declared_in_main(self):
        self.assertIn('@filter.command("paa")', self.source)
        self.assertIn('@filter.llm_tool(name="paadm")', self.source)
        self.assertIn("@filter.on_astrbot_loaded()", self.source)
        self.assertIn("async def paa_cmd", self.source)
        self.assertIn("async def paadm_tool", self.source)
        self.assertIn("async def initialize", self.source)
        self.assertIn("async def on_astrbot_loaded", self.source)

    def test_llm_tool_docstring_has_args(self):
        method = MODULE.PaadmPlugin.paadm_tool
        doc = method.__doc__ or ""
        self.assertIn("Args:", doc)
        self.assertIn("action(string)", doc)
        self.assertIn("args_json(string)", doc)

    def test_llm_tool_registered_from_main_module(self):
        registered = [
            (name, func, module)
            for name, func, module in _support.LLM_TOOL_REGISTRY
            if name == "paadm"
        ]
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0][2], MODULE.__name__)

    def test_paa_command_args_is_bare_greedystr(self):
        """回归：/paa 的 args 必须是无默认值的 GreedyStr。

        AstrBot 4.29.4 的 CommandFilter 对带默认值的参数只记录默认值、
        丢掉 GreedyStr 标记，导致 /paa task add x 只收到 "task"。
        这里通过解析源码里 paa_cmd 的签名做静态回归。
        """
        tree = ast.parse(self.source, filename=str(PLUGIN_DIR / "main.py"))
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "paa_cmd":
                params = node.args.args
                self.assertGreaterEqual(len(params), 3)
                args_param = params[-1]
                self.assertEqual(args_param.arg, "args")
                self.assertEqual(len(node.args.defaults), 0,
                                 "paa_cmd 的 GreedyStr 参数不能有默认值")
                found = True
        self.assertTrue(found, "未找到 paa_cmd 定义")

    def test_startup_check_calls_version_and_init(self):
        self.assertIn('["version"]', self.source)
        self.assertIn('["init"]', self.source)
        self.assertIn('source="startup"', self.source)


class DependencyTest(unittest.TestCase):
    def test_only_stdlib_astrbot_and_local_imports(self):
        """插件源文件（.py）只能 import 标准库 / astrbot / 本地模块。"""
        problems = []
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root not in ALLOWED_IMPORT_ROOTS:
                            problems.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue  # 相对导入（包内模块）
                    root = (node.module or "").split(".")[0]
                    if root not in ALLOWED_IMPORT_ROOTS:
                        problems.append(f"{path.name}: from {node.module} import ...")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_requirements_txt_has_no_dependencies(self):
        text = (PLUGIN_DIR / "requirements.txt").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.fail(f"requirements.txt 不应包含依赖：{stripped}")
        self.assertIn("无第三方依赖", text)


if __name__ == "__main__":
    unittest.main()
