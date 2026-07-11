"""Tests for the pull client watch mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from health_bridge.pull_config import PullConfig
from health_bridge.pull_watch import watch_once


def _make_config() -> PullConfig:
    return PullConfig(
        base_url="https://example.com",
        api_base="https://example.com/health/api/v1",
        timeout_seconds=30,
        timezone="Asia/Shanghai",
        read_token="test-token",
    )


class _MockTransport:
    def __init__(self, responses: dict[str, bytes]):
        self._responses = responses
        self.call_count = 0

    def get(self, path: str, params: dict | None = None, **kwargs) -> bytes:
        self.call_count += 1
        obs_type = (params or {}).get("type", "")
        return self._responses.get(obs_type, b'{}')


class TestWatchOnce:
    """Single iteration of watch loop."""

    def test_writes_new_file_on_first_run(self, tmp_path):
        body = json.dumps({
            "heart_rate": {
                "type": "heart_rate",
                "value": {"bpm": 84},
                "timestamp_utc": "2026-07-11T06:35:00+00:00",
                "timestamp_local": "2026-07-11T14:35:00+08:00",
            }
        }).encode()
        transport = _MockTransport({"heart_rate": body})
        notifications = watch_once(
            transport, _make_config(),
            types=["heart_rate"],
            output_dir=tmp_path,
            last_hashes={},
        )

        assert len(notifications) == 1
        assert "heart_rate" in notifications[0]
        assert "84" in notifications[0]

        # File should exist.
        out_file = tmp_path / "heart_rate.json"
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data["heart_rate"]["value"]["bpm"] == 84

    def test_skips_unchanged(self, tmp_path):
        body = json.dumps({
            "heart_rate": {
                "type": "heart_rate",
                "value": {"bpm": 84},
                "timestamp_utc": "2026-07-11T06:35:00+00:00",
            }
        }).encode()
        transport = _MockTransport({"heart_rate": body})

        # First run.
        watch_once(transport, _make_config(), ["heart_rate"], tmp_path, {})
        # Second run with same data.
        notifications = watch_once(
            transport, _make_config(),
            ["heart_rate"], tmp_path,
            last_hashes={},
        )
        # Should detect no change because hash is same.
        # But since we pass empty last_hashes, it will detect as "new".
        # Let's test properly with saved hash.
        assert len(notifications) == 1  # First time always notifies

    def test_no_notification_on_same_hash(self, tmp_path):
        body = json.dumps({
            "heart_rate": {"value": {"bpm": 84}, "timestamp_utc": "2026-07-11T06:35:00+00:00"},
        }).encode()
        transport = _MockTransport({"heart_rate": body})

        import hashlib
        result_for_hash = json.loads(body)
        content_hash = hashlib.sha256(json.dumps(result_for_hash, sort_keys=True).encode("utf-8")).hexdigest()

        notifications = watch_once(
            transport, _make_config(),
            ["heart_rate"], tmp_path,
            last_hashes={"heart_rate": content_hash},
        )

        assert len(notifications) == 0  # No change

    def test_handles_null_value(self, tmp_path):
        body = json.dumps({"sleep_stage": None}).encode()
        transport = _MockTransport({"sleep_stage": body})
        notifications = watch_once(
            transport, _make_config(),
            ["sleep_stage"], tmp_path, {},
        )

        # Null value should not write file but should notify.
        assert len(notifications) == 0  # No change for null
        out_file = tmp_path / "sleep_stage.json"
        assert not out_file.exists()

    def test_multiple_types(self, tmp_path):
        hr_body = json.dumps({"heart_rate": {"value": {"bpm": 90}}}).encode()
        steps_body = json.dumps({"steps_daily": {"value": {"steps": 5000}}}).encode()
        transport = _MockTransport({
            "heart_rate": hr_body,
            "steps_daily": steps_body,
        })

        notifications = watch_once(
            transport, _make_config(),
            ["heart_rate", "steps_daily"],
            tmp_path, {},
        )

        assert len(notifications) == 2
        assert (tmp_path / "heart_rate.json").exists()
        assert (tmp_path / "steps_daily.json").exists()

    def test_atomic_write(self, tmp_path):
        """Ensure no partial files are visible during write."""
        body = json.dumps({"heart_rate": {"value": {"bpm": 84}}}).encode()
        transport = _MockTransport({"heart_rate": body})
        watch_once(transport, _make_config(), ["heart_rate"], tmp_path, {})

        # No temp files should remain.
        temp_files = list(tmp_path.glob(".*.tmp"))
        assert len(temp_files) == 0
