"""Tests for the normalized SQLite database module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import (
    ObservationRecord,
    connect,
    init_database,
    is_duplicate_snapshot,
    query_by_week,
    query_latest,
    query_range,
    record_snapshot,
    resolve_device,
    resolve_user,
    update_archive_state,
    upsert_observations,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.sqlite3"
    init_database(p)
    return p


@pytest.fixture()
def conn(db_path: Path):
    with connect(db_path) as c:
        yield c


class TestInitDatabase:
    def test_creates_all_tables(self, db_path: Path):
        with connect(db_path) as c:
            tables = {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert tables >= {
            "schema_meta", "snapshots", "devices", "users",
            "observations", "archive_state",
        }

    def test_sets_schema_version(self, db_path: Path):
        with connect(db_path) as c:
            row = c.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
        assert row[0] == "1"

    def test_idempotent(self, db_path: Path):
        init_database(db_path)  # second call should not error
        with connect(db_path) as c:
            tables = {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert "observations" in tables


class TestDeviceUserResolution:
    def test_resolve_device_creates(self, conn):
        dev_id = resolve_device(
            conn, name="Band 8", identifier="AA:BB:CC",
            type_name="MIBAND8", model="M2239B1",
        )
        assert dev_id == 1

    def test_resolve_device_idempotent(self, conn):
        id1 = resolve_device(
            conn, name="Band 8", identifier="AA:BB:CC",
            type_name="MIBAND8", model="M2239B1",
        )
        id2 = resolve_device(
            conn, name="Band 8", identifier="AA:BB:CC",
            type_name="MIBAND8", model="M2239B1",
        )
        assert id1 == id2

    def test_resolve_user_creates(self, conn):
        uid = resolve_user(conn, name="alice")
        assert uid == 1

    def test_resolve_user_idempotent(self, conn):
        u1 = resolve_user(conn, name="alice")
        u2 = resolve_user(conn, name="alice")
        assert u1 == u2

    def test_different_users_different_ids(self, conn):
        u1 = resolve_user(conn, name="alice")
        u2 = resolve_user(conn, name="bob")
        assert u1 != u2


class TestSnapshotMetadata:
    def test_record_and_check_duplicate(self, conn):
        assert not is_duplicate_snapshot(conn, "abc123")
        record_snapshot(
            conn, snapshot_hash="abc123",
            schema_fingerprint="fp1", status="accepted",
        )
        assert is_duplicate_snapshot(conn, "abc123")

    def test_non_accepted_not_duplicate(self, conn):
        record_snapshot(
            conn, snapshot_hash="xyz",
            schema_fingerprint="fp1", status="unsupported_schema",
        )
        assert not is_duplicate_snapshot(conn, "xyz")


class TestObservationUpsert:
    def _make_obs(self, dedup_key="k1", ts="2026-07-09T10:00:00+00:00"):
        return ObservationRecord(
            dedup_key=dedup_key,
            type="heart_rate",
            timestamp_utc=ts,
            timestamp_local="2026-07-09T18:00:00+08:00",
            value={"bpm": 79},
            raw_source={"HEART_RATE": 79},
            source_table="XIAOMI_ACTIVITY_SAMPLE",
            source_identity="row:1783746120",
            internal_device_id=1,
            internal_user_id=1,
        )

    def test_insert_new(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        result = upsert_observations(conn, [self._make_obs()])
        assert result.new_count == 1
        assert result.duplicate_count == 0

    def test_duplicate_key_skipped(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        upsert_observations(conn, [self._make_obs()])
        result = upsert_observations(conn, [self._make_obs()])
        assert result.new_count == 0
        assert result.duplicate_count == 1


class TestQueryLatest:
    def test_returns_most_recent(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        for ts in ["2026-07-09T10:00:00+00:00", "2026-07-09T12:00:00+00:00",
                    "2026-07-09T11:00:00+00:00"]:
            upsert_observations(conn, [
                ObservationRecord(
                    dedup_key=f"k-{ts}", type="heart_rate",
                    timestamp_utc=ts, timestamp_local=ts,
                    value={"bpm": 80}, raw_source={},
                    source_table="t", source_identity="r",
                    internal_device_id=1, internal_user_id=1,
                )
            ])
        latest = query_latest(conn, obs_type="heart_rate")
        assert latest is not None
        assert latest.timestamp_utc == "2026-07-09T12:00:00+00:00"

    def test_returns_none_when_empty(self, conn):
        assert query_latest(conn, obs_type="heart_rate") is None


class TestQueryRange:
    def test_pagination(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        for i in range(5):
            upsert_observations(conn, [
                ObservationRecord(
                    dedup_key=f"k{i}", type="heart_rate",
                    timestamp_utc=f"2026-07-09T1{i}:00:00+00:00",
                    timestamp_local=f"2026-07-09T1{i}:00:00+00:00",
                    value={"bpm": 80}, raw_source={},
                    source_table="t", source_identity="r",
                    internal_device_id=1, internal_user_id=1,
                )
            ])
        page1 = query_range(conn, obs_type="heart_rate", limit=2)
        assert len(page1.observations) == 2
        assert page1.next_cursor is not None

        page2 = query_range(
            conn, obs_type="heart_rate", limit=2, cursor=page1.next_cursor,
        )
        assert len(page2.observations) == 2
        assert page2.next_cursor is not None

        page3 = query_range(
            conn, obs_type="heart_rate", limit=2, cursor=page2.next_cursor,
        )
        assert len(page3.observations) == 1
        assert page3.next_cursor is None

    def test_time_filter(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        for day in [9, 10, 11]:
            upsert_observations(conn, [
                ObservationRecord(
                    dedup_key=f"k{day}", type="heart_rate",
                    timestamp_utc=f"2026-07-{day:02d}T10:00:00+00:00",
                    timestamp_local=f"2026-07-{day:02d}T10:00:00+00:00",
                    value={"bpm": 80}, raw_source={},
                    source_table="t", source_identity="r",
                    internal_device_id=1, internal_user_id=1,
                )
            ])
        result = query_range(
            conn, obs_type="heart_rate",
            from_ts="2026-07-10T00:00:00+00:00",
            to_ts="2026-07-11T23:59:59+00:00",
            limit=100,
        )
        assert len(result.observations) == 2


class TestQueryByWeek:
    def test_returns_in_range(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        for ts in ["2026-07-06T10:00:00+00:00", "2026-07-09T10:00:00+00:00",
                    "2026-07-13T10:00:00+00:00"]:
            upsert_observations(conn, [
                ObservationRecord(
                    dedup_key=f"k-{ts}", type="heart_rate",
                    timestamp_utc=ts, timestamp_local=ts,
                    value={"bpm": 80}, raw_source={},
                    source_table="t", source_identity="r",
                    internal_device_id=1, internal_user_id=1,
                )
            ])
        result = query_by_week(
            conn,
            week_start_utc="2026-07-06T00:00:00+00:00",
            week_end_utc="2026-07-12T23:59:59+00:00",
        )
        assert len(result) == 2

    def test_filtered_by_type(self, conn):
        resolve_device(conn, name="d", identifier="x", type_name=None, model=None)
        resolve_user(conn, name="u")
        for t in ["heart_rate", "steps"]:
            upsert_observations(conn, [
                ObservationRecord(
                    dedup_key=f"k-{t}", type=t,
                    timestamp_utc="2026-07-09T10:00:00+00:00",
                    timestamp_local="2026-07-09T10:00:00+00:00",
                    value={"v": 1}, raw_source={},
                    source_table="t", source_identity="r",
                    internal_device_id=1, internal_user_id=1,
                )
            ])
        result = query_by_week(
            conn,
            week_start_utc="2026-07-01T00:00:00+00:00",
            week_end_utc="2026-07-31T23:59:59+00:00",
            obs_type="heart_rate",
        )
        assert len(result) == 1
        assert result[0].type == "heart_rate"


class TestArchiveState:
    def test_update_and_read(self, conn):
        update_archive_state(conn, week_id="2026-W28", obs_type="heart_rate")
        row = conn.execute(
            "SELECT * FROM archive_state WHERE week_id=? AND type=?",
            ("2026-W28", "heart_rate"),
        ).fetchone()
        assert row is not None
        assert row["week_id"] == "2026-W28"
