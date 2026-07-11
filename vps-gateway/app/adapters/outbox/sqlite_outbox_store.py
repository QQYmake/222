"""SQLiteOutboxStore: OutboxStore 的 SQLite 适配器实现。

数据合同来源：架构文档 6.2 OutboxStore + 5.9 OutboxMessage。

职责：纯 IO 操作，不含业务逻辑。
  - enqueue_once: 幂等写入（ON CONFLICT DO NOTHING + SELECT）
  - list_after:   游标查询（cursor ASC, LIMIT clamp 1..100）
"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from app.domain.models.outbox import NewOutboxMessage, OutboxMessage, OutboxPage
from app.domain.ports.outbox_store import OutboxStore
from app.infrastructure.logging import get_logger


_DDL = """
CREATE TABLE IF NOT EXISTS outbox_messages (
    cursor         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    trigger_id     TEXT NOT NULL UNIQUE,
    created_at     TEXT NOT NULL,
    content        TEXT NOT NULL,
    metadata_json  TEXT NOT NULL
)
"""


class SQLiteOutboxStore(OutboxStore):
    """SQLite 持久化 OutboxStore 适配器。

    数据输入: database_path (SQLite 文件路径)
    数据输出: enqueue_once → OutboxMessage, list_after → OutboxPage
    指令:
      1. __init__ 连接 SQLite (check_same_thread=False), WAL 模式, 建表
      2. enqueue_once 原子写入 + 幂等返回
      3. list_after 游标查询 + limit clamp
      4. close() 关闭连接
    """

    def __init__(self, database_path: str):
        self._db_path = database_path
        self._logger = get_logger("sqlite_outbox_store")
        self._conn = sqlite3.connect(database_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_table()

    def _ensure_table(self):
        """建表 (IF NOT EXISTS)。"""
        self._conn.execute(_DDL)
        self._conn.commit()

    def enqueue_once(self, message: NewOutboxMessage) -> OutboxMessage:
        """幂等写入一条消息。

        数据输入: NewOutboxMessage (不含 cursor)
        数据输出: OutboxMessage (含 cursor)
        指令:
          1. BEGIN TRANSACTION
          2. INSERT ... ON CONFLICT(trigger_id) DO NOTHING
          3. SELECT WHERE trigger_id = ? → 返回已存在或刚插入的行
          4. COMMIT
          5. 事务失败时 raise, 不返回假成功
        """
        metadata_json = json.dumps(message.metadata, ensure_ascii=False)
        try:
            self._conn.execute(
                """
                INSERT INTO outbox_messages (event_id, trigger_id, created_at, content, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(trigger_id) DO NOTHING
                """,
                (
                    message.event_id,
                    message.trigger_id,
                    message.created_at,
                    message.content,
                    metadata_json,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            self._conn.rollback()
            self._logger.error("enqueue_failed", extra={"trigger_id": message.trigger_id})
            raise

        # SELECT 返回已存在或刚插入的行
        row = self._conn.execute(
            "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json "
            "FROM outbox_messages WHERE trigger_id = ?",
            (message.trigger_id,),
        ).fetchone()

        return self._row_to_message(row)

    def list_after(self, after_cursor: int, limit: int) -> OutboxPage:
        """游标查询消息。

        数据输入: after_cursor, limit
        数据输出: OutboxPage { items, next_cursor }
        指令:
          1. safe_limit = clamp(limit, 1, 100)
          2. SELECT * WHERE cursor > ? ORDER BY cursor ASC LIMIT ?
          3. next_cursor = items.last.cursor if items else after_cursor
          4. 不删除消息
        """
        safe_limit = max(1, min(limit, 100))

        rows = self._conn.execute(
            "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json "
            "FROM outbox_messages WHERE cursor > ? ORDER BY cursor ASC LIMIT ?",
            (after_cursor, safe_limit),
        ).fetchall()

        items = [self._row_to_message(row) for row in rows]
        next_cursor = items[-1].cursor if items else after_cursor

        return OutboxPage(items=items, next_cursor=next_cursor)

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()

    @staticmethod
    def _row_to_message(row) -> OutboxMessage:
        """SQLite 行 → OutboxMessage。"""
        cursor, event_id, trigger_id, created_at, content, metadata_json = row
        return OutboxMessage(
            cursor=cursor,
            event_id=event_id,
            trigger_id=trigger_id,
            created_at=created_at,
            content=content,
            metadata=json.loads(metadata_json),
        )
