"""Schema inspection for Gadgetbridge SQLite databases.

Opens a validated SQLite file read-only, discovers table/column structure,
and produces a deterministic fingerprint for known-schema matching.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

# Tables we care about for health data extraction.
_REQUIRED_TABLES = frozenset({
    "XIAOMI_ACTIVITY_SAMPLE",
    "XIAOMI_DAILY_SUMMARY_SAMPLE",
    "XIAOMI_SLEEP_TIME_SAMPLE",
    "XIAOMI_SLEEP_STAGE_SAMPLE",
    "DEVICE",
    "USER",
})


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SchemaReport:
    fingerprint: str
    tables: list[TableInfo]
    is_supported: bool


def inspect_schema(db_path: Path) -> SchemaReport:
    """Open *db_path* read-only and return a schema report."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name"
        ).fetchall()
        table_infos: list[TableInfo] = []
        for (table_name,) in rows:
            col_rows = conn.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
            col_names = [r[1] for r in col_rows]
            table_infos.append(TableInfo(name=table_name, columns=col_names))

        fingerprint = _compute_fingerprint(table_infos)
        table_name_set = {t.name for t in table_infos}
        is_supported = _REQUIRED_TABLES.issubset(table_name_set)

        return SchemaReport(
            fingerprint=fingerprint,
            tables=table_infos,
            is_supported=is_supported,
        )
    finally:
        conn.close()


def _compute_fingerprint(tables: list[TableInfo]) -> str:
    """Deterministic hash of table names + column names."""
    parts: list[str] = []
    for t in sorted(tables, key=lambda x: x.name):
        cols = ",".join(sorted(t.columns))
        parts.append(f"{t.name}({cols})")
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode()).hexdigest()
