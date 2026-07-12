"""SQLiteGraphStore：图谱存储 SQLite 适配器。

数据合同来源：V3 架构文档 5.7 + 6.10。

指令：
  1. 每次操作使用独立 SQLite 短连接
  2. 递归 CTE 替代 Neo4j Cypher 做 1-max_hops 跳遍历
  3. write_event 同时写入 events 和 relations 表
"""
from __future__ import annotations

import json
import sqlite3
import os
from typing import Any

from app.domain.ports.graph_store import GraphStore


class SQLiteGraphStore(GraphStore):
    """SQLite 图谱适配器。"""

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
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    subject TEXT, object TEXT, predicate TEXT,
                    action_type TEXT NOT NULL,
                    context TEXT,
                    event_time TEXT,
                    emotion_label TEXT,
                    impact_score REAL DEFAULT 0.5,
                    confidence REAL DEFAULT 0.7,
                    source_msg_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    object TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    event_id TEXT REFERENCES events(event_id),
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    start_time TEXT, end_time TEXT,
                    event_ids TEXT,
                    big_five_snapshot TEXT,
                    efstb_snapshot TEXT,
                    source_msg_ids TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sagas (
                    saga_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    narrative TEXT NOT NULL,
                    raw_narrative TEXT NOT NULL,
                    episode_ids TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
        finally:
            conn.close()

    async def query_events(self, entity: str, max_hops: int) -> list[dict[str, Any]]:
        """递归 CTE 从 relations 表做 0-(max_hops-1) 跳遍历。

        entity 在 depth 0，其直接邻居在 depth 1，以此类推。
        max_hops=1 只返回 entity 直接参与的 event；
        max_hops=2 额外返回 1-hop 邻居参与的 event。
        """
        conn = self._connect()
        try:
            cte = """
                WITH RECURSIVE hop_chain(depth, node) AS (
                    -- 基始：entity 本身在 depth 0
                    SELECT 0, ?
                    UNION
                    -- 递归：从上一层节点继续跳，直到 depth < max_hops - 1
                    SELECT hc.depth + 1,
                        CASE
                            WHEN r.subject = hc.node THEN r.object
                            WHEN r.object = hc.node THEN r.subject
                        END
                    FROM hop_chain hc
                    JOIN relations r ON r.subject = hc.node OR r.object = hc.node
                    WHERE hc.depth < ? - 1
                )
                SELECT DISTINCT e.* FROM events e
                JOIN relations rel ON rel.event_id = e.event_id
                WHERE rel.subject IN (SELECT node FROM hop_chain)
                   OR rel.object IN (SELECT node FROM hop_chain)
            """
            rows = conn.execute(
                cte, (entity, max_hops)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def query_episodes(self, intent_type: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def query_sagas(self, status: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM sagas WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def query_plans(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE action_type = 'PLAN' ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def write_event(self, event: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO events
                (event_id, subject, object, predicate, action_type, context,
                 event_time, emotion_label, impact_score, confidence,
                 source_msg_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_id"],
                    event.get("subject"),
                    event.get("object"),
                    event.get("predicate"),
                    event["action_type"],
                    event.get("context"),
                    event.get("event_time"),
                    event.get("emotion_label"),
                    event.get("impact_score", 0.5),
                    event.get("confidence", 0.7),
                    event["source_msg_id"],
                    event["created_at"],
                ),
            )
            # 同时写入关系
            if event.get("subject") and event.get("predicate"):
                conn.execute(
                    """INSERT INTO relations (subject, object, predicate, event_id, created_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        event["subject"],
                        event.get("object", ""),
                        event["predicate"],
                        event["event_id"],
                        event["created_at"],
                    ),
                )
        finally:
            conn.close()

    async def write_episode(self, episode: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO episodes
                (episode_id, summary, start_time, end_time, event_ids,
                 big_five_snapshot, efstb_snapshot, source_msg_ids, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    episode["episode_id"],
                    episode["summary"],
                    episode.get("start_time"),
                    episode.get("end_time"),
                    episode.get("event_ids"),
                    episode.get("big_five_snapshot"),
                    episode.get("efstb_snapshot"),
                    episode.get("source_msg_ids"),
                    episode["created_at"],
                ),
            )
        finally:
            conn.close()

    async def write_saga(self, saga: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO sagas
                (saga_id, title, narrative, raw_narrative, episode_ids,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    saga["saga_id"],
                    saga["title"],
                    saga["narrative"],
                    saga["raw_narrative"],
                    saga.get("episode_ids"),
                    saga.get("status", "active"),
                    saga["created_at"],
                    saga["updated_at"],
                ),
            )
        finally:
            conn.close()
