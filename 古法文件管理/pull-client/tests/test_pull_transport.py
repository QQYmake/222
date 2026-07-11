"""Tests for the pull client transport layer."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from health_bridge.pull_config import PullConfig
from health_bridge.pull_transport import (
    AuthError,
    NotFoundError,
    PullTransport,
    TransientError,
    TransportError,
)


def _make_config(
    base_url: str = "https://example.com",
    token: str = "test-token",
) -> PullConfig:
    return PullConfig(
        base_url=base_url,
        api_base=base_url + "/health/api/v1",
        timeout_seconds=30,
        timezone="Asia/Shanghai",
        read_token=token,
    )


class _FakeResponse:
    """Simulates an HTTP response."""

    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self._read = False

    def read(self, size: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._body


class _FakeConnection:
    """Simulates an HTTP connection."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.request_method: str | None = None
        self.request_path: str | None = None
        self.request_headers: dict[str, str] = {}

    def putrequest(self, method: str, path: str) -> None:
        self.request_method = method
        self.request_path = path

    def putheader(self, name: str, value: str) -> None:
        self.request_headers[name] = value

    def endheaders(self) -> None:
        pass

    def getresponse(self) -> _FakeResponse:
        return self._response

    def close(self) -> None:
        pass


class TestPullTransportSuccess:
    """Successful HTTP requests."""

    def test_get_json_200(self):
        body = json.dumps({"weeks": ["2026-W28"]}).encode()
        conn = _FakeConnection(_FakeResponse(200, body))
        transport = PullTransport(_make_config())
        result = transport.get("/weeks", connection_factory=lambda: conn)

        assert conn.request_method == "GET"
        assert "/weeks" in conn.request_path
        assert conn.request_headers["Authorization"] == "Bearer test-token"
        assert conn.request_headers["User-Agent"] == "health-bridge-pull/1.0"
        assert result == body

    def test_get_with_query_params(self):
        body = b'{"heart_rate": null}'
        conn = _FakeConnection(_FakeResponse(200, body))
        transport = PullTransport(_make_config())
        result = transport.get(
            "/latest",
            params={"type": "heart_rate"},
            connection_factory=lambda: conn,
        )

        assert result == body
        assert "type=heart_rate" in conn.request_path

    def test_get_text_200(self):
        body = b"# Health Archive 2026-W28\n..."
        conn = _FakeConnection(_FakeResponse(200, body))
        transport = PullTransport(_make_config())
        result = transport.get("/archive/2026-W28", connection_factory=lambda: conn)
        assert result == body


class TestPullTransportErrors:
    """HTTP error handling."""

    def test_401_raises_auth_error(self):
        conn = _FakeConnection(_FakeResponse(401, b'{"detail": "Unauthorized"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(AuthError):
            transport.get("/latest", connection_factory=lambda: conn)

    def test_403_raises_auth_error(self):
        conn = _FakeConnection(_FakeResponse(403, b'{"detail": "Forbidden"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(AuthError):
            transport.get("/latest", connection_factory=lambda: conn)

    def test_404_raises_not_found_error(self):
        conn = _FakeConnection(_FakeResponse(404, b'{"detail": "Not found"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(NotFoundError):
            transport.get("/archive/2099-W01", connection_factory=lambda: conn)

    def test_429_raises_transient_error(self):
        conn = _FakeConnection(_FakeResponse(429, b'{"detail": "Too many requests"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(TransientError):
            transport.get("/latest", connection_factory=lambda: conn)

    def test_500_raises_transient_error(self):
        conn = _FakeConnection(_FakeResponse(500, b'{"detail": "Internal error"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(TransientError):
            transport.get("/latest", connection_factory=lambda: conn)

    def test_503_raises_transient_error(self):
        conn = _FakeConnection(_FakeResponse(503, b'{"detail": "Service unavailable"}'))
        transport = PullTransport(_make_config())
        with pytest.raises(TransientError):
            transport.get("/latest", connection_factory=lambda: conn)

    def test_unknown_status_raises_transport_error(self):
        conn = _FakeConnection(_FakeResponse(418, b"I'm a teapot"))
        transport = PullTransport(_make_config())
        with pytest.raises(TransportError):
            transport.get("/latest", connection_factory=lambda: conn)


class TestPullTransportTokenSafety:
    """Token must not appear in error messages."""

    def test_token_not_in_error_message(self):
        conn = _FakeConnection(_FakeResponse(401, b'{"detail": "Unauthorized"}'))
        transport = PullTransport(_make_config(token="super-secret-123"))
        try:
            transport.get("/latest", connection_factory=lambda: conn)
            assert False, "Should have raised"
        except AuthError as exc:
            assert "super-secret-123" not in str(exc)


class TestPullTransportBoundedRead:
    """Response body must be bounded to prevent memory overflow."""

    def test_large_response_raises(self):
        # 2 MB body, max is 1 MB.
        big_body = b"x" * (2 * 1024 * 1024)
        conn = _FakeConnection(_FakeResponse(200, big_body))
        transport = PullTransport(_make_config())
        with pytest.raises(TransportError, match="exceed"):
            transport.get("/latest", connection_factory=lambda: conn)
