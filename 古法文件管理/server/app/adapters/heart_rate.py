"""Heart-rate adapter for Gadgetbridge XIAOMI_ACTIVITY_SAMPLE.

Extracts rows where HEART_RATE > 0.  Each row becomes a RawObservation
with type "heart_rate".
"""

from __future__ import annotations

import sqlite3

from app.adapters.base import RawObservation, register_adapter


@register_adapter("heart_rate")
class HeartRateAdapter:
    @staticmethod
    def extract(
        conn: sqlite3.Connection,
        *,
        source_device_id: int,
        source_user_id: int,
    ) -> list[RawObservation]:
        rows = conn.execute(
            "SELECT TIMESTAMP, HEART_RATE FROM XIAOMI_ACTIVITY_SAMPLE "
            "WHERE DEVICE_ID = ? AND USER_ID = ? AND HEART_RATE > 0 "
            "ORDER BY TIMESTAMP",
            (source_device_id, source_user_id),
        ).fetchall()

        results: list[RawObservation] = []
        for ts, hr in rows:
            results.append(RawObservation(
                source_timestamp_sec=ts,
                type="heart_rate",
                value={"bpm": hr},
                source_table="XIAOMI_ACTIVITY_SAMPLE",
                source_identity=f"row:{ts}",
                raw_fields={"HEART_RATE": hr, "TIMESTAMP": ts},
            ))
        return results
