"""paadm CLI 子进程客户端：构造 argv、执行、解析 JSON、错误映射、用户缓存。

设计要点（依据 paadm README / src/cli 源码核实）：

- 命令行：``paadm --json [--user-id <UID>] <业务命令...>``；
- 数据库路径通过环境变量 ``PAADM_DB_PATH`` 传入（不依赖 cwd、不拼接 --db）；
- stdout 只输出一个 JSON：``{"success": bool, "data": ..., "error": null|{"code","message"}}``；
- 退出码：成功 0；参数/输入错误 2；业务/存储错误 1；
- 任何业务错误都**不抛异常**，统一映射成稳定错误码：
  ``PAADM_NOT_FOUND`` / ``TIMEOUT`` / ``INVALID_OUTPUT`` / ``SPAWN_ERROR`` / ``PAADM_ERROR``
  （业务错误码如 ``TASK_NOT_FOUND`` 原样透传）；
- 每次实际调用都会写一条 interactions 日志（store 可选，失败只告警）；
- ``resolve_user`` 带进程内缓存 + 锁（同一 provider + external_user_id 只解析一次）。

本模块不 import astrbot：日志通过注入的 logger 对象（需要 info/warning/error 方法）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_TIMEOUT_SECONDS = 15

ERROR_NOT_FOUND = "PAADM_NOT_FOUND"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_INVALID_OUTPUT = "INVALID_OUTPUT"
ERROR_SPAWN = "SPAWN_ERROR"
ERROR_PAADM = "PAADM_ERROR"
ERROR_USER_SCOPE = "USER_SCOPE_VIOLATION"

FORBIDDEN_GLOBAL_ARGS = ("--user-id", "--db")
"""用户可控参数中禁止出现的全局作用域参数。

``--user-id`` 必须由插件根据当前 AstrBot 会话身份注入，``--db`` 只能由
``PAADM_DB_PATH`` 控制；任何从聊天/LLM 参数透传进来的同名参数都视为越权。
"""


def _find_forbidden_global_arg(args: Sequence[Any]) -> Optional[str]:
    """返回第一个越权全局参数（支持 ``--flag value`` 与 ``--flag=value``）。"""
    for item in args:
        token = str(item)
        for name in FORBIDDEN_GLOBAL_ARGS:
            if token == name or token.startswith(name + "="):
                return token
    return None


class _NullLogger:
    """默认 logger：丢弃（独立调试/单测用）。"""

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass


@dataclass
class PaadmResult:
    """一次 paadm 调用的结果（永不携带异常）。"""

    success: bool
    data: Any = None
    error_code: Optional[str] = None
    error_message: str = ""
    exit_code: Optional[int] = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    argv: List[str] = field(default_factory=list)
    timed_out: bool = False

    def error_payload(self) -> Dict[str, Any]:
        return {
            "code": self.error_code or ERROR_PAADM,
            "message": self.error_message or "",
        }


@dataclass
class UserResolution:
    """paadm 用户 resolve 结果。"""

    ok: bool
    user_id: Optional[str] = None
    provider: str = ""
    external_user_id: str = ""
    error_code: Optional[str] = None
    error_message: str = ""
    from_cache: bool = False
    result: Optional[PaadmResult] = None


class PaadmClient:
    """paadm 子进程客户端（同步实现；异步 handler 用 asyncio.to_thread 包一层）。"""

    def __init__(
        self,
        paadm_bin: str,
        db_path: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        store: Any = None,
        log: Any = None,
    ) -> None:
        self.paadm_bin = str(paadm_bin or "")
        self.db_path = str(db_path or "")
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = float(DEFAULT_TIMEOUT_SECONDS)
        self.timeout_seconds = timeout if timeout > 0 else float(DEFAULT_TIMEOUT_SECONDS)
        self.store = store
        self.log = log or _NullLogger()

        self._cache_lock = threading.Lock()
        self._user_cache: Dict[tuple, str] = {}

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------
    def run(
        self,
        args: Sequence[str],
        *,
        source: str,
        action: Optional[str] = None,
        platform: str = "",
        sender_id: str = "",
        user_id: Optional[str] = None,
    ) -> PaadmResult:
        """执行一次 paadm 调用并写交互日志；业务错误不会抛异常。"""
        action_name = action or _action_from_args(args)
        forbidden = _find_forbidden_global_arg(args)
        if forbidden is not None:
            argv = [self.paadm_bin, "--json"]
            result = PaadmResult(
                success=False,
                error_code=ERROR_USER_SCOPE,
                error_message=(
                    f"拒绝执行：业务参数中不允许出现全局参数 {forbidden}；"
                    "用户身份由当前对话自动注入"
                ),
                argv=argv,
            )
            self._record(
                source=source,
                action=action_name,
                platform=platform,
                sender_id=sender_id,
                paadm_user_id=user_id,
                result=result,
            )
            self.log.warning(
                "[paadm] 拒绝越权参数 action=%s arg=%s", action_name, forbidden
            )
            return result

        argv: List[str] = [self.paadm_bin, "--json"]
        if user_id:
            argv += ["--user-id", str(user_id)]
        argv += [str(item) for item in args]

        env = os.environ.copy()
        if self.db_path:
            env["PAADM_DB_PATH"] = self.db_path

        started = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code: Optional[int] = None
        timed_out = False
        error_code: Optional[str] = None
        error_message = ""

        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except FileNotFoundError:
            error_code = ERROR_NOT_FOUND
            error_message = f"找不到 paadm 可执行文件：{self.paadm_bin}"
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            error_code = ERROR_TIMEOUT
            error_message = f"paadm 执行超时（>{self.timeout_seconds:g}s）"
            stdout = _to_text(exc.stdout)
            stderr = _to_text(exc.stderr)
        except OSError as exc:
            error_code = ERROR_SPAWN
            error_message = f"无法启动 paadm：{exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底：客户端永不把异常抛给调用方
            error_code = ERROR_PAADM
            error_message = f"执行 paadm 时发生内部错误：{exc}"

        duration_ms = max(0, int((time.monotonic() - started) * 1000))

        if error_code is not None:
            result = PaadmResult(
                success=False,
                error_code=error_code,
                error_message=error_message,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                argv=argv,
                timed_out=timed_out,
            )
        else:
            result = _parse_result(
                argv=argv,
                exit_code=exit_code if exit_code is not None else -1,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
            )

        self._record(
            source=source,
            action=action_name,
            platform=platform,
            sender_id=sender_id,
            paadm_user_id=user_id,
            result=result,
        )
        if not result.success:
            self.log.warning(
                "[paadm] action=%s 失败 code=%s exit=%s msg=%s",
                action_name,
                result.error_code,
                result.exit_code,
                (result.error_message or "")[:300],
            )
        return result

    # ------------------------------------------------------------------
    # 用户 resolve（幂等 + 内存缓存）
    # ------------------------------------------------------------------
    def resolve_user(
        self,
        provider: str,
        external_user_id: str,
        *,
        source: str = "command",
        platform: str = "",
        sender_id: str = "",
    ) -> UserResolution:
        """``paadm user resolve`` 的幂等封装：同一外部身份只真正调用一次。"""
        provider_text = str(provider or "").strip().lower()
        external_text = str(external_user_id or "").strip()
        if not provider_text or not external_text:
            return UserResolution(
                ok=False,
                provider=provider_text,
                external_user_id=external_text,
                error_code="INVALID_ARGUMENT",
                error_message="缺少平台 provider 或发送者 ID，无法映射 paadm 用户",
            )

        key = (provider_text, external_text)
        with self._cache_lock:
            cached = self._user_cache.get(key)
        if cached:
            return UserResolution(
                ok=True,
                user_id=cached,
                provider=provider_text,
                external_user_id=external_text,
                from_cache=True,
            )

        result = self.run(
            [
                "user",
                "resolve",
                "--provider",
                provider_text,
                "--external-user-id",
                external_text,
            ],
            source=source,
            action="user.resolve",
            platform=platform,
            sender_id=sender_id,
        )
        if not result.success:
            return UserResolution(
                ok=False,
                provider=provider_text,
                external_user_id=external_text,
                error_code=result.error_code,
                error_message=result.error_message,
                result=result,
            )
        data = result.data if isinstance(result.data, dict) else {}
        user_id = data.get("id")
        if not user_id:
            return UserResolution(
                ok=False,
                provider=provider_text,
                external_user_id=external_text,
                error_code=ERROR_INVALID_OUTPUT,
                error_message="paadm user resolve 返回的数据缺少 id",
                result=result,
            )
        resolved = str(user_id)
        with self._cache_lock:
            self._user_cache[key] = resolved
        return UserResolution(
            ok=True,
            user_id=resolved,
            provider=provider_text,
            external_user_id=external_text,
            from_cache=False,
            result=result,
        )

    def clear_user_cache(self) -> None:
        with self._cache_lock:
            self._user_cache.clear()

    # ------------------------------------------------------------------
    # 交互日志
    # ------------------------------------------------------------------
    def _record(
        self,
        *,
        source: str,
        action: str,
        platform: str,
        sender_id: str,
        paadm_user_id: Optional[str],
        result: PaadmResult,
    ) -> None:
        if self.store is None:
            return
        try:
            self.store.record(
                source=source,
                action=action,
                platform=platform,
                sender_id=sender_id,
                paadm_user_id=paadm_user_id,
                argv=result.argv,
                exit_code=result.exit_code,
                success=1 if result.success else 0,
                duration_ms=result.duration_ms,
                error_code=result.error_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except Exception as exc:  # noqa: BLE001 - 日志失败绝不能影响业务回复
            self.log.warning("[paadm] 写交互日志失败（已忽略）：%s", exc)


def _action_from_args(args: Sequence[str]) -> str:
    """没有显式 action 时，从 argv 推断一个可读 action（如 task.create）。"""
    parts = [str(item) for item in args if not str(item).startswith("--")]
    if not parts:
        return "paadm"
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return parts[0]


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_result(
    *,
    argv: List[str],
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: int,
) -> PaadmResult:
    """解析 paadm stdout 的统一 JSON，并映射失败信息。"""
    text = (stdout or "").strip()
    if not text:
        message = f"paadm 没有输出 JSON（exit={exit_code}）"
        if stderr.strip():
            message += f"；stderr：{stderr.strip()[:500]}"
        return PaadmResult(
            success=False,
            error_code=ERROR_INVALID_OUTPUT,
            error_message=message,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return PaadmResult(
            success=False,
            error_code=ERROR_INVALID_OUTPUT,
            error_message=f"paadm 输出不是合法 JSON：{exc}；原文：{text[:500]}",
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )

    if not isinstance(payload, dict) or "success" not in payload:
        return PaadmResult(
            success=False,
            error_code=ERROR_INVALID_OUTPUT,
            error_message="paadm 输出的 JSON 缺少 success 字段",
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )

    ok = bool(payload.get("success"))
    if ok:
        if exit_code != 0:
            return PaadmResult(
                success=False,
                error_code=ERROR_INVALID_OUTPUT,
                error_message=f"paadm 返回 success=true 但退出码为 {exit_code}",
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
                argv=argv,
            )
        return PaadmResult(
            success=True,
            data=payload.get("data"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            argv=argv,
        )

    error = payload.get("error")
    code = ERROR_PAADM
    message = f"paadm 执行失败（exit={exit_code}）"
    if isinstance(error, dict):
        code = str(error.get("code") or ERROR_PAADM)
        message = str(error.get("message") or message)
    elif error is not None:
        message = f"paadm 执行失败：{error}"
    return PaadmResult(
        success=False,
        error_code=code,
        error_message=message,
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        argv=argv,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_INVALID_OUTPUT",
    "ERROR_NOT_FOUND",
    "ERROR_PAADM",
    "ERROR_SPAWN",
    "ERROR_TIMEOUT",
    "PaadmClient",
    "PaadmResult",
    "UserResolution",
]
