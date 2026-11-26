"""插件单元测试的离线支撑。

硬约束：测试不 import 真正的 astrbot（本机没装）。在导入 main.py 之前把最小
stub 装进 ``sys.modules``，再按文件路径加载 main.py；main.py 的 ``from . import``
会失败并走绝对导入兜底（tests 已把插件目录加入 sys.path）。

stub 形态依据 AstrBot 4.29.4 容器源码核对结果（与 reference 插件一致）：
- ``astrbot.api.logger``；
- ``astrbot.api.event.AstrMessageEvent`` + ``astrbot.api.event.filter``
  （command / llm_tool / on_astrbot_loaded 等在测试里是透传装饰器）；
- ``astrbot.api.star.Context / Star / register``；
- ``astrbot.core.star.filter.command.GreedyStr``（命令剩余文本类型）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PLUGIN_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# ---------------------------------------------------------------------------
# astrbot stub
# ---------------------------------------------------------------------------


class FakeLogger:
    """记录日志的 logger 替身（插件只调用 debug/info/warning/error/exception）。"""

    def __init__(self) -> None:
        self.records: list = []

    def _record(self, level: str):
        def handler(*args, **kwargs) -> None:
            self.records.append((level, args))

        return handler

    debug = property(lambda self: self._record("debug"))
    info = property(lambda self: self._record("info"))
    warning = property(lambda self: self._record("warning"))
    error = property(lambda self: self._record("error"))
    exception = property(lambda self: self._record("exception"))


class _PassthroughDecorator:
    """透传装饰器（测试里不模拟 AstrBot 的 handler 注册副作用）。"""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs

    def __call__(self, func):
        return func


LLM_TOOL_REGISTRY: list = []


def _llm_tool_decorator(*args, **kwargs):
    def decorator(func):
        name = kwargs.get("name") or func.__name__
        func.__llm_tool_name__ = name
        LLM_TOOL_REGISTRY.append((name, func.__name__, func.__module__))
        return func

    return decorator


def _build_astrbot_stub() -> dict:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = FakeLogger()

    event_module = types.ModuleType("astrbot.api.event")
    filter_module = types.ModuleType("astrbot.api.event.filter")

    class EventMessageType:
        GROUP_MESSAGE = "GROUP_MESSAGE"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
        OTHER_MESSAGE = "OTHER_MESSAGE"
        ALL = "ALL"

    class PlatformAdapterType:
        AIOCQHTTP = "AIOCQHTTP"
        ALL = "ALL"

    filter_module.EventMessageType = EventMessageType
    filter_module.PlatformAdapterType = PlatformAdapterType
    filter_module.command = _PassthroughDecorator
    filter_module.event_message_type = _PassthroughDecorator
    filter_module.platform_adapter_type = _PassthroughDecorator
    filter_module.llm_tool = _llm_tool_decorator
    filter_module.on_llm_request = _PassthroughDecorator
    filter_module.on_llm_response = _PassthroughDecorator
    filter_module.on_astrbot_loaded = _PassthroughDecorator

    class AstrMessageEvent:  # noqa: D401 - 仅用于 import 兼容
        pass

    event_module.filter = filter_module
    event_module.AstrMessageEvent = AstrMessageEvent

    star_module = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None, config=None) -> None:
            self.context = context
            self.config = config

    def register(*args, **kwargs):
        def decorator(cls):
            return cls

        return decorator

    star_module.Context = Context
    star_module.Star = Star
    star_module.register = register

    # astrbot.core.star.filter.command.GreedyStr（命令参数类型）
    core_module = types.ModuleType("astrbot.core")
    core_star_module = types.ModuleType("astrbot.core.star")
    core_filter_module = types.ModuleType("astrbot.core.star.filter")
    command_module = types.ModuleType("astrbot.core.star.filter.command")

    class GreedyStr(str):
        """AstrBot 中代表「命令名之后的剩余全部文本」的类型。"""

    command_module.GreedyStr = GreedyStr
    core_filter_module.command = command_module
    core_star_module.filter = core_filter_module
    core_module.star = core_star_module

    api.event = event_module
    api.star = star_module
    astrbot.api = api
    astrbot.core = core_module

    return {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_module,
        "astrbot.api.event.filter": filter_module,
        "astrbot.api.star": star_module,
        "astrbot.core": core_module,
        "astrbot.core.star": core_star_module,
        "astrbot.core.star.filter": core_filter_module,
        "astrbot.core.star.filter.command": command_module,
    }


def install_stubs() -> None:
    """安装 astrbot stub（幂等）。"""
    if "astrbot.api" not in sys.modules:
        sys.modules.update(_build_astrbot_stub())


_MODULE = None


def load_plugin():
    """按文件路径加载 main.py，返回模块（缓存）。"""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    install_stubs()
    spec = importlib.util.spec_from_file_location("paa_plugin_main", PLUGIN_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["paa_plugin_main"] = module
    spec.loader.exec_module(module)
    _MODULE = module
    return module


# ---------------------------------------------------------------------------
# 假 paadm 脚本
# ---------------------------------------------------------------------------

_FAKE_PAADM_TEMPLATE = '''#!/usr/bin/env python3
"""假 paadm：输出与真实 paadm 一致的 JSON 契约，供插件单测使用。"""

import json
import os
import sys
import time

MODE = {mode!r}
CALLS = {calls!r}

if CALLS:
    with open(CALLS, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")

if MODE == "timeout":
    time.sleep(5.0)
    sys.exit(0)

if MODE == "invalid":
    print("this is not a json envelope")
    sys.exit(1)

if MODE == "fail":
    print(json.dumps({{
        "success": False,
        "data": None,
        "error": {{"code": "TASK_NOT_FOUND", "message": "task not found"}},
    }}, ensure_ascii=False))
    sys.exit(1)

if MODE == "fail_after_resolve" and "resolve" not in sys.argv[1:]:
    print(json.dumps({{
        "success": False,
        "data": None,
        "error": {{"code": "INVALID_ARGUMENT", "message": "bad arguments"}},
    }}, ensure_ascii=False))
    sys.exit(2)

args = sys.argv[1:]


def value_of(flag, default=None):
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def base_item():
    return {{
        "id": value_of("--id", "item-1"),
        "title": value_of("--title", "示例待办"),
        "status": "pending",
        "priority": 2,
        "due_at": value_of("--due-at"),
    }}


if "version" in args:
    data = {{"name": "paadm", "version": "0.1.0"}}
elif "init" in args:
    data = {{"db_path": os.environ.get("PAADM_DB_PATH", ""), "schema_version": 1}}
elif "resolve" in args:
    data = {{
        "id": "paa-user-1",
        "provider": value_of("--provider", "qq"),
        "external_user_id": value_of("--external-user-id", ""),
        "created": False,
    }}
elif "summary" in args:
    data = {{
        "income_total": 1000,
        "expense_total": 1850,
        "net_total": -850,
        "by_category": [{{"category": "food", "income": 0, "expense": 1850}}],
    }}
elif "list" in args:
    data = {{
        "items": [
            {{
                "id": "item-1",
                "type": "task",
                "status": "pending",
                "title": "示例待办",
                "priority": 2,
                "due_at": "2026-11-20T10:00:00+08:00",
                "content": "示例笔记内容",
            }},
            {{
                "id": "item-2",
                "type": "task",
                "status": "completed",
                "title": "第二件事",
                "priority": None,
                "content": "第二条内容",
            }},
        ],
        "count": 2,
    }}
elif "delete" in args:
    data = {{"id": value_of("--id", "item-1"), "deleted": True}}
elif "complete" in args or "cancel" in args:
    data = {{"id": value_of("--id", "item-1"), "title": "示例待办", "status": "completed"}}
elif "get" in args:
    if "note" in args:
        data = {{
            "id": value_of("--id", "note-1"),
            "title": "标题",
            "content": "正文内容",
        }}
    elif "reminder" in args:
        data = {{
            "id": value_of("--id", "rem-1"),
            "content": "喝水",
            "remind_at": "2026-11-20T09:00:00+08:00",
            "status": "pending",
        }}
    else:
        data = base_item()
elif "create" in args:
    if "transaction" in args:
        data = {{
            "id": "tx-1",
            "type": value_of("--type", "expense"),
            "amount": int(value_of("--amount", "0") or 0),
            "category": value_of("--category", ""),
            "description": value_of("--description", ""),
            "occurred_at": value_of("--occurred-at", "2026-11-18T12:00:00+08:00"),
        }}
    elif "note" in args:
        data = {{
            "id": "note-1",
            "title": value_of("--title", ""),
            "content": value_of("--content", ""),
        }}
    elif "reminder" in args:
        data = {{
            "id": "rem-1",
            "content": value_of("--content", ""),
            "remind_at": value_of("--remind-at", ""),
            "status": "pending",
        }}
    elif "schedule" in args:
        data = {{
            "id": "event-1",
            "title": value_of("--title", ""),
            "status": "pending",
            "start_at": value_of("--start-at", ""),
            "end_at": value_of("--end-at", ""),
            "location": value_of("--location", ""),
            "all_day": False,
        }}
    else:
        data = base_item()
else:
    data = {{"echo": args}}

print(json.dumps({{"success": True, "data": data, "error": None}}, ensure_ascii=False))
'''


def write_fake_paadm(directory: str, mode: str = "success", *, name: str = "") -> tuple:
    """写一个可执行的假 paadm 脚本，返回 (脚本路径, 调用记录路径)。"""
    base = name or f"fake_paadm_{mode}_{os.getpid()}_{abs(hash(directory)) % 100000}"
    script_path = os.path.join(str(directory), base + ".py")
    calls_path = os.path.join(str(directory), base + ".calls.jsonl")
    content = _FAKE_PAADM_TEMPLATE.format(mode=mode, calls=calls_path)
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(script_path, 0o755)
    return script_path, calls_path


def read_calls(calls_path: str) -> list:
    """读取假 paadm 记录的 argv 列表（每次调用一行 JSON 数组）。"""
    if not os.path.exists(calls_path):
        return []
    calls = []
    with open(calls_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                calls.append(json.loads(line))
    return calls


# ---------------------------------------------------------------------------
# 事件 / 插件实例 / 异步辅助
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "plain"


class FakeEvent:
    """AstrMessageEvent 的最小替身（只实现插件用到的 API）。"""

    def __init__(
        self,
        platform: str = "aiocqhttp",
        sender_id: str = "2856307114",
        sender_name: str = "huy",
        message_str: str = "",
    ) -> None:
        self.platform = platform
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.message_str = message_str
        self.results: list = []

    def get_platform_name(self) -> str:
        return self.platform

    def get_platform_id(self) -> str:
        return getattr(self, "platform_id", self.platform)

    def get_unified_msg_origin(self) -> str:
        return f"{self.get_platform_id()}:FriendMessage:{self.sender_id}"

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_sender_name(self) -> str:
        return self.sender_name

    def plain_result(self, text: str) -> FakeResult:
        result = FakeResult(text)
        self.results.append(result)
        return result


def make_config(tmpdir: str, **overrides) -> dict:
    """默认插件配置（显式给全，避免依赖 schema 默认层）。"""
    config = {
        "paadm_bin": "/AstrBot/data/paa/bin/paadm",
        "paadm_db": os.path.join(tmpdir, "paa.db"),
        "interactions_db": os.path.join(tmpdir, "interactions.db"),
        "timeout_seconds": 15,
        "reply_max_rows": 20,
        "log_stdout_chars": 4000,
        "provider_override": "",
    }
    config.update(overrides)
    return config


def make_plugin(module, tmpdir: str, **overrides):
    """实例化插件（config 全显式，不依赖 AstrBot 配置层）。"""
    return module.PaadmPlugin(context=object(), config=make_config(tmpdir, **overrides))


def run_handler(plugin, event, text: str) -> list:
    """跑完 /paa 命令（async generator），返回 yield 的结果列表。"""

    async def _run():
        return [item async for item in plugin.paa_cmd(event, text)]

    return asyncio.run(_run())


def run(coro):
    return asyncio.run(coro)


async def call_llm_tool(plugin, event, action: str, args_json: str = "") -> str:
    return await plugin.paadm_tool(event, action=action, args_json=args_json)


__all__ = [
    "FakeEvent",
    "FakeResult",
    "LLM_TOOL_REGISTRY",
    "PLUGIN_DIR",
    "TESTS_DIR",
    "call_llm_tool",
    "install_stubs",
    "load_plugin",
    "make_config",
    "make_plugin",
    "read_calls",
    "run",
    "run_handler",
    "write_fake_paadm",
]
