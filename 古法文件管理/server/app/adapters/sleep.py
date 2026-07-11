"""Sleep adapter for Gadgetbridge.

Two source tables:
1. XIAOMI_SLEEP_TIME_SAMPLE — sleep sessions (start, duration, stage durations)
2. XIAOMI_SLEEP_STAGE_SAMPLE — per-timestamp sleep stage codes

Stage codes are stored as raw integers per architecture decision (option A).
A disclaimer is included in archive output, not in the value itself.
"""

from __future__ import annotations

import sqlite3

from app.adapters.base import RawObservation, register_adapter


@register_adapter("sleep")
class SleepAdapter:
    @staticmethod
    def extract(
        conn: sqlite3.Connection,
        *,
        source_device_id: int,
        source_user_id: int,
    ) -> list[RawObservation]:
        results: list[RawObservation] = []

        # Source 1: sleep sessions
        session_rows = conn.execute(
            "SELECT TIMESTAMP, WAKEUP_TIME, IS_AWAKE, TOTAL_DURATION, "
            "DEEP_SLEEP_DURATION, LIGHT_SLEEP_DURATION, REM_SLEEP_DURATION, "
            "AWAKE_DURATION "
            "FROM XIAOMI_SLEEP_TIME_SAMPLE "
            "WHERE DEVICE_ID = ? AND USER_ID = ? "
            "ORDER BY TIMESTAMP",
            (source_device_id, source_user_id),
        ).fetchall()
        for row in session_rows:
            (ts, wakeup, is_awake, total, deep, light, rem, awake) = row
            results.append(RawObservation(
                source_timestamp_sec=ts,
                type="sleep_session",
                value={
                    "wakeup_time": wakeup,
                    "is_awake": bool(is_awake),
                    "total_duration": total,
                    "deep_sleep_duration": deep,
                    "light_sleep_duration": light,
                    "rem_sleep_duration": rem,
                    "awake_duration": awake,
                },
                source_table="XIAOMI_SLEEP_TIME_SAMPLE",
                source_identity=f"row:{ts}",
                raw_fields={
                    "TIMESTAMP": ts, "WAKEUP_TIME": wakeup,
                    "TOTAL_DURATION": total,
                    "DEEP_SLEEP_DURATION": deep,
                    "LIGHT_SLEEP_DURATION": light,
                    "REM_SLEEP_DURATION": rem,
                    "AWAKE_DURATION": awake,
                },
            ))

        # Source 2: sleep stages
        stage_rows = conn.execute(
            "SELECT TIMESTAMP, STAGE FROM XIAOMI_SLEEP_STAGE_SAMPLE "
            "WHERE DEVICE_ID = ? AND USER_ID = ? "
            "ORDER BY TIMESTAMP",
            (source_device_id, source_user_id),
        ).fetchall()
        for ts, stage in stage_rows:
            results.append(RawObservation(
                source_timestamp_sec=ts,
                type="sleep_stage",
                value={"stage_code": stage},
                source_table="XIAOMI_SLEEP_STAGE_SAMPLE",
                source_identity=f"row:{ts}",
                raw_fields={"TIMESTAMP": ts, "STAGE": stage},
            ))

        return results
