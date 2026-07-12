"""SQL Pool 适配模块——仅保留 SQLite 路径。

适配自 ebbingflow memory/sql/pool.py：
  - 移除外部数据库分支
  - 保留 SQLite 短连接路径
  - 配置通过构造注入，不引用 vps-gateway config.py
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# 默认数据库路径，可通过 set_default_db_path() 覆盖
_default_db_path: Optional[str] = None


def set_default_db_path(path: str) -> None:
    """设置默认 SQLite 数据库路径。"""
    global _default_db_path
    _default_db_path = path


class AsyncSQLiteCompatCursor:
    """sqlite3.Cursor 的异步兼容包装。"""

    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class AsyncSQLiteCompatConnection:
    """sqlite3.Connection 的异步兼容包装。"""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    async def execute(self, sql: str, params=()):
        cur = self._conn.execute(sql, params or ())
        return AsyncSQLiteCompatCursor(cur)

    async def commit(self):
        self._conn.commit()

    async def close(self):
        self._conn.close()


@contextlib.asynccontextmanager
async def get_db():
    """统一异步数据库访问接口（仅 SQLite）。"""
    db_path = _default_db_path or os.environ.get("MEMORY_DB_PATH", "data/memory_graph.sqlite3")
    d = os.path.dirname(db_path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = AsyncSQLiteCompatConnection(db_path)
    try:
        yield conn
    finally:
        await conn.close()
