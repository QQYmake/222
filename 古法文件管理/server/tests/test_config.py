"""Tests for server configuration."""

from __future__ import annotations

from pathlib import Path

from app.config import ServerConfig, load_server_config


class TestServerConfigDefaults:
    def test_default_paths(self):
        cfg = load_server_config({})
        assert cfg.data_dir == Path("/srv/health-bridge/data")
        assert cfg.raw_dir == Path("/srv/health-bridge/data/raw")
        assert cfg.incoming_dir == Path("/srv/health-bridge/data/incoming")
        assert cfg.db_path == Path("/srv/health-bridge/data/health.sqlite3")
        assert cfg.archives_dir == Path("/srv/health-bridge/archives")
        assert cfg.latest_dir == Path("/srv/health-bridge/latest")

    def test_default_listen(self):
        cfg = load_server_config({})
        assert cfg.listen_host == "127.0.0.1"
        assert cfg.listen_port == 8765

    def test_default_limits(self):
        cfg = load_server_config({})
        assert cfg.max_decompressed_bytes == 104_857_600
        assert cfg.max_body_bytes == 104_857_600

    def test_tokens_default_none(self):
        cfg = load_server_config({})
        assert cfg.upload_token is None
        assert cfg.read_token is None


class TestServerConfigFromEnv:
    def test_data_dir_from_env(self):
        cfg = load_server_config({"HEALTH_BRIDGE_DATA_DIR": "/tmp/hb-test"})
        assert cfg.data_dir == Path("/tmp/hb-test")
        assert cfg.db_path == Path("/tmp/hb-test/health.sqlite3")

    def test_tokens_from_env(self):
        cfg = load_server_config({
            "HEALTH_BRIDGE_UPLOAD_TOKEN": "secret-upload",
            "HEALTH_BRIDGE_READ_TOKEN": "secret-read",
        })
        assert cfg.upload_token == "secret-upload"
        assert cfg.read_token == "secret-read"

    def test_listen_from_env(self):
        cfg = load_server_config({
            "HEALTH_BRIDGE_LISTEN_HOST": "0.0.0.0",
            "HEALTH_BRIDGE_LISTEN_PORT": "9999",
        })
        assert cfg.listen_host == "0.0.0.0"
        assert cfg.listen_port == 9999

    def test_explicit_args_override_env(self):
        cfg = load_server_config(
            {"HEALTH_BRIDGE_DATA_DIR": "/from-env"},
            data_dir="/from-arg",
            upload_token="from-arg-token",
        )
        assert cfg.data_dir == Path("/from-arg")
        assert cfg.upload_token == "from-arg-token"


class TestTokenSafety:
    def test_token_not_in_repr(self):
        cfg = load_server_config(upload_token="super-secret-value")
        assert "super-secret-value" not in repr(cfg)

    def test_token_not_in_repr_read(self):
        cfg = load_server_config(read_token="super-secret-read")
        assert "super-secret-read" not in repr(cfg)
