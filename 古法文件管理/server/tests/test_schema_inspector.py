"""Tests for schema inspection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.schema_inspector import SchemaReport, TableInfo, inspect_schema


@pytest.fixture()
def gadgetbridge_db(tmp_path: Path) -> Path:
    """Create a minimal Gadgetbridge-compatible DB for testing."""
    db = tmp_path / "test.db"
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
            WAKEUP_TIME INTEGER, DEEP_SLEEP_DURATION INTEGER
        );
        CREATE TABLE XIAOMI_SLEEP_STAGE_SAMPLE (
            TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, STAGE INTEGER
        );
        CREATE TABLE DEVICE (
            _id INTEGER, NAME TEXT, IDENTIFIER TEXT, TYPE_NAME TEXT, MODEL TEXT
        );
        CREATE TABLE USER (
            _id INTEGER, NAME TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def empty_db(tmp_path: Path) -> Path:
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()
    return db


class TestInspectSchema:
    def test_returns_fingerprint(self, gadgetbridge_db: Path):
        report = inspect_schema(gadgetbridge_db)
        assert len(report.fingerprint) == 64  # SHA-256 hex

    def test_fingerprint_deterministic(self, gadgetbridge_db: Path):
        r1 = inspect_schema(gadgetbridge_db)
        r2 = inspect_schema(gadgetbridge_db)
        assert r1.fingerprint == r2.fingerprint

    def test_fingerprint_changes_with_columns(self, tmp_path: Path):
        db1 = tmp_path / "d1.db"
        db2 = tmp_path / "d2.db"
        for path, extra_col in [(db1, ""), (db2, ", EXTRA INTEGER")]:
            conn = sqlite3.connect(str(path))
            conn.execute(f"CREATE TABLE T (A INTEGER{extra_col})")
            conn.commit()
            conn.close()
        assert inspect_schema(db1).fingerprint != inspect_schema(db2).fingerprint

    def test_supported_schema(self, gadgetbridge_db: Path):
        report = inspect_schema(gadgetbridge_db)
        assert report.is_supported is True

    def test_unsupported_schema(self, empty_db: Path):
        report = inspect_schema(empty_db)
        assert report.is_supported is False

    def test_tables_listed(self, gadgetbridge_db: Path):
        report = inspect_schema(gadgetbridge_db)
        names = {t.name for t in report.tables}
        assert "XIAOMI_ACTIVITY_SAMPLE" in names
        assert "DEVICE" in names

    def test_columns_listed(self, gadgetbridge_db: Path):
        report = inspect_schema(gadgetbridge_db)
        device_table = next(t for t in report.tables if t.name == "DEVICE")
        assert "_id" in device_table.columns
        assert "NAME" in device_table.columns
        assert "IDENTIFIER" in device_table.columns


class TestRealGadgetbridgeDB:
    """Test against the real Gadgetbridge DB if available."""

    REAL_DB = Path("/tmp/Gadgetbridge.db")

    @pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
    def test_real_db_supported(self):
        report = inspect_schema(self.REAL_DB)
        assert report.is_supported is True

    @pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
    def test_real_db_fingerprint_stable(self):
        r1 = inspect_schema(self.REAL_DB)
        r2 = inspect_schema(self.REAL_DB)
        assert r1.fingerprint == r2.fingerprint

    @pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
    def test_real_db_has_expected_tables(self):
        report = inspect_schema(self.REAL_DB)
        names = {t.name for t in report.tables}
        for required in ["XIAOMI_ACTIVITY_SAMPLE", "DEVICE", "USER",
                         "XIAOMI_DAILY_SUMMARY_SAMPLE"]:
            assert required in names
