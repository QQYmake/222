"""Tests for the pull client configuration module."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from health_bridge.pull_config import PullConfig, load_pull_config


class TestPullConfigDefaults:
    """Default values and environment variable overrides."""

    def test_defaults_from_env(self):
        env = {
            "HEALTH_PULL_BASE_URL": "https://example.com",
            "HEALTH_READ_TOKEN": "secret-token",
        }
        cfg = load_pull_config(None, env)
        assert cfg.base_url == "https://example.com"
        assert cfg.api_base == "https://example.com/health/api/v1"
        assert cfg.read_token == "secret-token"
        assert cfg.timeout_seconds == 30
        assert cfg.timezone == "Asia/Shanghai"

    def test_builtin_default_base_url(self):
        env = {"HEALTH_READ_TOKEN": "tok"}
        cfg = load_pull_config(None, env)
        assert cfg.base_url == "https://oh-my-frontweb.duckdns.org"

    def test_timeout_override(self):
        env = {"HEALTH_READ_TOKEN": "tok", "HEALTH_PULL_TIMEOUT": "60"}
        cfg = load_pull_config(None, env)
        assert cfg.timeout_seconds == 60

    def test_timezone_override(self):
        env = {"HEALTH_READ_TOKEN": "tok", "HEALTH_PULL_TIMEZONE": "UTC"}
        cfg = load_pull_config(None, env)
        assert cfg.timezone == "UTC"

    def test_trailing_slash_stripped(self):
        env = {
            "HEALTH_PULL_BASE_URL": "https://example.com/",
            "HEALTH_READ_TOKEN": "tok",
        }
        cfg = load_pull_config(None, env)
        assert cfg.base_url == "https://example.com"
        assert cfg.api_base == "https://example.com/health/api/v1"

    def test_multiple_trailing_slashes(self):
        env = {
            "HEALTH_PULL_BASE_URL": "https://example.com///",
            "HEALTH_READ_TOKEN": "tok",
        }
        cfg = load_pull_config(None, env)
        assert cfg.base_url == "https://example.com"


class TestPullConfigValidation:
    """Configuration validation."""

    def test_missing_token_raises(self):
        env = {"HEALTH_PULL_BASE_URL": "https://example.com"}
        with pytest.raises(ValueError, match="[Rr]ead.*token"):
            load_pull_config(None, env)

    def test_missing_token_ok_in_dry_run(self):
        env = {"HEALTH_PULL_BASE_URL": "https://example.com"}
        cfg = load_pull_config(None, env, dry_run=True)
        assert cfg.read_token == ""

    def test_https_required_in_production(self):
        env = {
            "HEALTH_PULL_BASE_URL": "http://example.com",
            "HEALTH_READ_TOKEN": "tok",
        }
        with pytest.raises(ValueError, match="HTTPS"):
            load_pull_config(None, env)

    def test_http_allowed_with_insecure_flag(self):
        env = {
            "HEALTH_PULL_BASE_URL": "http://localhost:8765",
            "HEALTH_READ_TOKEN": "tok",
        }
        cfg = load_pull_config(None, env, allow_insecure=True)
        assert cfg.base_url == "http://localhost:8765"

    def test_empty_base_url_uses_default(self):
        env = {"HEALTH_READ_TOKEN": "tok"}
        cfg = load_pull_config(None, env)
        assert cfg.base_url == "https://oh-my-frontweb.duckdns.org"


class TestPullConfigFile:
    """Loading from JSON config file."""

    def test_config_file_overrides_defaults(self, tmp_path):
        config_file = tmp_path / "pull-config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://custom.server.com",
            "timeout_seconds": 45,
            "timezone": "America/New_York",
        }))
        env = {"HEALTH_READ_TOKEN": "tok"}
        cfg = load_pull_config(config_file, env)
        assert cfg.base_url == "https://custom.server.com"
        assert cfg.timeout_seconds == 45
        assert cfg.timezone == "America/New_York"

    def test_env_overrides_config_file(self, tmp_path):
        config_file = tmp_path / "pull-config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://from-file.com",
        }))
        env = {
            "HEALTH_PULL_BASE_URL": "https://from-env.com",
            "HEALTH_READ_TOKEN": "tok",
        }
        cfg = load_pull_config(config_file, env)
        assert cfg.base_url == "https://from-env.com"

    def test_token_never_in_config_file(self, tmp_path):
        """Token must come from env, not config file."""
        config_file = tmp_path / "pull-config.json"
        config_file.write_text(json.dumps({
            "base_url": "https://example.com",
            "read_token": "should-be-ignored",
        }))
        env = {"HEALTH_READ_TOKEN": "real-token"}
        cfg = load_pull_config(config_file, env)
        assert cfg.read_token == "real-token"


class TestPullConfigRepr:
    """Token must not appear in repr."""

    def test_token_not_in_repr(self):
        env = {
            "HEALTH_PULL_BASE_URL": "https://example.com",
            "HEALTH_READ_TOKEN": "super-secret",
        }
        cfg = load_pull_config(None, env)
        assert "super-secret" not in repr(cfg)
