"""SQLiteBufferStore：@a/@d/@e 缓冲 SQLite 适配器。

数据合同来源：V3 架构文档 5.4-5.6 + 6.3。

指令：
  1. 每次操作使用独立 SQLite 短连接
  2. @a (buffer_raw): 跨平台跨窗口，2am 清空
  3. @d (buffer_recall): 已读标记 read_at，不删除，2am 清空
  4. @e (buffer_surface): 读取后删除 (FIFO)
"""
from __future__ import annotations

import json
import sqlite3
import os
from datetime import datetime, timezone
from typing import Optional

from app.domain.models.memory import RecallEntry, SurfaceEntry
from app.domain.ports.buffer_store import BufferStore


class SQLiteBufferStore(BufferStore):
    """SQLite 缓冲适配器。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ensure_dir()
        self._init_db()

    def _ensure_dir(self):
        d = os.path.dirname(self._db_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS buffer_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_platform TEXT,
                    turn_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buffer_raw_created ON buffer_raw(created_at)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS buffer_recall (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    read_at TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buffer_recall_created ON buffer_recall(created_at)"
            )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS buffer_surface (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    raw_content TEXT NOT NULL,
                    surface_type TEXT NOT NULL,
                    source_recall_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_buffer_surface_created ON buffer_surface(created_at)"
            )
        finally:
            conn.close()

    @staticmethod
    def _row_to_recall(row: sqlite3.Row) -> RecallEntry:
        metadata = json.loads(row["metadata"]) if row["metadata"] else None
        return RecallEntry(
            id=row["id"],
            trigger_id=row["trigger_id"],
            content=row["content"],
            raw_content=row["raw_content"],
            metadata=metadata,
            created_at=row["created_at"],
            read_at=row["read_at"],
        )

    @staticmethod
    def _row_to_surface(row: sqlite3.Row) -> SurfaceEntry:
        return SurfaceEntry(
            id=row["id"],
            content=row["content"],
            raw_content=row["raw_content"],
            surface_type=row["surface_type"],
            source_recall_ids=json.loads(row["source_recall_ids"]),
            created_at=row["created_at"],
        )

    async def append_raw(self, role: str, content: str, platform: str, turn_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO buffer_raw (role, content, source_platform, turn_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (role, content, platform, turn_id, datetime.now(timezone.utc).isoformat()),
            )
        finally:
            conn.close()

    async def read_all_raw(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM buffer_raw ORDER BY created_at").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def read_recent_recall(self, n: int) -> list[RecallEntry]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM buffer_recall ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [self._row_to_recall(r) for r in rows]
        finally:
            conn.close()

    async def write_recall(
        self, trigger_id: str, content: str, raw_content: str, metadata: dict
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO buffer_recall (trigger_id, content, raw_content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    trigger_id,
                    content,
                    raw_content,
                    json.dumps(metadata) if metadata else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    async def read_recall_latest(self) -> Optional[RecallEntry]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM buffer_recall ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            # 标记 read_at
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE buffer_recall SET read_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            # 重新读取以反映 read_at 更新
            row = conn.execute(
                "SELECT * FROM buffer_recall WHERE id = ?", (row["id"],)
            ).fetchone()
            return self._row_to_recall(row)
        finally:
            conn.close()

    async def scan_recall_for_surface(self) -> list[RecallEntry]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM buffer_recall ORDER BY created_at"
            ).fetchall()
            return [self._row_to_recall(r) for r in rows]
        finally:
            conn.close()

    async def read_all_recall(self) -> list[RecallEntry]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM buffer_recall ORDER BY created_at"
            ).fetchall()
            return [self._row_to_recall(r) for r in rows]
        finally:
            conn.close()

    async def write_surface(
        self, content: str, raw_content: str, surface_type: str, source_ids: list[int]
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO buffer_surface (content, raw_content, surface_type, source_recall_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    content,
                    raw_content,
                    surface_type,
                    json.dumps(source_ids),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.lastrowid
        finally:
            conn.close()

    async def read_surface(self) -> Optional[SurfaceEntry]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM buffer_surface ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            # FIFO 读取后删除
            conn.execute("DELETE FROM buffer_surface WHERE id = ?", (row["id"],))
            return self._row_to_surface(row)
        finally:
            conn.close()

    async def clear_raw(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM buffer_raw")
        finally:
            conn.close()

    async def clear_recall(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM buffer_recall")
        finally:
            conn.close()
