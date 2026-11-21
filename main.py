"""astrbot_plugin_paadm —— 让 AstrBot 用 paadm 管理个人事务。

能力：
- ``/paa ...`` 命令：待办 / 日程 / 提醒 / 收支 / 笔记 / 当前用户；
- ``@filter.llm_tool(name="paadm")``：给 LLM 的通用工具（action + args_json）；
- 每次实际 paadm 调用都写 ``interactions.db``（source=command|llm_tool|startup），
  供 HTML 面板展示；
- 启动自检：``paadm version`` + ``paadm init``（失败只告警，不抛异常）。

AstrBot 4.29.4 实测约束（与 reference/astrbot_plugin_paa_bridge 一致）：
- 类继承 ``astrbot.api.star.Star``，用 ``@register(...)`` 注册；
- **所有 handler 必须定义在 main.py**（加载器按 ``handler.__module__ ==
  data.plugins.<目录名>.main`` 绑定插件实例），辅助逻辑放在同目录子模块；
- 命令参数用 ``GreedyStr`` 收集剩余全部文本；LLM 工具参数 schema 来自
  Google 风格 docstring 的 Args 段（类型限 string/number/boolean/object/array）；
- 插件进程内可用 subprocess 调用 paadm 二进制；纯 stdlib，无第三方依赖。
"""

from __future__ import annotations

import asyncio
import json
import shlex
from typing import Any, Dict, Iterable, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 正常路径：AstrBot 以 data.plugins.<name>.main 导入，包内相对导入可用
    from . import command_router, interaction_store, paadm_client
except ImportError:  # pragma: no cover - 兜底：以顶层模块/文件路径加载时
    import command_router  # type: ignore[no-redef]
    import interaction_store  # type: ignore[no-redef]
    import paadm_client  # type: ignore[no-redef]

try:  # AstrBot 命令参数类型：收集命令名之后的全部文本
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 极少数加载方式下兜底（退化为 str）
    GreedyStr = str  # type: ignore[assignment,misc]

PLUGIN_NAME = "astrbot_plugin_paadm"
PLUGIN_VERSION = "1.0.1"
PLUGIN_DESC = "通过 paadm 管理待办/日程/提醒/收支/笔记，并把每次调用写入交互日志供面板展示"

DEFAULT_CONFIG: Dict[str, Any] = {
    "paadm_bin": "/AstrBot/data/paa/bin/paadm",
    "paadm_db": "/AstrBot/data/paa/paa.db",
    "interactions_db": "/AstrBot/data/paa/interactions.db",
    "timeout_seconds": 15,
    "reply_max_rows": 20,
    "log_stdout_chars": 4000,
    "provider_override": "",
}

PLATFORM_PROVIDER_MAP: Dict[str, str] = {
    "aiocqhttp": "qq",
    "napcat": "qq",
    "onebot": "qq",
    "qq": "qq",
    "qq_official": "qq",
    "qqofficial": "qq",
    "webchat": "webchat",
    "telegram": "telegram",
    "wechat": "wechat",
    "wecom": "wecom",
    "discord": "discord",
    "slack": "slack",
}
"""AstrBot 平台名 → paadm provider（未知平台小写兜底）。"""


def _config_str(config: Dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _config_int(config: Dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _safe_call(event: Any, method: str, default: str = "") -> str:
    """安全调用 event 的 getter（不同适配器字段可能缺失/抛异常）。"""
    getter = getattr(event, method, None)
    if not callable(getter):
        return default
    try:
        value = getter()
    except Exception:  # noqa: BLE001 - 适配层容错
        return default
    return "" if value is None else str(value)


def _command_text_from_event(event: Any) -> str:
    """从 event.message_str 还原 /paa 之后的文本（args 未注入时的兜底）。"""
    try:
        raw = str(getattr(event, "message_str", "") or "").strip()
    except Exception:  # noqa: BLE001 - 适配层容错
        return ""
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    tokens = raw.split(None, 1)
    if tokens and tokens[0].lower() == "paa":
        return tokens[1].strip() if len(tokens) > 1 else ""
    return ""


@register(PLUGIN_NAME, "Huy4z", PLUGIN_DESC, PLUGIN_VERSION)
class PaadmPlugin(Star):
    """paadm 命令 + LLM 工具 + 交互日志的 AstrBot 插件。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        # AstrBot 传入 _conf_schema.json 生成的 AstrBotConfig（dict 子类）
        self.config: Dict[str, Any] = config if isinstance(config, dict) else {}
        self._load_settings()

        self.store = interaction_store.InteractionStore(
            self.interactions_db,
            stdout_limit=self.log_stdout_chars,
            log=logger,
        )
        self.client = paadm_client.PaadmClient(
            self.paadm_bin,
            self.paadm_db,
            timeout_seconds=self.timeout_seconds,
            store=self.store,
            log=logger,
        )
        self._startup_checked = False
        self._startup_lock = asyncio.Lock()

        logger.info(
            "[paadm] v%s 已加载：bin=%s db=%s interactions=%s timeout=%ss",
            PLUGIN_VERSION,
            self.paadm_bin,
            self.paadm_db,
            self.interactions_db,
            self.timeout_seconds,
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        config = self.config
        self.paadm_bin = _config_str(config, "paadm_bin", DEFAULT_CONFIG["paadm_bin"])
        self.paadm_db = _config_str(config, "paadm_db", DEFAULT_CONFIG["paadm_db"])
        self.interactions_db = _config_str(
            config, "interactions_db", DEFAULT_CONFIG["interactions_db"]
        )
        self.timeout_seconds = _config_int(
            config, "timeout_seconds", int(DEFAULT_CONFIG["timeout_seconds"])
        )
        self.reply_max_rows = _config_int(
            config, "reply_max_rows", int(DEFAULT_CONFIG["reply_max_rows"])
        )
        self.log_stdout_chars = _config_int(
            config, "log_stdout_chars", int(DEFAULT_CONFIG["log_stdout_chars"])
        )
        self.provider_override = _config_str(config, "provider_override", "")

    # ------------------------------------------------------------------
    # 事件元数据 / provider 映射
    # ------------------------------------------------------------------
    def _event_meta(self, event: Any) -> Dict[str, str]:
        platform = _safe_call(event, "get_platform_name", "")
        sender_id = _safe_call(event, "get_sender_id", "")
        provider = self._provider_for(platform)
        return {
            "platform": platform,
            "sender_id": sender_id,
            "provider": provider,
        }

    def _provider_for(self, platform: str) -> str:
        override = (self.provider_override or "").strip()
        if override:
            return override.lower()
        name = str(platform or "").strip().lower()
        if name in PLATFORM_PROVIDER_MAP:
            return PLATFORM_PROVIDER_MAP[name]
        return name or "unknown"

    # ------------------------------------------------------------------
    # paadm 调用封装（阻塞的 subprocess 放到线程池）
    # ------------------------------------------------------------------
    async def _run_paadm(
        self,
        args: Iterable[str],
        *,
        source: str,
        action: str,
        platform: str = "",
        sender_id: str = "",
        user_id: Optional[str] = None,
    ) -> paadm_client.PaadmResult:
        # 安全边界：除系统动作外，所有业务调用都必须带当前会话解析出的 paadm 用户。
        # 这里做最后一道断言，避免未来新增命令忘记注入 --user-id。
        if not user_id and action not in ("version", "init", "user.resolve"):
            logger.error("[paadm] 拒绝无用户上下文的业务调用 action=%s", action)
            return paadm_client.PaadmResult(
                success=False,
                error_code="USER_SCOPE_VIOLATION",
                error_message="安全限制：业务调用缺少当前对话用户上下文",
            )
        return await asyncio.to_thread(
            self.client.run,
            list(args),
            source=source,
            action=action,
            platform=platform,
            sender_id=sender_id,
            user_id=user_id,
        )

    async def _resolve_user(
        self,
        event: Any,
        *,
        source: str,
    ) -> Tuple[Dict[str, str], command_router.UserResolution]:
        meta = self._event_meta(event)
        resolution = await asyncio.to_thread(
            self.client.resolve_user,
            meta["provider"],
            meta["sender_id"],
            source=source,
            platform=meta["platform"],
            sender_id=meta["sender_id"],
        )
        return meta, resolution

    # ------------------------------------------------------------------
    # /paa 命令
    # ------------------------------------------------------------------
    @filter.command("paa")
    async def paa_cmd(self, event: AstrMessageEvent, args: GreedyStr):
        """paadm 命令入口：/paa help 查看用法。

        注意：``args`` 必须写成**没有默认值**的 ``GreedyStr``。AstrBot 4.29.4 的
        ``CommandFilter.init_handler_md`` 对带默认值的参数只记录默认值、丢掉
        ``GreedyStr`` 标记，导致命令只拿到第一个词（例如 ``/paa task add x``
        只收到 ``task``）。无参数时剩余列表为空，AstrBot 会注入空字符串。
        """
        text = str(args or "").strip()
        if not text:
            # 兼容极少数加载路径下命令参数未注入的情况：从 event 文本里还原。
            text = _command_text_from_event(event)
        reply = await self._safe_handle_command(event, text)
        yield event.plain_result(reply)

    async def _safe_handle_command(self, event: Any, text: str) -> str:
        """命令处理兜底：任何异常都转成可读回复，绝不让 AstrBot 报错。"""
        try:
            return await self._handle_command(event, text)
        except Exception:  # noqa: BLE001 - 插件内部错误不能炸掉对话
            logger.exception("[paadm] 处理 /paa 命令异常")
            return "插件内部错误：处理命令时发生异常，请查看 AstrBot 日志。"

    async def _handle_command(self, event: Any, text: str) -> str:
        try:
            tokens = shlex.split(text) if text else []
        except ValueError as exc:
            return command_router.format_command_error(
                command_router.CommandError(
                    f"命令中的引号不匹配：{exc}",
                    hint="含空格的标题请用引号包起来，例如 /paa task add \"写周报\"",
                )
            )
        try:
            plan = command_router.parse_command(tokens)
        except command_router.CommandError as exc:
            return command_router.format_command_error(exc)

        if plan.kind == "help":
            return command_router.HELP_TEXT

        meta = self._event_meta(event)

        # 业务命令：先幂等 resolve 当前发送者对应的 paadm 用户
        _, resolution = await self._resolve_user(event, source="command")
        if not resolution.ok:
            return command_router.format_resolution_failure(resolution)
        if plan.action == "user.resolve":
            return command_router.format_user(resolution)

        result = await self._run_paadm(
            plan.argv,
            source="command",
            action=plan.action,
            platform=meta["platform"],
            sender_id=meta["sender_id"],
            user_id=resolution.user_id,
        )
        command_router.limit_result_data(plan, result)
        return command_router.format_reply(plan, result, reply_max_rows=self.reply_max_rows)

    # ------------------------------------------------------------------
    # LLM 工具（声明必须在 main.py；参数 schema 来自 docstring Args 段）
    # ------------------------------------------------------------------
    @filter.llm_tool(name="paadm")
    async def paadm_tool(self, event: AstrMessageEvent, action: str, args_json: str = "") -> str:
        """调用 paadm 管理当前用户的待办、日程、提醒、收支和笔记。只有用户明确要求记录/查询个人事务时才调用。

        支持的 action：task.create task.get task.list task.complete task.cancel task.delete
        schedule.create schedule.get schedule.list schedule.delete
        reminder.create reminder.get reminder.list reminder.cancel reminder.delete
        transaction.create transaction.get transaction.list transaction.summary transaction.delete
        note.create note.get note.list note.delete

        注意：transaction.create 的 amount 是整数人民币分（18.50 元 → 1850）；
        其它金额参数在命令层用元，但本工具统一透传 paadm 参数，金额也用分。
        工具只操作当前对话账号自己的数据，不接受 user_id / db 等全局参数。

        Args:
            action(string): 操作名，如 task.create、task.list、transaction.summary、note.get
            args_json(string): JSON 对象字符串，键为 paadm 的 snake_case 参数名，如 {"title":"写周报","due_at":"2026-11-20T18:00:00+08:00"}；无参数时传 {}
        """
        try:
            return await self._run_llm_tool(event, action, args_json)
        except Exception:  # noqa: BLE001 - 工具异常必须变成可读 JSON，不能中断对话
            logger.exception("[paadm] LLM 工具执行异常")
            return command_router.tool_error_json("PLUGIN_ERROR", "插件内部错误，请查看 AstrBot 日志")

    async def _run_llm_tool(self, event: Any, action: str, args_json: Any) -> str:
        if isinstance(args_json, dict):
            payload: Any = args_json
        else:
            raw = str(args_json or "").strip()
            payload = {}
            if raw:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    return command_router.tool_error_json(
                        "INVALID_ARGUMENT", f"args_json 不是合法 JSON：{exc}"
                    )
        try:
            plan = command_router.build_action(action, payload)
        except command_router.CommandError as exc:
            hint = f"；{exc.hint}" if exc.hint else ""
            return command_router.tool_error_json("INVALID_ARGUMENT", f"{exc.message}{hint}")

        meta = self._event_meta(event)

        # 所有 LLM 业务动作都强制使用当前会话解析出的用户；user.* 已从工具移除。
        _, resolution = await self._resolve_user(event, source="llm_tool")
        if not resolution.ok:
            return command_router.tool_error_json(
                resolution.error_code or "PAADM_ERROR",
                resolution.error_message or "无法解析 paadm 用户",
            )
        result = await self._run_paadm(
            plan.argv,
            source="llm_tool",
            action=plan.action,
            platform=meta["platform"],
            sender_id=meta["sender_id"],
            user_id=resolution.user_id,
        )
        command_router.limit_result_data(plan, result)
        return command_router.tool_result_json(result)

    # ------------------------------------------------------------------
    # 生命周期 / 启动自检
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件激活后调用：执行一次 paadm 自检（幂等）。"""
        await self._startup_check()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """AstrBot 加载完成：兜底执行一次 paadm 自检（幂等）。"""
        await self._startup_check()

    async def _startup_check(self) -> None:
        """调用 paadm version + init；成功/失败都写 source='startup' 日志。"""
        if self._startup_checked:
            return
        self._startup_checked = True
        try:
            async with self._startup_lock:
                await self._startup_check_inner()
        except Exception:  # noqa: BLE001 - 自检失败绝不能影响插件加载
            logger.exception("[paadm] 启动自检异常（插件继续运行）")

    async def _startup_check_inner(self) -> None:
        version_result = await self._run_paadm(
            ["version"], source="startup", action="version"
        )
        if version_result.success:
            data = version_result.data if isinstance(version_result.data, dict) else {}
            logger.info(
                "[paadm] 启动自检通过：%s %s",
                data.get("name", "paadm"),
                data.get("version", "?"),
            )
        else:
            logger.warning(
                "[paadm] 启动自检失败：paadm version 调用失败 code=%s message=%s（bin=%s）",
                version_result.error_code,
                version_result.error_message,
                self.paadm_bin,
            )

        init_result = await self._run_paadm(["init"], source="startup", action="init")
        if init_result.success:
            data = init_result.data if isinstance(init_result.data, dict) else {}
            logger.info(
                "[paadm] 启动自检通过：数据库就绪 %s (schema v%s)",
                data.get("db_path", self.paadm_db),
                data.get("schema_version", "?"),
            )
        else:
            logger.warning(
                "[paadm] 启动自检失败：paadm init 调用失败 code=%s message=%s（db=%s）",
                init_result.error_code,
                init_result.error_message,
                self.paadm_db,
            )

    async def terminate(self) -> None:
        """插件禁用/重载：当前实现无后台任务，仅记录日志。"""
        logger.info("[paadm] 插件已停止")


__all__ = ["PaadmPlugin", "PLUGIN_NAME", "PLUGIN_VERSION"]
