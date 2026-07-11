"""Tests for the pull client command handlers."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from clients.health_bridge.pull_config import PullConfig
from clients.health_bridge.pull_commands import (
    cmd_latest,
    cmd_range,
    cmd_weeks,
    cmd_archive,
    RangeResult,
)


def _make_config() -> PullConfig:
    return PullConfig(
        base_url="https://example.com",
        api_base="https://example.com/health/api/v1",
        timeout_seconds=30,
        timezone="Asia/Shanghai",
        read_token="test-token",
    )


class _MockTransport:
    """Mock transport that returns canned responses."""

    def __init__(self, response_body: bytes):
        self._response_body = response_body
        self.last_path: str | None = None
        self.last_params: dict | None = None

    def get(self, path: str, params: dict | None = None, **kwargs) -> bytes:
        self.last_path = path
        self.last_params = params
        return self._response_body


class TestCmdLatest:
    """latest command handler."""

    def test_latest_all_types(self):
        body = json.dumps({
            "heart_rate": {"type": "heart_rate", "value": {"bpm": 84}},
            "steps": None,
            "steps_daily": {"type": "steps_daily", "value": {"steps": 100}},
            "sleep_stage": None,
        }).encode()
        transport = _MockTransport(body)
        result = cmd_latest(transport, _make_config(), None)

        assert transport.last_path == "/latest"
        assert transport.last_params is None
        assert result["heart_rate"]["value"]["bpm"] == 84
        assert result["steps"] is None
        assert result["steps_daily"]["value"]["steps"] == 100

    def test_latest_single_type(self):
        body = json.dumps({
            "heart_rate": {"type": "heart_rate", "value": {"bpm": 84}},
        }).encode()
        transport = _MockTransport(body)
        result = cmd_latest(transport, _make_config(), "heart_rate")

        assert transport.last_params == {"type": "heart_rate"}
        assert result["heart_rate"]["value"]["bpm"] == 84

    def test_latest_type_not_found(self):
        body = json.dumps({"sleep_stage": None}).encode()
        transport = _MockTransport(body)
        result = cmd_latest(transport, _make_config(), "sleep_stage")
        assert result["sleep_stage"] is None


class TestCmdRange:
    """range command handler."""

    def test_range_basic(self):
        body = json.dumps({
            "observations": [
                {"type": "heart_rate", "timestamp_utc": "2026-07-11T05:00:00+00:00",
                 "timestamp_local": "2026-07-11T13:00:00+08:00",
                 "value": {"bpm": 79}, "source_table": "XIAOMI_ACTIVITY_SAMPLE",
                 "source_identity": "row:123"},
            ],
            "next_cursor": None,
        }).encode()
        transport = _MockTransport(body)
        result = cmd_range(transport, _make_config(), "heart_rate")

        assert transport.last_path == "/data"
        assert transport.last_params["type"] == "heart_rate"
        assert len(result.observations) == 1
        assert result.observations[0]["value"]["bpm"] == 79
        assert result.next_cursor is None
        assert result.has_more is False

    def test_range_with_from_to(self):
        body = json.dumps({"observations": [], "next_cursor": None}).encode()
        transport = _MockTransport(body)
        cmd_range(
            transport, _make_config(), "heart_rate",
            from_ts="2026-07-01T00:00:00+08:00",
            to_ts="2026-07-08T00:00:00+08:00",
            limit=50,
        )

        assert transport.last_params["from"] == "2026-07-01T00:00:00+08:00"
        assert transport.last_params["to"] == "2026-07-08T00:00:00+08:00"
        assert transport.last_params["limit"] == "50"

    def test_range_with_cursor(self):
        body = json.dumps({"observations": [], "next_cursor": "abc123"}).encode()
        transport = _MockTransport(body)
        result = cmd_range(
            transport, _make_config(), "heart_rate",
            cursor="next-page",
        )

        assert transport.last_params["cursor"] == "next-page"
        assert result.next_cursor == "abc123"
        assert result.has_more is True


class TestCmdWeeks:
    """weeks command handler."""

    def test_weeks_list(self):
        body = json.dumps({"weeks": ["2026-W27", "2026-W28"]}).encode()
        transport = _MockTransport(body)
        result = cmd_weeks(transport, _make_config())

        assert transport.last_path == "/weeks"
        assert result == ["2026-W27", "2026-W28"]

    def test_weeks_empty(self):
        body = json.dumps({"weeks": []}).encode()
        transport = _MockTransport(body)
        result = cmd_weeks(transport, _make_config())
        assert result == []


class TestCmdArchive:
    """archive command handler."""

    def test_archive_markdown(self):
        body = b"# Health Archive 2026-W28\n\n## Heart Rate\n..."
        transport = _MockTransport(body)
        result = cmd_archive(transport, _make_config(), "2026-W28")

        assert transport.last_path == "/archive/2026-W28"
        assert result == body.decode("utf-8")
        assert "2026-W28" in result

    def test_archive_includes_disclaimer(self):
        body = b"# Health Archive\n\n> Disclaimer: not medical advice"
        transport = _MockTransport(body)
        result = cmd_archive(transport, _make_config(), "2026-W28")
        assert "Disclaimer" in result
