"""Normalization of raw observations into deterministic, deduplicated records.

Converts RawObservation (Unix-epoch timestamps) into ObservationRecord
(ISO-8601 UTC + local), generating a stable dedup_key from
device + type + timestamp + source_identity + value fields.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Sequence

from app.adapters.base import RawObservation
from app.database import ObservationRecord

# Asia/Shanghai timezone (UTC+8, no DST).
_TZ_SHANGHAI = timezone(timedelta(hours=8))


def normalize_observations(
    raw_observations: Sequence[RawObservation],
    *,
    internal_device_id: int,
    internal_user_id: int,
    snapshot_hash: str | None = None,
) -> list[ObservationRecord]:
    """Convert raw observations to normalized records with dedup keys."""
    results: list[ObservationRecord] = []
    for raw in raw_observations:
        ts_utc = _to_utc_iso(raw.source_timestamp_sec)
        ts_local = _to_local_iso(raw.source_timestamp_sec)

        dedup_key = _make_dedup_key(
            internal_device_id=internal_device_id,
            obs_type=raw.type,
            timestamp_sec=raw.source_timestamp_sec,
            source_identity=raw.source_identity,
            value=raw.value,
        )

        results.append(ObservationRecord(
            dedup_key=dedup_key,
            type=raw.type,
            timestamp_utc=ts_utc,
            timestamp_local=ts_local,
            value=raw.value,
            raw_source=raw.raw_fields,
            source_table=raw.source_table,
            source_identity=raw.source_identity,
            internal_device_id=internal_device_id,
            internal_user_id=internal_user_id,
            snapshot_hash=snapshot_hash,
        ))
    return results


def _to_utc_iso(timestamp_sec: int) -> str:
    """Convert Unix seconds to ISO-8601 UTC string."""
    dt = datetime.fromtimestamp(timestamp_sec, tz=timezone.utc)
    return dt.isoformat()


def _to_local_iso(timestamp_sec: int) -> str:
    """Convert Unix seconds to ISO-8601 Asia/Shanghai string."""
    dt = datetime.fromtimestamp(timestamp_sec, tz=_TZ_SHANGHAI)
    return dt.isoformat()


def _make_dedup_key(
    *,
    internal_device_id: int,
    obs_type: str,
    timestamp_sec: int,
    source_identity: str,
    value: dict,
) -> str:
    """Generate a deterministic SHA-256 dedup key.

    Same device + type + timestamp + source_identity + value → same key.
    """
    payload = json.dumps(
        {
            "d": internal_device_id,
            "t": obs_type,
            "ts": timestamp_sec,
            "si": source_identity,
            "v": value,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ---------------------------------------------------------------------------
# ISO week helpers
# ---------------------------------------------------------------------------

def iso_week_id(timestamp_utc: str) -> str:
    """Return the ISO week ID (e.g. '2026-W28') for a UTC timestamp."""
    dt = datetime.fromisoformat(timestamp_utc)
    return f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"


def week_utc_range(week_id: str) -> tuple[str, str]:
    """Return (start_utc, end_utc) ISO strings for a given ISO week.

    Week runs Monday 00:00 Asia/Shanghai → Sunday 23:59:59 Asia/Shanghai,
    converted to UTC for database queries.
    """
    year_str, week_str = week_id.split("-W")
    year = int(year_str)
    week = int(week_str)

    # ISO week: Monday is day 1.
    monday = datetime.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=7) - timedelta(seconds=1)

    # Interpret as Asia/Shanghai local time, then convert to UTC.
    monday_local = monday.replace(tzinfo=_TZ_SHANGHAI)
    sunday_local = sunday.replace(tzinfo=_TZ_SHANGHAI)

    return (
        monday_local.astimezone(timezone.utc).isoformat(),
        sunday_local.astimezone(timezone.utc).isoformat(),
    )


def affected_weeks(observations: list[ObservationRecord]) -> list[str]:
    """Return sorted unique list of ISO week IDs from observations."""
    weeks = {iso_week_id(o.timestamp_utc) for o in observations}
    return sorted(weeks)
