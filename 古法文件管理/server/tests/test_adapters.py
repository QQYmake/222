"""Tests for type adapters (heart_rate, steps, sleep)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Ensure adapters are registered on import.
import app.adapters.heart_rate  # noqa: F401
import app.adapters.sleep  # noqa: F401
import app.adapters.steps  # noqa: F401
from app.adapters.base import RawObservation, get_all_adapters

REAL_DB = Path("/tmp/Gadgetbridge.db")


# ---------------------------------------------------------------------------
# Synthetic DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def synth_db(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE XIAOMI_ACTIVITY_SAMPLE (
            TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            STEPS INTEGER, HEART_RATE INTEGER
        );
        CREATE TABLE XIAOMI_DAILY_SUMMARY_SAMPLE (
            TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, STEPS INTEGER
        );
        CREATE TABLE XIAOMI_SLEEP_TIME_SAMPLE (
            TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            WAKEUP_TIME INTEGER, IS_AWAKE INTEGER, TOTAL_DURATION INTEGER,
            DEEP_SLEEP_DURATION INTEGER, LIGHT_SLEEP_DURATION INTEGER,
            REM_SLEEP_DURATION INTEGER, AWAKE_DURATION INTEGER
        );
        CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE (
            TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, STAGE INTEGER
        );
    """)
    conn.executemany(
        "INSERT INTO XIAOMI_ACTIVITY_SAMPLE VALUES (?,?,?,?,?)",
        [
            (1000, 1, 1, 0, 79),
            (2000, 1, 1, 100, 0),
            (3000, 1, 1, 0, 91),
            (4000, 1, 1, 50, 0),
        ],
    )
    conn.execute(
        "INSERT INTO XIAOMI_DAILY_SUMMARY_SAMPLE VALUES (?,?,?,?)",
        (5_000_000, 1, 1, 8000),  # timestamp in milliseconds
    )
    conn.executemany(
        "INSERT INTO XIAOMI_SLEEP_TIME_SAMPLE VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (10000, 1, 1, 16000, 0, 6000, 2000, 3000, 500, 500),
        ],
    )
    conn.executemany(
        "INSERT INTO XIAOMI_SLEEP_STAGE_SAMPLE VALUES (?,?,?,?)",
        [
            (10000, 1, 1, 1),
            (11000, 1, 1, 2),
            (12000, 1, 1, 3),
        ],
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Heart rate adapter
# ---------------------------------------------------------------------------

class TestHeartRateAdapter:
    def test_extracts_only_heart_rate_rows(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["heart_rate"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        assert len(results) == 2  # only rows with HEART_RATE > 0
        assert all(r.type == "heart_rate" for r in results)

    def test_values_correct(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["heart_rate"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        assert results[0].value == {"bpm": 79}
        assert results[1].value == {"bpm": 91}

    def test_timestamps_in_seconds(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["heart_rate"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        assert results[0].source_timestamp_sec == 1000
        assert results[1].source_timestamp_sec == 3000

    def test_source_table_correct(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["heart_rate"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        assert all(r.source_table == "XIAOMI_ACTIVITY_SAMPLE" for r in results)


# ---------------------------------------------------------------------------
# Steps adapter (dual-source)
# ---------------------------------------------------------------------------

class TestStepsAdapter:
    def test_extracts_both_sources(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["steps"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        types = {r.type for r in results}
        assert "steps" in types
        assert "steps_daily" in types

    def test_per_sample_steps(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["steps"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        sample_steps = [r for r in results if r.type == "steps"]
        assert len(sample_steps) == 2  # rows with STEPS > 0
        assert sample_steps[0].value["steps"] == 100
        assert sample_steps[1].value["steps"] == 50

    def test_daily_summary_ms_to_sec_conversion(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["steps"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        daily = [r for r in results if r.type == "steps_daily"]
        assert len(daily) == 1
        # 5_000_000 ms → 5000 sec
        assert daily[0].source_timestamp_sec == 5000
        assert daily[0].value["steps"] == 8000

    def test_daily_source_tagged(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["steps"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        daily = [r for r in results if r.type == "steps_daily"]
        assert daily[0].value["source"] == "daily_summary"


# ---------------------------------------------------------------------------
# Sleep adapter
# ---------------------------------------------------------------------------

class TestSleepAdapter:
    def test_extracts_sessions_and_stages(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        types = {r.type for r in results}
        assert "sleep_session" in types
        assert "sleep_stage" in types

    def test_session_count(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        sessions = [r for r in results if r.type == "sleep_session"]
        assert len(sessions) == 1

    def test_stage_count(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        stages = [r for r in results if r.type == "sleep_stage"]
        assert len(stages) == 3

    def test_session_values(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        session = next(r for r in results if r.type == "sleep_session")
        assert session.value["wakeup_time"] == 16000
        assert session.value["total_duration"] == 6000
        assert session.value["deep_sleep_duration"] == 2000

    def test_stage_raw_code(self, synth_db: Path):
        conn = sqlite3.connect(str(synth_db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        stages = [r for r in results if r.type == "sleep_stage"]
        codes = [r.value["stage_code"] for r in stages]
        assert codes == [1, 2, 3]

    def test_empty_sleep_tables(self, tmp_path: Path):
        """Sleep tables may be empty in real data — must not error."""
        db = tmp_path / "no_sleep.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE XIAOMI_SLEEP_TIME_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
                WAKEUP_TIME INTEGER, IS_AWAKE INTEGER, TOTAL_DURATION INTEGER,
                DEEP_SLEEP_DURATION INTEGER, LIGHT_SLEEP_DURATION INTEGER,
                REM_SLEEP_DURATION INTEGER, AWAKE_DURATION INTEGER
            );
            CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE (
                TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, STAGE INTEGER
            );
        """)
        conn.commit()
        conn.close()
        conn2 = sqlite3.connect(str(db))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn2, source_device_id=1, source_user_id=1)
        conn2.close()
        assert results == []


# ---------------------------------------------------------------------------
# Real DB tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
class TestRealDBAdapters:
    def test_real_heart_rate(self):
        conn = sqlite3.connect(str(REAL_DB))
        adapter = get_all_adapters()["heart_rate"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        assert len(results) > 0
        assert all(r.type == "heart_rate" for r in results)
        assert all(r.value["bpm"] > 0 for r in results)

    def test_real_steps_dual_source(self):
        conn = sqlite3.connect(str(REAL_DB))
        adapter = get_all_adapters()["steps"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        types = {r.type for r in results}
        assert "steps" in types or "steps_daily" in types

    def test_real_sleep_empty_ok(self):
        conn = sqlite3.connect(str(REAL_DB))
        adapter = get_all_adapters()["sleep"]
        results = adapter.extract(conn, source_device_id=1, source_user_id=1)
        conn.close()
        # Real DB has 0 sleep rows — should return empty, not error
        assert results == []
