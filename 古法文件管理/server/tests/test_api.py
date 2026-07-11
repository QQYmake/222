"""Tests for FastAPI endpoints and auth."""

import gzip
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_server_config
from app.database import init_database
from app.main import app, get_config


@pytest.fixture
def server_config(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("HEALTH_BRIDGE_DATA_DIR", str(data))
    monkeypatch.setenv("HEALTH_BRIDGE_ARCHIVES_DIR", str(data / "archives"))
    monkeypatch.setenv("HEALTH_BRIDGE_LATEST_DIR", str(data / "latest"))
    monkeypatch.setenv("HEALTH_BRIDGE_UPLOAD_TOKEN", "test-upload-token")
    monkeypatch.setenv("HEALTH_BRIDGE_READ_TOKEN", "test-read-token")

    cfg = load_server_config()
    data.mkdir(parents=True, exist_ok=True)
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    cfg.archives_dir.mkdir(parents=True, exist_ok=True)
    cfg.latest_dir.mkdir(parents=True, exist_ok=True)
    init_database(cfg.db_path)

    # Override the global config singleton.
    import app.main as main_mod
    main_mod._config = cfg
    return cfg


@pytest.fixture
def client(server_config):
    return TestClient(app)


@pytest.fixture
def real_gzip_data():
    db_path = Path("/tmp/Gadgetbridge.db")
    if not db_path.exists():
        pytest.skip("Real DB not available")
    return gzip.compress(db_path.read_bytes())


# ---------------------------------------------------------------------------
# 1. Health check (no auth)
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_returns_ok(self, client):
        resp = client.get("/health/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_no_token_required(self, client):
        resp = client.get("/health/api/v1/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 2. Upload endpoint
# ---------------------------------------------------------------------------

class TestUpload:
    def test_missing_token_returns_401(self, client, real_gzip_data):
        resp = client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
        )
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, client, real_gzip_data):
        resp = client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_valid_upload_succeeds(self, client, real_gzip_data):
        resp = client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "uploaded"
        assert data["is_new"] is True
        assert data["new_count"] > 0
        assert len(data["snapshot_hash"]) == 64

    def test_duplicate_upload_returns_duplicate(self, client, real_gzip_data):
        # First upload.
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        # Second upload of same data.
        resp = client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "duplicate"
        assert data["new_count"] == 0

    def test_invalid_gzip_returns_422(self, client):
        resp = client.post(
            "/health/api/v1/upload",
            files={"file": ("bad.gz", b"not-gzip-data", "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 3. Latest endpoint
# ---------------------------------------------------------------------------

class TestLatest:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/health/api/v1/latest")
        assert resp.status_code == 401

    def test_wrong_token_returns_403(self, client):
        resp = client.get(
            "/health/api/v1/latest",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 403

    def test_returns_all_types(self, client, real_gzip_data):
        # Upload first to have data.
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/latest",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "heart_rate" in data
        assert "steps" in data

    def test_filter_by_type(self, client, real_gzip_data):
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/latest?type=heart_rate",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "heart_rate" in data
        assert "steps" not in data

    def test_empty_db_returns_nulls(self, client):
        resp = client.get(
            "/health/api/v1/latest",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["heart_rate"] is None


# ---------------------------------------------------------------------------
# 4. Data range query
# ---------------------------------------------------------------------------

class TestData:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/health/api/v1/data?type=heart_rate")
        assert resp.status_code == 401

    def test_returns_observations(self, client, real_gzip_data):
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/data?type=heart_rate&limit=10",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "observations" in data
        assert len(data["observations"]) > 0
        assert data["observations"][0]["type"] == "heart_rate"

    def test_pagination_with_limit(self, client, real_gzip_data):
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/data?type=heart_rate&limit=2",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["observations"]) <= 2


# ---------------------------------------------------------------------------
# 5. Weeks listing
# ---------------------------------------------------------------------------

class TestWeeks:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/health/api/v1/weeks")
        assert resp.status_code == 401

    def test_returns_weeks_after_upload(self, client, real_gzip_data):
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/weeks",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "weeks" in data
        assert len(data["weeks"]) > 0


# ---------------------------------------------------------------------------
# 6. Archive reading
# ---------------------------------------------------------------------------

class TestArchive:
    def test_missing_token_returns_401(self, client):
        resp = client.get("/health/api/v1/archive/2026-W28")
        assert resp.status_code == 401

    def test_returns_markdown(self, client, real_gzip_data):
        client.post(
            "/health/api/v1/upload",
            files={"file": ("test.db.gz", real_gzip_data, "application/gzip")},
            headers={"X-Upload-Token": "test-upload-token"},
        )
        resp = client.get(
            "/health/api/v1/archive/2026-W28",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert "2026-W28" in resp.text

    def test_404_for_missing_week(self, client):
        resp = client.get(
            "/health/api/v1/archive/1999-W01",
            headers={"Authorization": "Bearer test-read-token"},
        )
        assert resp.status_code == 404
