"""interactions 交互日志（纯标准库 sqlite3）。

每次 paadm 调用写一行，供 HTML 面板展示。表结构由面板侧依赖，**不要改列名**：

- ``ts``：ISO8601 本地时间（固定 +08:00）；
- ``source``：'command' | 'llm_tool' | 'startup'；
- ``action``：如 task.create / user.resolve / version；
- ``argv``：JSON 数组；
- ``success``：0/1；
- ``stdout`` / ``stderr``：按配置截断。

实现约定：单文件 SQLite，WAL + busy_timeout，懒连接（每次写入短连接），
写失败只告警不抛出（插件不能因为面板库不可写而崩溃）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

LOCAL_TZ = timezone(timedelta(hours=8))
"""交互时间统一按东八区落库（面板按 +08:00 显示）。"""

DEFAULT_STDOUT_CHARS = 4000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  platform TEXT,
  sender_id TEXT,
  paadm_user_id TEXT,
  action TEXT NOT NULL,
  argv TEXT,
  exit_code INTEGER,
  success INTEGER NOT NULL,
  duration_ms INTEGER,
  error_code TEXT,
  stdout TEXT,
  stderr TEXT
);
CREATE INDEX IF NOT EXISTS idx_interactions_ts ON interactions(ts);
CREATE INDEX IF NOT EXISTS idx_interactions_action ON interactions(action);
"""

INTERACTION_COLUMNS: Sequence[str] = (
    "id",
    "ts",
    "source",
    "platform",
    "sender_id",
    "paadm_user_id",
    "action",
    "argv",
    "exit_code",
    "success",
    "duration_ms",
    "error_code",
    "stdout",
    "stderr",
)
"""面板依赖的列顺序（建表语句的物理顺序）。"""


class _NullLogger:
    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def info(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def error(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def now_local_iso() -> str:
    """ISO8601 本地时间（+08:00），精确到秒。"""
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


class InteractionStore:
    """interactions 表读写封装（线程安全；初始化失败自动禁用）。"""

    def __init__(
        self,
        db_path: str,
        *,
        stdout_limit: int = DEFAULT_STDOUT_CHARS,
        stderr_limit: Optional[int] = None,
        log: Any = None,
    ) -> None:
        self.db_path = str(db_path or "")
        self.stdout_limit = _coerce_limit(stdout_limit, DEFAULT_STDOUT_CHARS)
        self.stderr_limit = (
            self.stdout_limit
            if stderr_limit is None
            else _coerce_limit(stderr_limit, DEFAULT_STDOUT_CHARS)
        )
        self.log = log or _NullLogger()
        self.enabled = False
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        if not self.db_path:
            self.last_error = "interactions_db 未配置"
            self.log.warning("[paadm] 未配置 interactions_db，交互日志功能禁用")
            return
        try:
            self._ensure_schema()
            self.enabled = True
        except Exception as exc:  # noqa: BLE001 - 日志库失败不能影响插件加载
            self.last_error = str(exc)
            self.log.warning(
                "[paadm] 初始化交互日志库失败（插件继续运行，仅无面板日志）：%s", exc
            )

    # ------------------------------------------------------------------
    # 建表 / 查询
    # ------------------------------------------------------------------
    def _ensure_schema(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def record(
        self,
        *,
        source: str,
        action: str,
        success: int,
        ts: Optional[str] = None,
        platform: str = "",
        sender_id: str = "",
        paadm_user_id: Optional[str] = None,
        argv: Optional[Sequence[str]] = None,
        exit_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_code: Optional[str] = None,
        stdout: str = "",
        stderr: str = "",
    ) -> bool:
        """写一条交互日志；成功返回 True，失败只告警返回 False。"""
        if not self.enabled:
            return False
        argv_json: Optional[str] = None
        if argv is not None:
            try:
                argv_json = json.dumps(list(argv), ensure_ascii=False)
            except (TypeError, ValueError):
                argv_json = json.dumps([str(item) for item in argv], ensure_ascii=False)
        values = (
            ts or now_local_iso(),
            str(source or "command"),
            _none_if_empty(platform),
            _none_if_empty(sender_id),
            _none_if_empty(paadm_user_id),
            str(action or "unknown"),
            argv_json,
            int(exit_code) if exit_code is not None else None,
            1 if success else 0,
            int(duration_ms) if duration_ms is not None else None,
            _none_if_empty(error_code),
            _truncate(stdout, self.stdout_limit),
            _truncate(stderr, self.stderr_limit),
        )
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute(
                        """
                        INSERT INTO interactions
                          (ts, source, platform, sender_id, paadm_user_id, action,
                           argv, exit_code, success, duration_ms, error_code,
                           stdout, stderr)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                    conn.commit()
                finally:
                    conn.close()
            return True
        except Exception as exc:  # noqa: BLE001 - 面板日志失败不影响业务
            self.last_error = str(exc)
            self.log.warning("[paadm] 写 interactions 日志失败（已忽略）：%s", exc)
            return False

    # ------------------------------------------------------------------
    # 读取（面板/排查/测试）
    # ------------------------------------------------------------------
    def count(self) -> int:
        row = self._fetch_one("SELECT COUNT(*) FROM interactions")
        return int(row[0]) if row else 0

    def fetch_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """按时间倒序返回最近日志（dict 列表）。"""
        limit = max(1, int(limit or 1))
        rows = self._fetch_all(
            """
            SELECT id, ts, source, platform, sender_id, paadm_user_id, action,
                   argv, exit_code, success, duration_ms, error_code, stdout, stderr
            FROM interactions ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        result: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(zip(INTERACTION_COLUMNS, row))
            if item.get("argv"):
                try:
                    item["argv"] = json.loads(item["argv"])
                except (TypeError, ValueError):
                    pass
            result.append(item)
        return result

    def _fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Sequence[Any]]:
        if not self.enabled:
            return None
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(sql, tuple(params))
                return cursor.fetchone()
            finally:
                conn.close()

    def _fetch_all(self, sql: str, params: Sequence[Any] = ()) -> List[Sequence[Any]]:
        if not self.enabled:
            return []
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(sql, tuple(params))
                return list(cursor.fetchall())
            finally:
                conn.close()


def _coerce_limit(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return number if number > 0 else int(default)


def _none_if_empty(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _truncate(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit]


__all__ = [
    "INTERACTION_COLUMNS",
    "LOCAL_TZ",
    "SCHEMA_SQL",
    "InteractionStore",
    "now_local_iso",
]
