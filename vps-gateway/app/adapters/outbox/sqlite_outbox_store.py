"""SQLiteOutboxStore: OutboxStore 的 SQLite 适配器实现。

数据合同来源：架构文档 6.14 SQLiteOutboxStore v2 + 5.6 OutboxMessage v2。

职责：纯 IO 操作，不含业务逻辑。
  - enqueue_once: 幂等写入（ON CONFLICT DO NOTHING + SELECT）
  - list_after:   游标查询（cursor ASC, LIMIT clamp 1..100）
  - claim_one:    原子领取一条 pending → claimed（M6 实现）

v2 变更：
  - 每次操作使用独立 SQLite 短连接
  - 不长期共享 connection 给 HTTP 与 Scheduler
  - 等待期间不持有 connection 或 transaction
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
    metadata_json  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    claimed_at     TEXT,
    claimed_by     TEXT
)
"""

_MIGRATE_DDL = """
ALTER TABLE outbox_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
"""

_MIGRATE_DDL_2 = """
ALTER TABLE outbox_messages ADD COLUMN claimed_at TEXT
"""

_MIGRATE_DDL_3 = """
ALTER TABLE outbox_messages ADD COLUMN claimed_by TEXT
"""


class SQLiteOutboxStore(OutboxStore):
    """SQLite 持久化 OutboxStore 适配器。

    数据输入: database_path (SQLite 文件路径)
    数据输出: enqueue_once → OutboxMessage, list_after → OutboxPage
    指令:
      1. __init__ 只保存路径，不长期持有连接
      2. 每次操作打开独立短连接，操作结束立即关闭
      3. enqueue_once 原子写入 + 幂等返回
      4. list_after 游标查询 + limit clamp
      5. WAL 模式提升并发
    """

    def __init__(self, database_path: str):
        self._db_path = database_path
        self._logger = get_logger("sqlite_outbox_store")
        # 确保数据库文件和表存在
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        """打开一个独立短连接，WAL 模式。"""
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_table(self):
        """建表 (IF NOT EXISTS) + 迁移旧表。使用短连接。"""
        conn = self._connect()
        try:
            conn.execute(_DDL)
            conn.commit()
            # 迁移：如果旧表缺少 status 列则添加
            cols = {row[1] for row in conn.execute("PRAGMA table_info(outbox_messages)").fetchall()}
            if "status" not in cols:
                conn.execute(_MIGRATE_DDL)
                conn.commit()
            if "claimed_at" not in cols:
                conn.execute(_MIGRATE_DDL_2)
                conn.commit()
            if "claimed_by" not in cols:
                conn.execute(_MIGRATE_DDL_3)
                conn.commit()
        finally:
            conn.close()

    def enqueue_once(self, message: NewOutboxMessage) -> OutboxMessage:
        """幂等写入一条消息。

        数据输入: NewOutboxMessage (不含 cursor)
        数据输出: OutboxMessage (含 cursor)
        指令:
          1. 打开独立连接
          2. INSERT ... ON CONFLICT(trigger_id) DO NOTHING
          3. SELECT WHERE trigger_id = ? → 返回已存在或刚插入的行
          4. COMMIT + 关闭连接
          5. 事务失败时 raise, 不返回假成功
        """
        metadata_json = json.dumps(message.metadata, ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO outbox_messages (event_id, trigger_id, created_at, content, metadata_json, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
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
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            self._logger.error("enqueue_failed", extra={"trigger_id": message.trigger_id})
            conn.close()
            raise

        # SELECT 返回已存在或刚插入的行
        row = conn.execute(
            "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json, status, claimed_at, claimed_by "
            "FROM outbox_messages WHERE trigger_id = ?",
            (message.trigger_id,),
        ).fetchone()

        conn.close()
        return self._row_to_message(row)

    def list_after(self, after_cursor: int, limit: int) -> OutboxPage:
        """游标查询消息。

        数据输入: after_cursor, limit
        数据输出: OutboxPage { items, next_cursor }
        指令:
          1. 打开独立连接
          2. safe_limit = clamp(limit, 1, 100)
          3. SELECT * WHERE cursor > ? ORDER BY cursor ASC LIMIT ?
          4. next_cursor = items.last.cursor if items else after_cursor
          5. 关闭连接
        """
        safe_limit = max(1, min(limit, 100))
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json, status, claimed_at, claimed_by "
                "FROM outbox_messages WHERE cursor > ? ORDER BY cursor ASC LIMIT ?",
                (after_cursor, safe_limit),
            ).fetchall()
        finally:
            conn.close()

        items = [self._row_to_message(row) for row in rows]
        next_cursor = items[-1].cursor if items else after_cursor

        return OutboxPage(items=items, next_cursor=next_cursor)

    async def claim_one(self, after_cursor: int, reader_id: str) -> Optional[OutboxMessage]:
        """原子领取一条 pending 消息，改为 claimed。

        数据输入: after_cursor, reader_id
        数据输出: OutboxMessage | None
        指令:
          1. 打开独立短连接
          2. 事务中选择 cursor 最小的 pending 消息
          3. 原子更新为 claimed 并返回同一行
          4. 操作结束立即提交并关闭连接
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json, status, claimed_at, claimed_by "
                "FROM outbox_messages "
                "WHERE status = 'pending' AND cursor > ? "
                "ORDER BY cursor ASC LIMIT 1",
                (after_cursor,),
            ).fetchone()

            if row is None:
                conn.execute("COMMIT")
                return None

            cursor_val = row[0]
            conn.execute(
                "UPDATE outbox_messages SET status = 'claimed', claimed_at = ?, claimed_by = ? "
                "WHERE cursor = ? AND status = 'pending'",
                (now_iso, reader_id, cursor_val),
            )
            conn.execute("COMMIT")

            # 重新查询已 claimed 的行
            row = conn.execute(
                "SELECT cursor, event_id, trigger_id, created_at, content, metadata_json, status, claimed_at, claimed_by "
                "FROM outbox_messages WHERE cursor = ?",
                (cursor_val,),
            ).fetchone()
            return self._row_to_message(row)
        except sqlite3.Error as e:
            conn.rollback()
            self._logger.error("claim_failed", extra={"after_cursor": after_cursor, "error": str(e)})
            return None
        finally:
            conn.close()

    def close(self):
        """兼容接口：短连接模式下无需关闭持久连接。"""
        pass

    @staticmethod
    def _row_to_message(row) -> OutboxMessage:
        """SQLite 行 → OutboxMessage。"""
        cursor, event_id, trigger_id, created_at, content, metadata_json = row[:6]
        status = row[6] if len(row) > 6 else "pending"
        claimed_at = row[7] if len(row) > 7 else None
        claimed_by = row[8] if len(row) > 8 else None
        msg = OutboxMessage(
            cursor=cursor,
            event_id=event_id,
            trigger_id=trigger_id,
            created_at=created_at,
            content=content,
            metadata=json.loads(metadata_json),
        )
        # 附加 v2 字段（通过 __dict__ 因为 OutboxMessage 是 frozen dataclass）
        object.__setattr__(msg, 'status', status)
        object.__setattr__(msg, 'claimed_at', claimed_at)
        object.__setattr__(msg, 'claimed_by', claimed_by)
        return msg
