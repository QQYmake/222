"""Tests for archive and latest generators."""

import json
import sqlite3
from pathlib import Path

import pytest

from app.config import ServerConfig, load_server_config
from app.database import init_database, connect
from app.archive import (
    generate_archive,
    list_archives,
    read_archive,
    _week_range,
)
from app.latest import generate_latest, read_latest


@pytest.fixture
def server_config(tmp_path):
    data = tmp_path / "data"
    cfg = load_server_config(
        {
            "HEALTH_BRIDGE_ARCHIVES_DIR": str(data / "archives"),
            "HEALTH_BRIDGE_LATEST_DIR": str(data / "latest"),
        },
        data_dir=data,
        upload_token="test-upload",
        read_token="test-read",
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    init_database(cfg.db_path)
    return cfg


@pytest.fixture
def populated_db(server_config):
    """Insert sample observations into the DB."""
    with connect(server_config.db_path) as conn:
        # Insert device and user (FK requirement).
        conn.execute(
            "INSERT INTO devices (name, identifier, type_name, model, first_seen) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Mi Band 8", "dev-001", "MI_BAND_8", "Xiaomi Smart Band 8",
             "2026-07-08T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO users (name, first_seen) VALUES (?, ?)",
            ("default", "2026-07-08T00:00:00+00:00"),
        )
        # Insert a snapshot record first (FK requirement).
        conn.execute(
            "INSERT INTO snapshots (hash, received_at, schema_fingerprint, status, "
            "new_count, duplicate_count, raw_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abc123", "2026-07-09T06:00:00+00:00", "fp1", "accepted", 3, 0, "/tmp/raw.db"),
        )
        # Insert observations for week 2026-W28 (July 6-12, 2026).
        obs = [
            ("k1", "heart_rate", "2026-07-08T05:02:00+00:00",
             "2026-07-08T13:02:00+08:00", '"72"',
             "{}", "XIAOMI_ACTIVITY_SAMPLE", "hr-1", 1, 1, "abc123"),
            ("k2", "heart_rate", "2026-07-09T06:00:00+00:00",
             "2026-07-09T14:00:00+08:00", '"68"',
             "{}", "XIAOMI_ACTIVITY_SAMPLE", "hr-2", 1, 1, "abc123"),
            ("k3", "steps", "2026-07-09T06:00:00+00:00",
             "2026-07-09T14:00:00+08:00", '"1234"',
             "{}", "XIAOMI_ACTIVITY_SAMPLE", "st-1", 1, 1, "abc123"),
        ]
        conn.executemany(
            "INSERT INTO observations "
            "(dedup_key, type, timestamp_utc, timestamp_local, value, "
            "raw_source, source_table, source_identity, "
            "internal_device_id, internal_user_id, snapshot_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            obs,
        )
    return server_config


class TestWeekRange:
    def test_week_28_2026(self):
        start, end = _week_range("2026-W28")
        assert start == "2026-07-06T00:00:00+00:00"
        assert end == "2026-07-13T00:00:00+00:00"

    def test_week_1_2026(self):
        start, end = _week_range("2026-W01")
        assert start == "2025-12-29T00:00:00+00:00"
        assert end == "2026-01-05T00:00:00+00:00"

    def test_invalid_week_raises(self):
        with pytest.raises(ValueError):
            _week_range("invalid")


class TestGenerateArchive:
    def test_generates_markdown_file(self, populated_db):
        path = generate_archive("2026-W28", populated_db)
        assert path.exists()
        assert path.suffix == ".md"
        assert path.name == "2026-W28.md"

    def test_markdown_contains_heart_rate_data(self, populated_db):
        path = generate_archive("2026-W28", populated_db)
        content = path.read_text()
        assert "Heart Rate" in content
        assert "72" in content
        assert "68" in content

    def test_markdown_contains_steps_data(self, populated_db):
        path = generate_archive("2026-W28", populated_db)
        content = path.read_text()
        assert "Steps" in content
        assert "1234" in content

    def test_markdown_has_header_and_disclaimer(self, populated_db):
        path = generate_archive("2026-W28", populated_db)
        content = path.read_text()
        assert "# Health Archive — 2026-W28" in content
        assert "Disclaimer" in content
        assert "not a medical diagnosis" in content

    def test_empty_week_generates_no_data(self, populated_db):
        path = generate_archive("2026-W27", populated_db)
        assert path.exists()
        content = path.read_text()
        assert "No Data" in content

    def test_atomic_write_replaces_existing(self, populated_db):
        generate_archive("2026-W28", populated_db)
        path = generate_archive("2026-W28", populated_db)
        assert path.exists()


class TestListArchives:
    def test_empty_returns_empty_list(self, server_config):
        assert list_archives(server_config) == []

    def test_lists_generated_archives(self, populated_db):
        generate_archive("2026-W27", populated_db)
        generate_archive("2026-W28", populated_db)
        weeks = list_archives(populated_db)
        assert "2026-W27" in weeks
        assert "2026-W28" in weeks
        assert weeks == sorted(weeks)


class TestReadArchive:
    def test_returns_content_if_exists(self, populated_db):
        generate_archive("2026-W28", populated_db)
        content = read_archive("2026-W28", populated_db)
        assert content is not None
        assert "2026-W28" in content

    def test_returns_none_if_not_found(self, populated_db):
        content = read_archive("1999-W01", populated_db)
        assert content is None


class TestGenerateLatest:
    def test_returns_dict_with_all_types(self, populated_db):
        result = generate_latest(populated_db)
        assert "heart_rate" in result
        assert "steps" in result
        assert "steps_daily" in result
        assert "sleep_stage" in result

    def test_heart_rate_is_most_recent(self, populated_db):
        result = generate_latest(populated_db)
        hr = result["heart_rate"]
        assert hr is not None
        assert hr["value"] == "68"  # the later one

    def test_unpopulated_type_is_null(self, populated_db):
        result = generate_latest(populated_db)
        assert result["sleep_stage"] is None
        assert result["steps_daily"] is None

    def test_writes_json_file(self, populated_db):
        generate_latest(populated_db)
        latest_path = populated_db.latest_dir / "latest.json"
        assert latest_path.exists()
        data = json.loads(latest_path.read_text())
        assert data["heart_rate"]["value"] == "68"

    def test_read_latest_from_disk(self, populated_db):
        generate_latest(populated_db)
        data = read_latest(populated_db)
        assert data is not None
        assert data["heart_rate"]["value"] == "68"

    def test_read_latest_returns_none_if_missing(self, server_config):
        assert read_latest(server_config) is None


class TestEmptyDB:
    def test_generate_latest_all_null(self, server_config):
        result = generate_latest(server_config)
        for v in result.values():
            assert v is None
