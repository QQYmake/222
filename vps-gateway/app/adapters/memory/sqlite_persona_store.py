"""SQLitePersonaStore：人格存储 SQLite 适配器。

数据合同来源：V3 架构文档 5.8 人格存储。
"""
from __future__ import annotations

import json
import sqlite3
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.domain.ports.persona_store import PersonaStore


class SQLitePersonaStore(PersonaStore):
    """SQLite 人格适配器。"""

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
                CREATE TABLE IF NOT EXISTS persona_profiles (
                    actor_id TEXT PRIMARY KEY,
                    big_five TEXT NOT NULL,
                    efstb TEXT NOT NULL,
                    aliases TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persona_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT NOT NULL,
                    observation TEXT NOT NULL,
                    raw_observation TEXT NOT NULL,
                    source_episode_ids TEXT,
                    created_at TEXT NOT NULL
                )
            """)
        finally:
            conn.close()

    async def write_profile(
        self,
        actor_id: str,
        big_five: dict[str, float],
        efstb: dict[str, float],
        aliases: list[str],
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO persona_profiles
                (actor_id, big_five, efstb, aliases, updated_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    actor_id,
                    json.dumps(big_five),
                    json.dumps(efstb),
                    json.dumps(aliases),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        finally:
            conn.close()

    async def read_profile(self, actor_id: str) -> Optional[dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM persona_profiles WHERE actor_id = ?", (actor_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            conn.close()

    async def write_observation(
        self,
        actor_id: str,
        observation: str,
        raw_observation: str,
        source_episode_ids: list[str],
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """INSERT INTO persona_observations
                (actor_id, observation, raw_observation, source_episode_ids, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    actor_id,
                    observation,
                    raw_observation,
                    json.dumps(source_episode_ids),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        finally:
            conn.close()

    async def read_observations(self, actor_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM persona_observations WHERE actor_id = ? ORDER BY created_at",
                (actor_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
