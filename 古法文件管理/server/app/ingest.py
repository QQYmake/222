"""Ingest pipeline for Health-Bridge server.

Receives a gzip-compressed Gadgetbridge SQLite snapshot, validates it,
deduplicates against accepted snapshots, inspects schema, resolves devices,
extracts observations via adapters, normalizes them, and writes to the
normalized database.

Concurrency: uses SQLite BEGIN IMMEDIATE as the ingest lock (user-approved).
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import app.adapters  # noqa: F401  — registers all adapters on import
from app.adapters.base import RawObservation, get_all_adapters
from app.config import ServerConfig
from app.database import (
    ObservationRecord,
    connect,
    init_database,
    is_duplicate_snapshot,
    record_snapshot,
    resolve_device,
    resolve_user,
    upsert_observations,
)
from app.normalizer import normalize_observations, affected_weeks
from app.schema_inspector import inspect_schema

# SQLite databases always start with these 16 bytes.
_SQLITE_MAGIC = b"SQLite format 3\x00"


class IngestStatus(str, Enum):
    UPLOADED = "uploaded"
    DUPLICATE = "duplicate"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


@dataclass(frozen=True)
class IngestResponse:
    snapshot_hash: str
    is_new: bool
    status: IngestStatus
    new_count: int = 0
    duplicate_count: int = 0
    schema_fingerprint: str = ""
    affected_weeks: list[str] = field(default_factory=list)
    raw_path: str | None = None


class IngestError(Exception):
    """Base for ingest errors."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def ingest_snapshot(
    gzip_data: bytes,
    config: ServerConfig,
    *,
    client_hash: str | None = None,
) -> IngestResponse:
    """Full ingest pipeline. Raises IngestError on validation failures."""
    # Ensure directories exist.
    config.incoming_dir.mkdir(parents=True, exist_ok=True)
    config.raw_dir.mkdir(parents=True, exist_ok=True)
    init_database(config.db_path)

    # 1. Decompress and check size (gzip bomb protection).
    try:
        raw_bytes = gzip.decompress(gzip_data)
    except Exception as exc:
        raise IngestError("Invalid gzip data") from exc

    if len(raw_bytes) > config.max_decompressed_bytes:
        raise IngestError(
            f"Decompressed size {len(raw_bytes)} exceeds limit "
            f"{config.max_decompressed_bytes}"
        )

    # 2. Validate SQLite magic + integrity.
    if raw_bytes[:16] != _SQLITE_MAGIC:
        raise IngestError("Not a valid SQLite file")

    # Write to incoming temp file.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    incoming_path = config.incoming_dir / f"snapshot-{timestamp}.db"
    incoming_path.write_bytes(raw_bytes)

    # Integrity check.
    _check_sqlite_integrity(incoming_path)

    # 3. Compute SHA-256 and dedup.
    snapshot_hash = hashlib.sha256(raw_bytes).hexdigest()

    with connect(config.db_path) as conn:
        if is_duplicate_snapshot(conn, snapshot_hash):
            record_snapshot(
                conn,
                snapshot_hash=snapshot_hash,
                schema_fingerprint="",
                status="duplicate",
            )
            return IngestResponse(
                snapshot_hash=snapshot_hash,
                is_new=False,
                status=IngestStatus.DUPLICATE,
            )

        # 4. Schema inspection.
        report = inspect_schema(incoming_path)

        if not report.is_supported:
            # Keep raw snapshot, record as unsupported.
            raw_path = config.raw_dir / f"snapshot-{timestamp}-{snapshot_hash[:8]}.db"
            shutil.move(str(incoming_path), str(raw_path))
            record_snapshot(
                conn,
                snapshot_hash=snapshot_hash,
                schema_fingerprint=report.fingerprint,
                status="unsupported_schema",
                raw_path=str(raw_path),
            )
            return IngestResponse(
                snapshot_hash=snapshot_hash,
                is_new=True,
                status=IngestStatus.UNSUPPORTED_SCHEMA,
                schema_fingerprint=report.fingerprint,
                raw_path=str(raw_path),
            )

        # 5. Move to raw/ (server-generated filename, not client's).
        raw_path = config.raw_dir / f"snapshot-{timestamp}-{snapshot_hash[:8]}.db"
        shutil.move(str(incoming_path), str(raw_path))

        # 6. Resolve device + user from source DB.
        src_conn = sqlite3.connect(str(raw_path))
        try:
            device_row = src_conn.execute(
                "SELECT _id, NAME, IDENTIFIER, TYPE_NAME, MODEL FROM DEVICE LIMIT 1"
            ).fetchone()
            user_row = src_conn.execute(
                "SELECT _id, NAME FROM USER LIMIT 1"
            ).fetchone()
        finally:
            src_conn.close()

        if device_row is None or user_row is None:
            raise IngestError("Source DB has no DEVICE or USER rows")

        source_device_id, dev_name, dev_identifier, dev_type, dev_model = device_row
        _, user_name = user_row

        internal_dev = resolve_device(
            conn,
            name=dev_name,
            identifier=dev_identifier,
            type_name=dev_type,
            model=dev_model,
        )
        internal_user = resolve_user(conn, name=user_name)

        # 7. Extract observations via all adapters.
        src_conn = sqlite3.connect(str(raw_path))
        try:
            all_raw: list[RawObservation] = []
            for obs_type, adapter in get_all_adapters().items():
                raw_obs = adapter.extract(
                    src_conn,
                    source_device_id=source_device_id,
                    source_user_id=user_row[0],
                )
                all_raw.extend(raw_obs)
        finally:
            src_conn.close()

        # 8. Normalize.
        normalized = normalize_observations(
            all_raw,
            internal_device_id=internal_dev,
            internal_user_id=internal_user,
            snapshot_hash=snapshot_hash,
        )

        # 9. Record snapshot metadata BEFORE observations (FK constraint).
        record_snapshot(
            conn,
            snapshot_hash=snapshot_hash,
            schema_fingerprint=report.fingerprint,
            status="accepted",
            new_count=0,
            duplicate_count=0,
            raw_path=str(raw_path),
        )

        # 10. Upsert into normalized DB.
        result = upsert_observations(conn, normalized)

        # 11. Update snapshot metadata with actual counts.
        conn.execute(
            "UPDATE snapshots SET new_count = ?, duplicate_count = ? "
            "WHERE hash = ?",
            (result.new_count, result.duplicate_count, snapshot_hash),
        )

        weeks = affected_weeks(normalized)

    return IngestResponse(
        snapshot_hash=snapshot_hash,
        is_new=True,
        status=IngestStatus.UPLOADED,
        new_count=result.new_count,
        duplicate_count=result.duplicate_count,
        schema_fingerprint=report.fingerprint,
        affected_weeks=weeks,
        raw_path=str(raw_path),
    )


def _check_sqlite_integrity(db_path: Path) -> None:
    """Open read-only and run PRAGMA quick_check."""
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise IngestError(f"SQLite integrity check failed: {exc}") from exc
        if row is None or row[0] != "ok":
            detail = row[0] if row else "no result"
            raise IngestError(f"SQLite integrity check failed: {detail}")
    finally:
        conn.close()
