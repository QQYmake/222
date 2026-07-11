"""Normalized SQLite storage for Health-Bridge server.

Manages database initialization, schema versioning, and read/write operations
for snapshots, devices, users, and observations.

Design decisions (per architecture plan, user-approved):
- Migration strategy: version number + backup + ALTER TABLE (conservative)
- Concurrency: SQLite BEGIN IMMEDIATE transaction lock
- Dedup: UPSERT by deterministic dedup_key
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

_CURRENT_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    hash               TEXT PRIMARY KEY,
    received_at        TEXT NOT NULL,
    schema_fingerprint TEXT NOT NULL,
    status             TEXT NOT NULL,
    new_count          INTEGER NOT NULL DEFAULT 0,
    duplicate_count    INTEGER NOT NULL DEFAULT 0,
    raw_path           TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    internal_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    identifier   TEXT NOT NULL,
    type_name    TEXT,
    model        TEXT,
    first_seen   TEXT NOT NULL,
    UNIQUE(identifier)
);

CREATE TABLE IF NOT EXISTS users (
    internal_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS observations (
    dedup_key           TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    timestamp_utc       TEXT NOT NULL,
    timestamp_local     TEXT NOT NULL,
    value               TEXT NOT NULL,       -- JSON
    raw_source          TEXT NOT NULL,       -- JSON
    source_table        TEXT NOT NULL,
    source_identity     TEXT NOT NULL,
    internal_device_id  INTEGER NOT NULL,
    internal_user_id    INTEGER NOT NULL,
    snapshot_hash       TEXT,
    FOREIGN KEY (internal_device_id) REFERENCES devices(internal_id),
    FOREIGN KEY (internal_user_id) REFERENCES users(internal_id),
    FOREIGN KEY (snapshot_hash) REFERENCES snapshots(hash)
);

CREATE INDEX IF NOT EXISTS idx_obs_type_ts
    ON observations(type, timestamp_utc);

CREATE INDEX IF NOT EXISTS idx_obs_type_ts_asc
    ON observations(type, timestamp_utc ASC);

CREATE TABLE IF NOT EXISTS archive_state (
    week_id           TEXT NOT NULL,
    type              TEXT NOT NULL,
    last_generated_at TEXT NOT NULL,
    PRIMARY KEY (week_id, type)
);
"""


@dataclass(frozen=True)
class ObservationRecord:
    """A single normalized observation as stored in the database."""

    dedup_key: str
    type: str
    timestamp_utc: str
    timestamp_local: str
    value: dict[str, Any]
    raw_source: dict[str, Any]
    source_table: str
    source_identity: str
    internal_device_id: int
    internal_user_id: int
    snapshot_hash: str | None = None


@dataclass(frozen=True)
class IngestResult:
    """Result of writing observations into the database."""

    new_count: int
    duplicate_count: int


@dataclass(frozen=True)
class PageResult:
    """Paginated query result."""

    observations: list[ObservationRecord]
    next_cursor: str | None


def init_database(db_path: Path) -> None:
    """Create the database file and all tables if they don't exist.

    If the database already exists, verify schema version and migrate
    if necessary (backup first).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)

        # Check / set schema version
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(_CURRENT_SCHEMA_VERSION),),
            )
            conn.commit()
        else:
            existing = int(row[0])
            if existing < _CURRENT_SCHEMA_VERSION:
                _migrate(conn, db_path, existing, _CURRENT_SCHEMA_VERSION)
    finally:
        conn.close()


def _migrate(
    conn: sqlite3.Connection,
    db_path: Path,
    from_version: int,
    to_version: int,
) -> None:
    """Backup the database file then apply migrations step by step."""
    backup_path = db_path.with_suffix(f".v{from_version}.bak")
    shutil.copy2(str(db_path), str(backup_path))

    # Future migrations would go here, each bumping version by 1.
    # For now, just update the version stamp.
    conn.execute(
        "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
        (str(to_version),),
    )
    conn.commit()


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Context manager that yields a SQLite connection with WAL mode."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Device / User resolution
# ---------------------------------------------------------------------------

def resolve_device(
    conn: sqlite3.Connection,
    *,
    name: str,
    identifier: str,
    type_name: str | None,
    model: str | None,
) -> int:
    """Return the internal device ID, creating a row if needed."""
    row = conn.execute(
        "SELECT internal_id FROM devices WHERE identifier = ?", (identifier,)
    ).fetchone()
    if row is not None:
        return row["internal_id"]
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO devices (name, identifier, type_name, model, first_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, identifier, type_name, model, now),
    )
    return cur.lastrowid


def resolve_user(
    conn: sqlite3.Connection,
    *,
    name: str,
) -> int:
    """Return the internal user ID, creating a row if needed."""
    row = conn.execute(
        "SELECT internal_id FROM users WHERE name = ?", (name,)
    ).fetchone()
    if row is not None:
        return row["internal_id"]
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO users (name, first_seen) VALUES (?, ?)",
        (name, now),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Snapshot metadata
# ---------------------------------------------------------------------------

def is_duplicate_snapshot(conn: sqlite3.Connection, snapshot_hash: str) -> bool:
    """Check whether a snapshot hash has already been accepted."""
    row = conn.execute(
        "SELECT 1 FROM snapshots WHERE hash = ? AND status = 'accepted'",
        (snapshot_hash,),
    ).fetchone()
    return row is not None


def record_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_hash: str,
    schema_fingerprint: str,
    status: str,
    new_count: int = 0,
    duplicate_count: int = 0,
    raw_path: str | None = None,
) -> None:
    """Insert or replace snapshot metadata."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO snapshots "
        "(hash, received_at, schema_fingerprint, status, new_count, "
        "duplicate_count, raw_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (snapshot_hash, now, schema_fingerprint, status, new_count,
         duplicate_count, raw_path),
    )


# ---------------------------------------------------------------------------
# Observation write / read
# ---------------------------------------------------------------------------

def upsert_observations(
    conn: sqlite3.Connection,
    observations: Sequence[ObservationRecord],
) -> IngestResult:
    """Upsert observations by dedup_key. Returns counts."""
    new_count = 0
    duplicate_count = 0
    for obs in observations:
        cur = conn.execute(
            "INSERT OR IGNORE INTO observations "
            "(dedup_key, type, timestamp_utc, timestamp_local, value, "
            "raw_source, source_table, source_identity, internal_device_id, "
            "internal_user_id, snapshot_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs.dedup_key,
                obs.type,
                obs.timestamp_utc,
                obs.timestamp_local,
                json.dumps(obs.value, sort_keys=True),
                json.dumps(obs.raw_source, sort_keys=True),
                obs.source_table,
                obs.source_identity,
                obs.internal_device_id,
                obs.internal_user_id,
                obs.snapshot_hash,
            ),
        )
        if cur.rowcount > 0:
            new_count += 1
        else:
            duplicate_count += 1
    return IngestResult(new_count=new_count, duplicate_count=duplicate_count)


def query_latest(
    conn: sqlite3.Connection,
    *,
    obs_type: str,
) -> ObservationRecord | None:
    """Return the most recent observation of the given type."""
    row = conn.execute(
        "SELECT * FROM observations WHERE type = ? "
        "ORDER BY timestamp_utc DESC LIMIT 1",
        (obs_type,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def query_range(
    conn: sqlite3.Connection,
    *,
    obs_type: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> PageResult:
    """Query observations in a time range with pagination."""
    conditions = ["type = ?"]
    params: list[Any] = [obs_type]

    if from_ts is not None:
        conditions.append("timestamp_utc >= ?")
        params.append(from_ts)
    if to_ts is not None:
        conditions.append("timestamp_utc <= ?")
        params.append(to_ts)
    if cursor is not None:
        conditions.append("timestamp_utc > ?")
        params.append(cursor)

    where = " AND ".join(conditions)
    query_limit = limit + 1  # fetch one extra to check for next page

    rows = conn.execute(
        f"SELECT * FROM observations WHERE {where} "
        f"ORDER BY timestamp_utc ASC LIMIT {query_limit}",
        params,
    ).fetchall()

    has_next = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1]["timestamp_utc"] if has_next and rows else None

    return PageResult(
        observations=[_row_to_record(r) for r in rows],
        next_cursor=next_cursor,
    )


def query_by_week(
    conn: sqlite3.Connection,
    *,
    week_start_utc: str,
    week_end_utc: str,
    obs_type: str | None = None,
) -> list[ObservationRecord]:
    """Query all observations in a UTC time range, optionally filtered by type."""
    if obs_type:
        rows = conn.execute(
            "SELECT * FROM observations "
            "WHERE type = ? AND timestamp_utc >= ? AND timestamp_utc <= ? "
            "ORDER BY timestamp_utc ASC",
            (obs_type, week_start_utc, week_end_utc),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM observations "
            "WHERE timestamp_utc >= ? AND timestamp_utc <= ? "
            "ORDER BY timestamp_utc ASC",
            (week_start_utc, week_end_utc),
        ).fetchall()
    return [_row_to_record(r) for r in rows]


# ---------------------------------------------------------------------------
# Archive state
# ---------------------------------------------------------------------------

def update_archive_state(
    conn: sqlite3.Connection,
    *,
    week_id: str,
    obs_type: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO archive_state (week_id, type, last_generated_at) "
        "VALUES (?, ?, ?)",
        (week_id, obs_type, now),
    )


def _row_to_record(row: sqlite3.Row) -> ObservationRecord:
    return ObservationRecord(
        dedup_key=row["dedup_key"],
        type=row["type"],
        timestamp_utc=row["timestamp_utc"],
        timestamp_local=row["timestamp_local"],
        value=json.loads(row["value"]),
        raw_source=json.loads(row["raw_source"]),
        source_table=row["source_table"],
        source_identity=row["source_identity"],
        internal_device_id=row["internal_device_id"],
        internal_user_id=row["internal_user_id"],
        snapshot_hash=row["snapshot_hash"] if "snapshot_hash" in row.keys() else None,
    )
