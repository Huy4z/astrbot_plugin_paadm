"""纯标准库的命令解析、参数校验与中文回复格式化。

本模块不 import astrbot，也不执行任何 IO，便于单测与复用：

- ``parse_command(tokens)``：把 ``/paa`` 后面的参数解析成 :class:`CommandPlan`
  （含 paadm 子命令 argv、日志 action、回复格式 kind）；
- ``build_action(action, payload)``：把 LLM 工具传入的 ``action + args_json``
  转成同样的 :class:`CommandPlan`（键名与 paadm 的 snake_case 参数一致）；
- ``format_reply(...)`` / ``format_error(...)``：把 paadm 的 JSON 结果转成
  简洁中文回复（列表最多 ``reply_max_rows`` 行、总长截断到约 1500 字符）；
- ``tool_result_json(...)``：LLM 工具返回的 JSON 字符串（success/data/error）。

约定（依据 paadm README 与 src/cli/*.rs 核实）：

- 全局 ``--json`` 在子命令前，业务命令必须带 ``--user-id``（由 client 注入）；
- 时间必须是带时区偏移的 ISO 8601（``2026-11-17T23:59:00+08:00``）；
- 金额在 CLI 层是整数人民币分（¥18.50 → 1850），本模块负责「元 → 分」换算；
- paadm 参数名：``--due-at`` / ``--start-at`` / ``--end-at`` / ``--remind-at`` /
  ``--occurred-at`` / ``--description`` / ``--item-id`` / ``--all-day``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOCAL_TZ = timezone(timedelta(hours=8))
"""容器内统一按东八区展示交互时间（ts 列固定 +08:00）。"""

MAX_REPLY_CHARS = 1500
"""单条命令回复的最大字符数（超出截断并加省略号）。"""

MAX_TOOL_JSON_CHARS = 4000
"""LLM 工具返回 JSON 的近似上限（超出时退化为 data_preview）。"""

HELP_TEXT = """\
/paa — paadm 个人事务助手

待办 task：
  /paa task add <标题> [--due ISO] [--priority N] [--desc 描述]
  /paa task list [--status pending|completed|cancelled] [--limit N]
  /paa task done <id>
  /paa task del <id>

日程 schedule：
  /paa schedule add <标题> --start ISO --end ISO [--location 地点] [--all-day 0|1]
  /paa schedule list [--from ISO] [--to ISO] [--limit N]

提醒 reminder：
  /paa reminder add <内容> --at ISO [--item <事项id>]
  /paa reminder list [--status pending|triggered|cancelled] [--limit N]

收支 tx：
  /paa tx add --type expense|income --amount 18.50 --category 分类 [--desc 备注] [--at ISO]
  /paa tx list [--from ISO] [--to ISO]
  /paa tx sum [--from ISO] [--to ISO]

笔记 note：
  /paa note add <内容...> [--title 标题]
  /paa note list [--q 关键词] [--limit N]
  /paa note get <id>

其它：
  /paa user                         查看当前账号对应的 paadm 用户
  /paa help                         查看本帮助

时间：必须带时区，如 2026-11-17T23:59:00+08:00
金额：单位元（18.50 = 18 元 5 角），内部按人民币分存储"""


class CommandError(ValueError):
    """参数解析/校验错误（消息直接面向用户，不执行 paadm）。"""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class CommandPlan:
    """一次待执行的 paadm 业务命令。"""

    action: str
    """交互日志 action，例如 task.create / transaction.summary。"""

    argv: List[str]
    """传给 paadm 的业务参数（不含二进制路径、--json、--user-id）。"""

    kind: str = "generic"
    """回复格式化类型，见 format_reply。"""

    verb: str = ""
    """回复里的中文动作名，如「已创建待办」。"""

    limit: Optional[int] = None
    """本地截断条数（paadm 的列表命令没有 --limit 参数）。"""


@dataclass
class UserResolution:
    """用户 resolve 的结果（由 paadm_client 填充，这里仅用于格式化）。"""

    ok: bool
    user_id: Optional[str] = None
    provider: str = ""
    external_user_id: str = ""
    error_code: Optional[str] = None
    error_message: str = ""
    from_cache: bool = False
    result: Any = None


# ---------------------------------------------------------------------------
# 时间 / 金额 / 基础类型校验
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """当前时间（东八区 ISO 8601，带 +08:00 偏移）。"""
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def parse_iso(value: str, field_label: str) -> str:
    """校验 ISO 8601 时间必须带时区偏移，返回去空白后的原串。

    兼容以 ``Z`` 结尾的 UTC 写法（Python 3.10 的 fromisoformat 不认 Z）。
    """
    text = str(value or "").strip()
    if not text:
        raise CommandError(f"{field_label} 不能为空")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CommandError(
            f"{field_label} 不是合法的 ISO 8601 时间：{text}",
            hint="示例：2026-11-17T23:59:00+08:00（必须带时区偏移）",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommandError(
            f"{field_label} 必须带时区偏移：{text}",
            hint="例如 +08:00；不接受不带时区的裸时间",
        )
    return text


def parse_amount_yuan(value: str) -> int:
    """把「元」金额换算成整数人民币分（18.50 → 1850）。

    只接受正数、最多两位小数；金额正负由收支类型区分（paadm 约束）。
    """
    text = str(value or "").strip()
    if not text:
        raise CommandError("金额不能为空")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise CommandError(
            f"金额格式不正确：{text}",
            hint="示例：/paa tx add --type expense --amount 18.50 --category food",
        ) from exc
    if not amount.is_finite():
        raise CommandError(f"金额格式不正确：{text}")
    if amount <= 0:
        raise CommandError("金额必须大于 0（收入/支出类型由 --type 指定）")
    if amount != amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise CommandError(f"金额最多两位小数：{text}")
    cents = int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    if cents <= 0:
        raise CommandError("金额换算后必须大于 0 分")
    return cents


def format_cents(cents: Any) -> str:
    """整数人民币分 → 「18.50 元」展示串。"""
    try:
        value = int(cents)
    except (TypeError, ValueError):
        return str(cents)
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d} 元"


def parse_int(value: Any, field_label: str) -> int:
    """严格解析整数（允许 int / 数字字符串）。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    try:
        return int(text)
    except ValueError as exc:
        raise CommandError(f"{field_label} 必须是整数：{value}") from exc


def parse_limit(value: Any) -> int:
    """解析 --limit：1..500 的整数。"""
    number = parse_int(value, "--limit")
    if number < 1:
        raise CommandError("--limit 必须大于等于 1")
    if number > 500:
        raise CommandError("--limit 最大 500")
    return number


def parse_bool01(value: Any) -> int:
    """解析 0/1 布尔（兼容 true/false/是/否）。"""
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "是", "全天"):
        return 1
    if text in ("0", "false", "no", "n", "否"):
        return 0
    raise CommandError(f"--all-day 只接受 0 或 1：{value}")


_TASK_STATUS_ALIASES = {
    "pending": "pending",
    "待办": "pending",
    "待处理": "pending",
    "completed": "completed",
    "done": "completed",
    "完成": "completed",
    "已完成": "completed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "取消": "cancelled",
    "已取消": "cancelled",
}

_REMINDER_STATUS_ALIASES = {
    "pending": "pending",
    "待触发": "pending",
    "triggered": "triggered",
    "已触发": "triggered",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "已取消": "cancelled",
}

_TX_TYPE_ALIASES = {
    "expense": "expense",
    "支出": "expense",
    "花费": "expense",
    "income": "income",
    "收入": "income",
}


def _normalize_enum(value: str, aliases: Dict[str, str], field_label: str, allowed: str) -> str:
    text = str(value or "").strip().lower()
    normalized = aliases.get(text)
    if normalized is None:
        raise CommandError(f"{field_label} 取值不合法：{value}", hint=f"可选值：{allowed}")
    return normalized


# ---------------------------------------------------------------------------
# 参数切分（支持 --flag value 与 --flag=value）
# ---------------------------------------------------------------------------


def parse_args(
    tokens: Sequence[str],
    flags: Dict[str, str],
    *,
    max_positionals: Optional[int] = None,
    usage: str = "",
) -> Tuple[List[str], Dict[str, str]]:
    """把 token 列表拆成 (positional, options)。

    :param flags: 允许的参数名（不含 ``--``）→ 目标字段名；所有参数都必须有取值。
    :param max_positionals: 位置参数上限（None 表示不限制）。
    """
    positionals: List[str] = []
    options: Dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positionals.extend(tokens[index + 1:])
            break
        if token.startswith("--"):
            raw = token[2:]
            if not raw:
                raise CommandError("多余的 --", hint=usage)
            if "=" in raw:
                name, value = raw.split("=", 1)
            else:
                name, value = raw, None
            dest = flags.get(name)
            if dest is None:
                raise CommandError(
                    f"未知参数 --{name}",
                    hint=("可用参数：" + "、".join("--" + key for key in flags))
                    if flags
                    else "该命令不接受参数",
                )
            if value is None:
                if index + 1 >= len(tokens):
                    raise CommandError(f"参数 --{name} 缺少取值", hint=usage)
                # 贪婪取值：一直取到下一个 -- 参数为止，支持多词取值
                # （例如 /paa task add 标题 --desc 在 AstrBot 中创建）。
                # 位置参数必须写在参数之前，这也是帮助里给出的语法。
                value_parts = [tokens[index + 1]]
                cursor = index + 2
                while cursor < len(tokens) and not tokens[cursor].startswith("--"):
                    value_parts.append(tokens[cursor])
                    cursor += 1
                value = " ".join(value_parts)
                index = cursor - 1
            options[dest] = value
        else:
            positionals.append(token)
        index += 1
    if max_positionals is not None and len(positionals) > max_positionals:
        raise CommandError(
            f"多余的参数：{' '.join(positionals[max_positionals:])}",
            hint=usage,
        )
    return positionals, options


def _positional_text(positionals: Sequence[str], label: str, usage: str) -> str:
    text = " ".join(positionals).strip()
    if not text:
        raise CommandError(f"缺少{label}", hint=usage)
    return text


def _require_option(options: Dict[str, str], key: str, label: str, usage: str) -> str:
    value = options.get(key)
    if value is None or str(value).strip() == "":
        raise CommandError(f"缺少{label}", hint=usage)
    return str(value)


# ---------------------------------------------------------------------------
# 命令解析（/paa ...）
# ---------------------------------------------------------------------------


def parse_command(tokens: Sequence[str]) -> CommandPlan:
    """解析 ``/paa`` 之后的参数，返回待执行的 paadm 命令。"""
    raw = [str(token) for token in tokens if str(token) != ""]
    if raw and raw[0].lower() == "paa":
        raw = raw[1:]
    if not raw:
        return CommandPlan(action="help", argv=[], kind="help")
    group = raw[0].lower()
    rest = raw[1:]

    if group in ("help", "-h", "--help", "?", "帮助"):
        return CommandPlan(action="help", argv=[], kind="help")
    if group == "user":
        if rest:
            raise CommandError("用法：/paa user", hint="不带任何参数，用于查看当前账号对应的 paadm 用户")
        return CommandPlan(action="user.resolve", argv=[], kind="user", verb="当前 paadm 用户")
    if group == "task":
        return _parse_task(rest)
    if group == "schedule":
        return _parse_schedule(rest)
    if group == "reminder":
        return _parse_reminder(rest)
    if group in ("tx", "transaction"):
        return _parse_transaction(rest)
    if group == "note":
        return _parse_note(rest)
    raise CommandError(
        f"未知命令：{group}",
        hint="发送 /paa help 查看完整命令列表",
    )


def _parse_task(tokens: Sequence[str]) -> CommandPlan:
    if not tokens:
        raise CommandError(
            "缺少 task 子命令",
            hint="可用：/paa task add|list|done|del",
        )
    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub in ("add", "create", "new", "新建"):
        usage = "/paa task add <标题> [--due ISO] [--priority N] [--desc 描述]"
        positionals, options = parse_args(
            rest,
            {
                "due": "due_at",
                "due-at": "due_at",
                "priority": "priority",
                "desc": "description",
                "description": "description",
            },
            usage=usage,
        )
        title = _positional_text(positionals, "待办标题", usage)
        argv = ["task", "create", "--title", title]
        if "due_at" in options:
            argv += ["--due-at", parse_iso(options["due_at"], "--due")]
        if "priority" in options:
            argv += ["--priority", str(parse_int(options["priority"], "--priority"))]
        if "description" in options:
            argv += ["--description", options["description"]]
        return CommandPlan(action="task.create", argv=argv, kind="item_single", verb="已创建待办")

    if sub in ("list", "ls", "列表"):
        usage = "/paa task list [--status pending|completed|cancelled] [--limit N]"
        _, options = parse_args(rest, {"status": "status", "limit": "limit"}, max_positionals=0, usage=usage)
        argv = ["task", "list"]
        limit = None
        if "status" in options:
            status = _normalize_enum(
                options["status"],
                _TASK_STATUS_ALIASES,
                "--status",
                "pending / completed / cancelled",
            )
            argv += ["--status", status]
        if "limit" in options:
            limit = parse_limit(options["limit"])
        return CommandPlan(
            action="task.list",
            argv=argv,
            kind="item_list",
            verb="待办",
            limit=limit,
        )

    if sub in ("get", "show", "查看"):
        usage = "/paa task get <id>"
        positionals, _ = parse_args(rest, {}, max_positionals=1, usage=usage)
        task_id = _positional_text(positionals, "待办 id", usage)
        return CommandPlan(
            action="task.get",
            argv=["task", "get", "--id", task_id],
            kind="item_single",
            verb="待办",
        )

    if sub in ("done", "complete", "完成"):
        usage = "/paa task done <id>"
        positionals, _ = parse_args(rest, {}, max_positionals=1, usage=usage)
        task_id = _positional_text(positionals, "待办 id", usage)
        return CommandPlan(
            action="task.complete",
            argv=["task", "complete", "--id", task_id],
            kind="item_single",
            verb="已完成待办",
        )

    if sub in ("del", "delete", "rm", "删除"):
        usage = "/paa task del <id>"
        positionals, _ = parse_args(rest, {}, max_positionals=1, usage=usage)
        task_id = _positional_text(positionals, "待办 id", usage)
        return CommandPlan(
            action="task.delete",
            argv=["task", "delete", "--id", task_id],
            kind="deleted",
            verb="已删除待办",
        )

    raise CommandError(
        f"未知 task 子命令：{sub}",
        hint="可用：/paa task add|list|done|del",
    )


def _parse_schedule(tokens: Sequence[str]) -> CommandPlan:
    if not tokens:
        raise CommandError(
            "缺少 schedule 子命令",
            hint="可用：/paa schedule add|list",
        )
    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub in ("add", "create", "new", "新建"):
        usage = (
            "/paa schedule add <标题> --start ISO --end ISO "
            "[--location 地点] [--all-day 0|1]"
        )
        positionals, options = parse_args(
            rest,
            {
                "start": "start_at",
                "start-at": "start_at",
                "end": "end_at",
                "end-at": "end_at",
                "location": "location",
                "all-day": "all_day",
            },
            usage=usage,
        )
        title = _positional_text(positionals, "日程标题", usage)
        start_at = parse_iso(_require_option(options, "start_at", "开始时间 --start", usage), "--start")
        end_at = parse_iso(_require_option(options, "end_at", "结束时间 --end", usage), "--end")
        argv = [
            "schedule",
            "create",
            "--title",
            title,
            "--start-at",
            start_at,
            "--end-at",
            end_at,
        ]
        if "location" in options:
            argv += ["--location", options["location"]]
        if "all_day" in options:
            argv += ["--all-day", str(parse_bool01(options["all_day"]))]
        return CommandPlan(
            action="schedule.create",
            argv=argv,
            kind="item_single",
            verb="已创建日程",
        )

    if sub in ("list", "ls", "列表"):
        usage = "/paa schedule list [--from ISO] [--to ISO] [--limit N]"
        _, options = parse_args(
            rest,
            {"from": "from", "to": "to", "limit": "limit"},
            max_positionals=0,
            usage=usage,
        )
        argv = ["schedule", "list"]
        if "from" in options:
            argv += ["--from", parse_iso(options["from"], "--from")]
        if "to" in options:
            argv += ["--to", parse_iso(options["to"], "--to")]
        limit = parse_limit(options["limit"]) if "limit" in options else None
        return CommandPlan(
            action="schedule.list",
            argv=argv,
            kind="item_list",
            verb="日程",
            limit=limit,
        )

    raise CommandError(
        f"未知 schedule 子命令：{sub}",
        hint="可用：/paa schedule add|list",
    )


def _parse_reminder(tokens: Sequence[str]) -> CommandPlan:
    if not tokens:
        raise CommandError(
            "缺少 reminder 子命令",
            hint="可用：/paa reminder add|list",
        )
    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub in ("add", "create", "new", "新建"):
        usage = "/paa reminder add <内容> --at ISO [--item <事项id>]"
        positionals, options = parse_args(
            rest,
            {
                "at": "remind_at",
                "remind-at": "remind_at",
                "item": "item_id",
                "item-id": "item_id",
            },
            usage=usage,
        )
        content = _positional_text(positionals, "提醒内容", usage)
        remind_at = parse_iso(
            _require_option(options, "remind_at", "提醒时间 --at", usage),
            "--at",
        )
        argv = ["reminder", "create", "--content", content, "--remind-at", remind_at]
        if "item_id" in options:
            argv += ["--item-id", options["item_id"]]
        return CommandPlan(
            action="reminder.create",
            argv=argv,
            kind="reminder_single",
            verb="已创建提醒",
        )

    if sub in ("list", "ls", "列表"):
        usage = "/paa reminder list [--status pending|triggered|cancelled] [--limit N]"
        _, options = parse_args(
            rest,
            {
                "status": "status",
                "from": "from",
                "to": "to",
                "item": "item_id",
                "item-id": "item_id",
                "limit": "limit",
            },
            max_positionals=0,
            usage=usage,
        )
        argv = ["reminder", "list"]
        if "status" in options:
            status = _normalize_enum(
                options["status"],
                _REMINDER_STATUS_ALIASES,
                "--status",
                "pending / triggered / cancelled",
            )
            argv += ["--status", status]
        if "from" in options:
            argv += ["--from", parse_iso(options["from"], "--from")]
        if "to" in options:
            argv += ["--to", parse_iso(options["to"], "--to")]
        if "item_id" in options:
            argv += ["--item-id", options["item_id"]]
        limit = parse_limit(options["limit"]) if "limit" in options else None
        return CommandPlan(
            action="reminder.list",
            argv=argv,
            kind="reminder_list",
            verb="提醒",
            limit=limit,
        )

    raise CommandError(
        f"未知 reminder 子命令：{sub}",
        hint="可用：/paa reminder add|list",
    )


def _parse_transaction(tokens: Sequence[str]) -> CommandPlan:
    if not tokens:
        raise CommandError(
            "缺少 tx 子命令",
            hint="可用：/paa tx add|list|sum",
        )
    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub in ("add", "create", "new", "记一笔"):
        usage = (
            "/paa tx add --type expense|income --amount 18.50 "
            "--category 分类 [--desc 备注] [--at ISO]"
        )
        _, options = parse_args(
            rest,
            {
                "type": "type",
                "amount": "amount",
                "category": "category",
                "desc": "description",
                "description": "description",
                "at": "occurred_at",
                "occurred-at": "occurred_at",
            },
            max_positionals=0,
            usage=usage,
        )
        tx_type = _normalize_enum(
            _require_option(options, "type", "收支类型 --type", usage),
            _TX_TYPE_ALIASES,
            "--type",
            "expense / income",
        )
        cents = parse_amount_yuan(_require_option(options, "amount", "金额 --amount", usage))
        category = _require_option(options, "category", "分类 --category", usage)
        occurred_at = (
            parse_iso(options["occurred_at"], "--at")
            if "occurred_at" in options
            else now_iso()
        )
        argv = [
            "transaction",
            "create",
            "--type",
            tx_type,
            "--amount",
            str(cents),
            "--category",
            category,
            "--occurred-at",
            occurred_at,
        ]
        if "description" in options:
            argv += ["--description", options["description"]]
        return CommandPlan(
            action="transaction.create",
            argv=argv,
            kind="tx_single",
            verb="已记账",
        )

    if sub in ("list", "ls", "列表"):
        usage = "/paa tx list [--from ISO] [--to ISO]"
        _, options = parse_args(
            rest,
            {"from": "from", "to": "to"},
            max_positionals=0,
            usage=usage,
        )
        argv = ["transaction", "list"]
        if "from" in options:
            argv += ["--from", parse_iso(options["from"], "--from")]
        if "to" in options:
            argv += ["--to", parse_iso(options["to"], "--to")]
        return CommandPlan(
            action="transaction.list",
            argv=argv,
            kind="tx_list",
            verb="收支记录",
        )

    if sub in ("sum", "summary", "汇总", "统计"):
        usage = "/paa tx sum [--from ISO] [--to ISO]"
        _, options = parse_args(
            rest,
            {"from": "from", "to": "to"},
            max_positionals=0,
            usage=usage,
        )
        argv = ["transaction", "summary"]
        if "from" in options:
            argv += ["--from", parse_iso(options["from"], "--from")]
        if "to" in options:
            argv += ["--to", parse_iso(options["to"], "--to")]
        return CommandPlan(
            action="transaction.summary",
            argv=argv,
            kind="tx_summary",
            verb="收支汇总",
        )

    raise CommandError(
        f"未知 tx 子命令：{sub}",
        hint="可用：/paa tx add|list|sum",
    )


def _parse_note(tokens: Sequence[str]) -> CommandPlan:
    if not tokens:
        raise CommandError(
            "缺少 note 子命令",
            hint="可用：/paa note add|list|get",
        )
    sub = tokens[0].lower()
    rest = tokens[1:]

    if sub in ("add", "create", "new", "新建"):
        usage = "/paa note add <内容...> [--title 标题]"
        positionals, options = parse_args(rest, {"title": "title"}, usage=usage)
        content = _positional_text(positionals, "笔记内容", usage)
        argv = ["note", "create", "--content", content]
        if "title" in options:
            argv += ["--title", options["title"]]
        return CommandPlan(
            action="note.create",
            argv=argv,
            kind="note_single",
            verb="已创建笔记",
        )

    if sub in ("list", "ls", "列表", "search", "搜索"):
        usage = "/paa note list [--q 关键词] [--limit N]"
        _, options = parse_args(
            rest,
            {"q": "keyword", "keyword": "keyword", "limit": "limit"},
            max_positionals=0,
            usage=usage,
        )
        argv = ["note", "list"]
        if "keyword" in options:
            argv += ["--keyword", options["keyword"]]
        limit = parse_limit(options["limit"]) if "limit" in options else None
        return CommandPlan(
            action="note.list",
            argv=argv,
            kind="note_list",
            verb="笔记",
            limit=limit,
        )

    if sub in ("get", "show", "查看"):
        usage = "/paa note get <id>"
        positionals, _ = parse_args(rest, {}, max_positionals=1, usage=usage)
        note_id = _positional_text(positionals, "笔记 id", usage)
        return CommandPlan(
            action="note.get",
            argv=["note", "get", "--id", note_id],
            kind="note_get",
            verb="笔记",
        )

    raise CommandError(
        f"未知 note 子命令：{sub}",
        hint="可用：/paa note add|list|get",
    )


# ---------------------------------------------------------------------------
# LLM 工具 action → paadm 参数
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    group: str
    sub: str
    fields: Tuple[str, ...]
    required: Tuple[str, ...] = ()
    kind: str = "generic"
    verb: str = ""

    @property
    def action(self) -> str:
        return f"{self.group}.{self.sub}"


LLM_ACTIONS: Dict[str, ActionSpec] = {
    "task.create": ActionSpec(
        "task", "create",
        fields=("title", "description", "priority", "due_at"),
        required=("title",),
        kind="item_single",
        verb="已创建待办",
    ),
    "task.get": ActionSpec("task", "get", fields=("id",), required=("id",), kind="item_single", verb="待办"),
    "task.list": ActionSpec(
        "task", "list",
        fields=("status", "priority", "due_from", "due_to", "keyword", "limit"),
        kind="item_list",
        verb="待办",
    ),
    "task.complete": ActionSpec(
        "task", "complete", fields=("id",), required=("id",), kind="item_single", verb="已完成待办"
    ),
    "task.cancel": ActionSpec(
        "task", "cancel", fields=("id",), required=("id",), kind="item_single", verb="已取消待办"
    ),
    "task.delete": ActionSpec("task", "delete", fields=("id",), required=("id",), kind="deleted", verb="已删除待办"),
    "schedule.create": ActionSpec(
        "schedule", "create",
        fields=("title", "description", "start_at", "end_at", "location", "all_day"),
        required=("title", "start_at"),
        kind="item_single",
        verb="已创建日程",
    ),
    "schedule.get": ActionSpec(
        "schedule", "get", fields=("id",), required=("id",), kind="item_single", verb="日程"
    ),
    "schedule.list": ActionSpec(
        "schedule", "list", fields=("from", "to", "keyword", "limit"), kind="item_list", verb="日程"
    ),
    "schedule.delete": ActionSpec(
        "schedule", "delete", fields=("id",), required=("id",), kind="deleted", verb="已删除日程"
    ),
    "reminder.create": ActionSpec(
        "reminder", "create",
        fields=("content", "remind_at", "item_id"),
        required=("content", "remind_at"),
        kind="reminder_single",
        verb="已创建提醒",
    ),
    "reminder.get": ActionSpec(
        "reminder", "get", fields=("id",), required=("id",), kind="reminder_single", verb="提醒"
    ),
    "reminder.list": ActionSpec(
        "reminder", "list", fields=("status", "from", "to", "item_id", "limit"), kind="reminder_list", verb="提醒"
    ),
    "reminder.cancel": ActionSpec(
        "reminder", "cancel", fields=("id",), required=("id",), kind="reminder_single", verb="已取消提醒"
    ),
    "reminder.delete": ActionSpec(
        "reminder", "delete", fields=("id",), required=("id",), kind="deleted", verb="已删除提醒"
    ),
    "transaction.create": ActionSpec(
        "transaction", "create",
        fields=("type", "amount", "category", "description", "occurred_at"),
        required=("type", "amount"),
        kind="tx_single",
        verb="已记账",
    ),
    "transaction.get": ActionSpec(
        "transaction", "get", fields=("id",), required=("id",), kind="tx_single", verb="收支记录"
    ),
    "transaction.list": ActionSpec(
        "transaction", "list", fields=("type", "category", "from", "to", "limit"), kind="tx_list", verb="收支记录"
    ),
    "transaction.summary": ActionSpec(
        "transaction", "summary", fields=("type", "category", "from", "to"), kind="tx_summary", verb="收支汇总"
    ),
    "transaction.delete": ActionSpec(
        "transaction", "delete", fields=("id",), required=("id",), kind="deleted", verb="已删除收支记录"
    ),
    "note.create": ActionSpec(
        "note", "create", fields=("title", "content"), required=("content",), kind="note_single", verb="已创建笔记"
    ),
    "note.get": ActionSpec("note", "get", fields=("id",), required=("id",), kind="note_get", verb="笔记"),
    "note.list": ActionSpec(
        "note", "list", fields=("keyword", "from", "to", "limit"), kind="note_list", verb="笔记"
    ),
    "note.delete": ActionSpec("note", "delete", fields=("id",), required=("id",), kind="deleted", verb="已删除笔记"),
}

LLM_ACTION_NAMES: Tuple[str, ...] = tuple(LLM_ACTIONS.keys())


def build_action(action: str, payload: Any) -> CommandPlan:
    """把 LLM 工具的 action + args_json 对象转成 CommandPlan。

    ``payload`` 的键是 paadm 的 snake_case 参数名；不允许未知键，统一做友好校验。
    """
    key = str(action or "").strip()
    spec = LLM_ACTIONS.get(key)
    if spec is None:
        raise CommandError(
            f"不支持的 action：{key or '(空)'}",
            hint="可用 action：" + "、".join(LLM_ACTION_NAMES),
        )
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CommandError("args_json 必须是 JSON 对象")
    unknown = sorted(set(payload) - set(spec.fields))
    if unknown:
        raise CommandError(
            "args_json 包含不支持的字段：" + "、".join(unknown),
            hint=f"{key} 支持字段：" + ("、".join(spec.fields) or "无"),
        )
    missing = [
        name
        for name in spec.required
        if name not in payload or payload[name] is None or str(payload[name]).strip() == ""
    ]
    if missing:
        raise CommandError(
            f"{key} 缺少必填字段：" + "、".join(missing),
            hint=f"{key} 支持字段：" + "、".join(spec.fields),
        )

    argv: List[str] = [spec.group, spec.sub]
    limit: Optional[int] = None
    for name in spec.fields:
        if name not in payload or payload[name] is None:
            continue
        value = payload[name]
        if name == "limit":
            limit = parse_limit(value)
            continue
        if name == "priority":
            value = parse_int(value, "priority")
        elif name == "all_day":
            value = parse_bool01(value)
        elif name == "amount":
            value = _coerce_cents(value)
        elif name in ("due_at", "start_at", "end_at", "remind_at", "occurred_at", "from", "to"):
            value = parse_iso(str(value), name)
        elif name in ("status", "type"):
            value = str(value).strip()
        elif name == "id":
            value = str(value).strip()
            if not value:
                raise CommandError("id 不能为空")
        argv += ["--" + name.replace("_", "-"), str(value)]

    if key == "transaction.create" and "occurred_at" not in payload:
        argv += ["--occurred-at", now_iso()]
    return CommandPlan(action=key, argv=argv, kind=spec.kind, verb=spec.verb, limit=limit)


def _coerce_cents(value: Any) -> int:
    """LLM 的 transaction.amount 契约是整数人民币分（不是元）。"""
    if isinstance(value, bool):
        raise CommandError("amount 必须是整数人民币分（例如 18.50 元 → 1850）")
    if isinstance(value, int):
        cents = value
    elif isinstance(value, float):
        if value != int(value):
            raise CommandError("amount 必须是整数人民币分（例如 18.50 元 → 1850）")
        cents = int(value)
    else:
        text = str(value).strip()
        try:
            cents = int(text)
        except ValueError as exc:
            raise CommandError(
                "amount 必须是整数人民币分（例如 18.50 元 → 1850）"
            ) from exc
    if cents <= 0:
        raise CommandError("amount 必须大于 0（收入/支出由 type 区分）")
    return cents


# ---------------------------------------------------------------------------
# 回复格式化
# ---------------------------------------------------------------------------

_ERROR_HINTS = {
    "PAADM_NOT_FOUND": "找不到 paadm 可执行文件，请检查插件配置 paadm_bin（容器内默认 /AstrBot/data/paa/bin/paadm）。",
    "TIMEOUT": "paadm 执行超时，请稍后重试或调大 timeout_seconds。",
    "INVALID_OUTPUT": "paadm 返回的不是约定的 JSON，请检查 paadm 版本或容器日志。",
    "SPAWN_ERROR": "无法启动 paadm 进程，请检查二进制权限与路径。",
    "PAADM_ERROR": "paadm 执行失败，可查看交互日志里的 stderr。",
    "TASK_NOT_FOUND": "没有找到这条待办（检查 id 是否正确、是否属于当前用户）。",
    "SCHEDULE_NOT_FOUND": "没有找到这条日程（检查 id 是否正确）。",
    "ITEM_NOT_FOUND": "没有找到关联事项（检查 --item 的 id）。",
    "REMINDER_NOT_FOUND": "没有找到这条提醒。",
    "TRANSACTION_NOT_FOUND": "没有找到这条收支记录。",
    "NOTE_NOT_FOUND": "没有找到这条笔记。",
    "USER_NOT_FOUND": "没有找到该 paadm 用户，可先发送 /paa user 重新解析。",
    "USER_SCOPE_VIOLATION": "安全限制：只能操作当前对话账号自己的数据，不能指定其他 paadm 用户。",
    "INVALID_ARGUMENT": "参数不合法：请检查时间格式、金额、枚举值或字段长度。",
    "INVALID_INPUT": "输入不合法，请调整后重试。",
    "DATABASE_BUSY": "数据库正忙，请稍后重试。",
    "DATABASE_ERROR": "数据库读写失败，请检查 paa 目录权限。",
}


def format_error(code: Optional[str], message: str) -> str:
    """把错误码 + message 变成中文可读回复。"""
    code_text = str(code or "PAADM_ERROR")
    detail = str(message or "").strip() or "未知错误"
    text = f"操作失败（{code_text}）：{detail}"
    hint = _ERROR_HINTS.get(code_text)
    if hint:
        text += f"\n提示：{hint}"
    return truncate_text(text)


def format_command_error(error: CommandError) -> str:
    """参数解析错误的统一回复。"""
    text = f"参数错误：{error.message}"
    if error.hint:
        text += f"\n{error.hint}"
    else:
        text += "\n发送 /paa help 查看用法"
    return truncate_text(text)


def format_resolution_failure(resolution: UserResolution) -> str:
    return format_error(
        resolution.error_code or "PAADM_ERROR",
        resolution.error_message or "无法解析 paadm 用户",
    )


def format_user(resolution: UserResolution) -> str:
    """``/paa user`` 的回复。"""
    data = resolution.result.data if resolution.result is not None else None
    data = data if isinstance(data, dict) else {}
    provider = data.get("provider") or resolution.provider
    external = data.get("external_user_id") or resolution.external_user_id
    lines = [
        f"paadm 用户：{resolution.user_id or '-'}",
        f"provider：{provider or '-'}",
        f"external_user_id：{external or '-'}",
    ]
    if data.get("created") is True:
        lines.append("（本次首次创建）")
    return truncate_text("\n".join(lines))


def truncate_text(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """按字符截断回复，避免刷屏（超长时保留 limit-1 字符 + 省略号）。"""
    text = str(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _preview(value: Any, limit: int = 40) -> str:
    text = "" if value is None else str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _data_dict(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else {}


def _item_status_label(status: Any) -> str:
    return {
        "pending": "待处理",
        "completed": "已完成",
        "cancelled": "已取消",
    }.get(str(status), str(status))


def _reminder_status_label(status: Any) -> str:
    return {
        "pending": "待触发",
        "triggered": "已触发",
        "cancelled": "已取消",
    }.get(str(status), str(status))


def _format_item_line(item: Dict[str, Any]) -> str:
    line = f"- {item.get('id', '?')} [{_item_status_label(item.get('status'))}] {item.get('title', '')}"
    if item.get("priority") is not None:
        line += f"（优先级 {item['priority']}）"
    if item.get("due_at"):
        line += f" 截止 {item['due_at']}"
    if item.get("start_at"):
        line += f" 开始 {item['start_at']}"
    if item.get("end_at"):
        line += f" 结束 {item['end_at']}"
    if item.get("location"):
        line += f" @{item['location']}"
    return line


def _format_item_list(plan: CommandPlan, data: Dict[str, Any], reply_max_rows: int) -> str:
    items = data.get("items") if isinstance(data.get("items"), list) else []
    total = data.get("count")
    total = len(items) if not isinstance(total, int) else total
    label = plan.verb or "记录"
    lines = [f"共 {total} 条{label}"]
    rows = items[:reply_max_rows]
    lines.extend(_format_item_line(item) for item in rows if isinstance(item, dict))
    if total > len(rows):
        lines.append(f"（共 {total} 条，仅显示前 {len(rows)} 条）")
    return "\n".join(lines)


def _format_reminder_list(data: Dict[str, Any], reply_max_rows: int) -> str:
    items = data.get("items") if isinstance(data.get("items"), list) else []
    total = data.get("count")
    total = len(items) if not isinstance(total, int) else total
    lines = [f"共 {total} 条提醒"]
    rows = items[:reply_max_rows]
    for item in rows:
        if not isinstance(item, dict):
            continue
        line = (
            f"- {item.get('id', '?')} [{_reminder_status_label(item.get('status', '?'))}] "
            f"{item.get('remind_at', '')} {item.get('content', '')}"
        )
        if item.get("item_id"):
            line += f"（关联 {item['item_id']}）"
        lines.append(line)
    if total > len(rows):
        lines.append(f"（共 {total} 条，仅显示前 {len(rows)} 条）")
    return "\n".join(lines)


def _format_tx_line(item: Dict[str, Any]) -> str:
    type_label = {"expense": "支出", "income": "收入"}.get(str(item.get("type")), str(item.get("type")))
    line = f"- {item.get('occurred_at', '')} {type_label} {format_cents(item.get('amount'))}"
    if item.get("category"):
        line += f" [{item['category']}]"
    if item.get("description"):
        line += f" {item['description']}"
    return line


def _format_tx_list(data: Dict[str, Any], reply_max_rows: int) -> str:
    items = data.get("items") if isinstance(data.get("items"), list) else []
    total = data.get("count")
    total = len(items) if not isinstance(total, int) else total
    lines = [f"共 {total} 笔收支记录"]
    rows = items[:reply_max_rows]
    lines.extend(_format_tx_line(item) for item in rows if isinstance(item, dict))
    if total > len(rows):
        lines.append(f"（共 {total} 笔，仅显示前 {len(rows)} 笔）")
    return "\n".join(lines)


def _format_tx_summary(data: Dict[str, Any], reply_max_rows: int) -> str:
    lines = [
        "收支汇总",
        f"收入：{format_cents(data.get('income_total', 0))}",
        f"支出：{format_cents(data.get('expense_total', 0))}",
        f"净额：{format_cents(data.get('net_total', 0))}",
    ]
    categories = data.get("by_category")
    if isinstance(categories, list) and categories:
        lines.append("按分类：")
        for row in categories[:reply_max_rows]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"- {row.get('category', '未分类')}："
                f"收入 {format_cents(row.get('income', 0))}，"
                f"支出 {format_cents(row.get('expense', 0))}"
            )
        if len(categories) > reply_max_rows:
            lines.append(f"（共 {len(categories)} 个分类，仅显示前 {reply_max_rows} 个）")
    return "\n".join(lines)


def _format_note(data: Dict[str, Any]) -> str:
    title = data.get("title") or "（无标题）"
    content = str(data.get("content") or "")
    return f"笔记 {data.get('id', '?')}\n标题：{title}\n正文：\n{content}"


def _format_note_list(data: Dict[str, Any], reply_max_rows: int) -> str:
    items = data.get("items") if isinstance(data.get("items"), list) else []
    total = data.get("count")
    total = len(items) if not isinstance(total, int) else total
    lines = [f"共 {total} 条笔记"]
    rows = items[:reply_max_rows]
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "（无标题）"
        lines.append(f"- {item.get('id', '?')} {title}：{_preview(item.get('content'), 30)}")
    if total > len(rows):
        lines.append(f"（共 {total} 条，仅显示前 {len(rows)} 条）")
    return "\n".join(lines)


def _format_json_payload(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(data)


def format_reply(
    plan: CommandPlan,
    result: Any,
    *,
    reply_max_rows: int = 20,
) -> str:
    """把一次 paadm 调用结果格式化成中文回复。"""
    reply_max_rows = max(1, int(reply_max_rows or 1))
    if not getattr(result, "success", False):
        return format_error(
            getattr(result, "error_code", None),
            getattr(result, "error_message", ""),
        )

    data = _data_dict(result)
    kind = plan.kind

    if kind == "item_single":
        title = data.get("title") or ""
        lines = [f"{plan.verb or '操作成功'}：{title}", f"id：{data.get('id', '-')}"]
        if data.get("status"):
            lines.append(f"状态：{_item_status_label(data['status'])}")
        if data.get("due_at"):
            lines.append(f"截止：{data['due_at']}")
        if data.get("start_at"):
            lines.append(f"开始：{data['start_at']}")
        if data.get("end_at"):
            lines.append(f"结束：{data['end_at']}")
        if data.get("location"):
            lines.append(f"地点：{data['location']}")
        if data.get("all_day"):
            lines.append("全天：是")
        return truncate_text("\n".join(lines))

    if kind == "item_list":
        return truncate_text(_format_item_list(plan, data, reply_max_rows))

    if kind == "deleted":
        return truncate_text(f"{plan.verb or '已删除'}：{data.get('id', '-')}")

    if kind == "reminder_single":
        lines = [
            f"{plan.verb or '提醒'}：{data.get('content', '')}",
            f"id：{data.get('id', '-')}",
            f"提醒时间：{data.get('remind_at', '-')}",
        ]
        if data.get("status"):
            lines.append(f"状态：{_reminder_status_label(data['status'])}")
        if data.get("item_id"):
            lines.append(f"关联事项：{data['item_id']}")
        return truncate_text("\n".join(lines))

    if kind == "reminder_list":
        return truncate_text(_format_reminder_list(data, reply_max_rows))

    if kind == "tx_single":
        type_label = {"expense": "支出", "income": "收入"}.get(str(data.get("type")), str(data.get("type")))
        lines = [
            f"{plan.verb or '已记账'}：{type_label} {format_cents(data.get('amount', 0))}",
            f"id：{data.get('id', '-')}",
        ]
        if data.get("category"):
            lines.append(f"分类：{data['category']}")
        if data.get("occurred_at"):
            lines.append(f"发生时间：{data['occurred_at']}")
        if data.get("description"):
            lines.append(f"备注：{data['description']}")
        return truncate_text("\n".join(lines))

    if kind == "tx_list":
        return truncate_text(_format_tx_list(data, reply_max_rows))

    if kind == "tx_summary":
        return truncate_text(_format_tx_summary(data, reply_max_rows))

    if kind == "note_single":
        lines = [f"{plan.verb or '笔记'}：{data.get('title') or _preview(data.get('content'))}"]
        if data.get("id"):
            lines.append(f"id：{data['id']}")
        return truncate_text("\n".join(lines))

    if kind == "note_get":
        return truncate_text(_format_note(data))

    if kind == "note_list":
        return truncate_text(_format_note_list(data, reply_max_rows))

    # generic 兜底
    if not data:
        return "操作成功。"
    return truncate_text("操作成功：\n" + _format_json_payload(data))


def limit_result_data(plan: CommandPlan, result: Any) -> Any:
    """按 plan.limit 截断列表结果（不修改原始 dict，浅拷贝 data）。"""
    data = getattr(result, "data", None)
    if plan.limit is None or not isinstance(data, dict):
        return result
    items = data.get("items")
    if not isinstance(items, list):
        return result
    limited = dict(data)
    limited["items"] = items[: plan.limit]
    result.data = limited
    return result


def tool_result_json(result: Any, *, max_chars: int = MAX_TOOL_JSON_CHARS) -> str:
    """LLM 工具返回的 JSON 字符串（success/data/error 精简）。"""
    if getattr(result, "success", False):
        payload: Dict[str, Any] = {"success": True, "data": getattr(result, "data", None), "error": None}
    else:
        payload = {
            "success": False,
            "data": None,
            "error": {
                "code": getattr(result, "error_code", None) or "PAADM_ERROR",
                "message": getattr(result, "error_message", "") or "",
            },
        }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    preview = text[: max_chars - 200]
    return json.dumps(
        {
            "success": payload["success"],
            "data": None,
            "error": payload["error"],
            "truncated": True,
            "data_preview": preview,
            "hint": "结果过长已截断；请缩小 limit 或缩小时间范围",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tool_error_json(code: str, message: str) -> str:
    """LLM 工具的参数/内部错误（仍返回合法 JSON 字符串）。"""
    return json.dumps(
        {
            "success": False,
            "data": None,
            "error": {"code": str(code or "INVALID_ARGUMENT"), "message": str(message)},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = [
    "ActionSpec",
    "CommandError",
    "CommandPlan",
    "HELP_TEXT",
    "LLM_ACTIONS",
    "LLM_ACTION_NAMES",
    "LOCAL_TZ",
    "MAX_REPLY_CHARS",
    "UserResolution",
    "build_action",
    "format_cents",
    "format_command_error",
    "format_error",
    "format_reply",
    "format_resolution_failure",
    "format_user",
    "limit_result_data",
    "now_iso",
    "parse_amount_yuan",
    "parse_args",
    "parse_bool01",
    "parse_command",
    "parse_int",
    "parse_iso",
    "parse_limit",
    "tool_error_json",
    "tool_result_json",
    "truncate_text",
]
