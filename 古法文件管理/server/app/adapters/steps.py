"""Steps adapter for Gadgetbridge.

Dual-source per architecture plan (user-approved):
1. XIAOMI_ACTIVITY_SAMPLE — per-sample steps (timestamp in seconds)
2. XIAOMI_DAILY_SUMMARY_SAMPLE — daily aggregate (timestamp in MILLISECONDS)

Both sources are imported, tagged with distinct observation types:
  "steps"       — per-sample
  "steps_daily" — daily aggregate
"""

from __future__ import annotations

import sqlite3

from app.adapters.base import RawObservation, register_adapter


@register_adapter("steps")
class StepsAdapter:
    @staticmethod
    def extract(
        conn: sqlite3.Connection,
        *,
        source_device_id: int,
        source_user_id: int,
    ) -> list[RawObservation]:
        results: list[RawObservation] = []

        # Source 1: per-sample steps (timestamp in seconds)
        sample_rows = conn.execute(
            "SELECT TIMESTAMP, STEPS FROM XIAOMI_ACTIVITY_SAMPLE "
            "WHERE DEVICE_ID = ? AND USER_ID = ? AND STEPS > 0 "
            "ORDER BY TIMESTAMP",
            (source_device_id, source_user_id),
        ).fetchall()
        for ts, steps in sample_rows:
            results.append(RawObservation(
                source_timestamp_sec=ts,
                type="steps",
                value={"steps": steps, "source": "activity_sample"},
                source_table="XIAOMI_ACTIVITY_SAMPLE",
                source_identity=f"row:{ts}",
                raw_fields={"STEPS": steps, "TIMESTAMP": ts},
            ))

        # Source 2: daily summary (timestamp in MILLISECONDS)
        daily_rows = conn.execute(
            "SELECT TIMESTAMP, STEPS FROM XIAOMI_DAILY_SUMMARY_SAMPLE "
            "WHERE DEVICE_ID = ? AND USER_ID = ? "
            "ORDER BY TIMESTAMP",
            (source_device_id, source_user_id),
        ).fetchall()
        for ts_ms, steps in daily_rows:
            ts_sec = ts_ms // 1000
            results.append(RawObservation(
                source_timestamp_sec=ts_sec,
                type="steps_daily",
                value={"steps": steps, "source": "daily_summary"},
                source_table="XIAOMI_DAILY_SUMMARY_SAMPLE",
                source_identity=f"row:{ts_ms}",
                raw_fields={"STEPS": steps, "TIMESTAMP": ts_ms},
            ))

        return results
