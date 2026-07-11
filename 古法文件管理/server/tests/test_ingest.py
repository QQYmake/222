"""Tests for the ingest pipeline."""

from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

import pytest

from app.config import load_server_config
from app.ingest import IngestError, IngestStatus, IngestResponse, ingest_snapshot

REAL_DB = Path("/tmp/Gadgetbridge.db")


@pytest.fixture()
def server_config(tmp_path: Path):
    return load_server_config(
        {},
        data_dir=tmp_path / "data",
        upload_token="test-upload",
        read_token="test-read",
    )


@pytest.fixture()
def real_gzip_data():
    raw = REAL_DB.read_bytes()
    return gzip.compress(raw)


class TestIngestValidation:
    def test_invalid_gzip_raises(self, server_config):
        with pytest.raises(IngestError, match="Invalid gzip"):
            ingest_snapshot(b"not gzip data", server_config)

    def test_non_sqlite_raises(self, server_config):
        bad_data = gzip.compress(b"not a sqlite file at all")
        with pytest.raises(IngestError, match="Not a valid SQLite file"):
            ingest_snapshot(bad_data, server_config)

    def test_corrupted_sqlite_raises(self, tmp_path, server_config):
        # Create a valid SQLite DB with enough data, then corrupt it.
        db = tmp_path / "corrupt.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
        conn.executemany(
            "INSERT INTO t VALUES (?, ?)",
            [(i, f"row-{i}" * 100) for i in range(200)],
        )
        conn.commit()
        conn.close()
        data = bytearray(db.read_bytes())
        # Corrupt the B-tree page header area (offset 100+).
        if len(data) > 120:
            data[100:116] = b"\xff" * 16
        bad_gzip = gzip.compress(bytes(data))
        with pytest.raises(IngestError, match="integrity|SQLite"):
            ingest_snapshot(bad_gzip, server_config)


@pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
class TestIngestRealDB:
    def test_successful_ingest(self, server_config, real_gzip_data):
        response = ingest_snapshot(real_gzip_data, server_config)
        assert response.status == IngestStatus.UPLOADED
        assert response.is_new is True
        assert response.new_count > 0
        assert len(response.snapshot_hash) == 64
        assert response.schema_fingerprint  # non-empty
        assert len(response.affected_weeks) > 0

    def test_duplicate_ingest_skipped(self, server_config, real_gzip_data):
        # First ingest.
        first = ingest_snapshot(real_gzip_data, server_config)
        assert first.is_new is True

        # Second ingest of same data → duplicate.
        second = ingest_snapshot(real_gzip_data, server_config)
        assert second.status == IngestStatus.DUPLICATE
        assert second.is_new is False
        assert second.snapshot_hash == first.snapshot_hash

    def test_observations_in_database(self, server_config, real_gzip_data):
        ingest_snapshot(real_gzip_data, server_config)

        from app.database import connect, query_latest

        with connect(server_config.db_path) as conn:
            # Heart rate observations should exist.
            latest_hr = query_latest(conn, obs_type="heart_rate")
            assert latest_hr is not None
            assert "bpm" in latest_hr.value

    def test_raw_snapshot_preserved(self, server_config, real_gzip_data):
        ingest_snapshot(real_gzip_data, server_config)
        raw_files = list(server_config.raw_dir.glob("*.db"))
        assert len(raw_files) == 1

    def test_re_ingest_same_data_no_duplicates(self, server_config, real_gzip_data):
        """Re-ingesting the same DB after it's already accepted should
        return duplicate status and not add new observations."""
        first = ingest_snapshot(real_gzip_data, server_config)
        assert first.new_count > 0

        second = ingest_snapshot(real_gzip_data, server_config)
        assert second.status == IngestStatus.DUPLICATE
        assert second.new_count == 0

        # Verify DB doesn't have double entries.
        from app.database import connect

        with connect(server_config.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            assert total == first.new_count  # same as first ingest


@pytest.mark.skipif(not REAL_DB.exists(), reason="Real DB not available")
class TestIngestUnsupportedSchema:
    def test_unsupported_schema_preserved(self, tmp_path, server_config):
        # Create a DB without required tables.
        db = tmp_path / "unsupported.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE something_else (x INTEGER)")
        conn.execute("INSERT INTO something_else VALUES (1)")
        conn.commit()
        conn.close()
        gzip_data = gzip.compress(db.read_bytes())

        response = ingest_snapshot(gzip_data, server_config)
        assert response.status == IngestStatus.UNSUPPORTED_SCHEMA
        assert response.is_new is True
        # Raw snapshot should still be preserved.
        assert response.raw_path is not None
        assert Path(response.raw_path).exists()
