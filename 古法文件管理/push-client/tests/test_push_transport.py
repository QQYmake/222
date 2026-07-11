"""Tests for push_transport — streaming HTTPS multipart transport.

Written before implementation, following strict TDD.  Uses injected fake
connection objects; no network access.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from health_bridge.push_config import PushConfig
from health_bridge.push_snapshot import PreparedSnapshot
from health_bridge.push_transport import (
    PermanentUploadError,
    TransientUploadError,
    UploadResult,
    upload_snapshot,
)

_LEAK_PROBE = "LEAK-PROBE-zzz999zzz"


# ── helpers ──────────────────────────────────────────────────────────


def _make_config(
    *,
    upload_url: str = "https://upload.example.com/api/v1/upload",
    chunk_size: int = 64,
    max_response_bytes: int = 1_048_576,
    request_timeout_seconds: float = 120,
    upload_token: str = "test-token-abc123",
) -> PushConfig:
    return PushConfig(
        source_path=Path("/tmp/source.db"),
        upload_url=upload_url,
        state_path=Path("/tmp/state.json"),
        poll_interval_seconds=900,
        stability_delay_seconds=5,
        request_timeout_seconds=request_timeout_seconds,
        max_retries=5,
        max_uncompressed_bytes=104_857_600,
        chunk_size=chunk_size,
        max_response_bytes=max_response_bytes,
        token_env="HEALTH_UPLOAD_TOKEN",
        token_file=None,
        upload_token=upload_token,
    )


def _make_snapshot(tmpdir: Path, content: bytes = b"test database content") -> PreparedSnapshot:
    """Build a PreparedSnapshot whose gzip_path points to a real gzip file."""
    gzip_path = tmpdir / "snapshot.db.gz"
    with gzip.open(gzip_path, "wb") as f:
        f.write(content)
    return PreparedSnapshot(
        source_path=tmpdir / "source.db",
        staged_db_path=tmpdir / "staged.db",
        gzip_path=gzip_path,
        sha256=hashlib.sha256(content).hexdigest(),
        uncompressed_bytes=len(content),
        compressed_bytes=gzip_path.stat().st_size,
    )


class FakeResponse:
    """Minimal stand-in for http.client.HTTPResponse."""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body
        self._pos = 0
        self.total_read = 0
        self._closed = False

    def read(self, n: int | None = None) -> bytes:
        if self._closed:
            return b""
        if n is None or n < 0:
            result = self._body[self._pos :]
            self._pos = len(self._body)
        else:
            result = self._body[self._pos : self._pos + n]
            self._pos += len(result)
        self.total_read += len(result)
        return result

    def close(self) -> None:
        self._closed = True


class InfiniteFakeResponse:
    """Returns data on every read() — never signals EOF."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.total_read = 0

    def read(self, n: int | None = None) -> bytes:
        if n is None or n <= 0:
            n = 8192
        self.total_read += n
        return b"x" * n

    def close(self) -> None:
        pass


class FakeConnection:
    """Records all interactions for assertion."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.putrequest_calls: list[tuple[str, str]] = []
        self.putheader_calls: list[tuple[str, str]] = []
        self.sent_data: list[bytes] = []
        self.endheaders_called = False
        self.getresponse_called = False
        self.closed = False

    def putrequest(self, method: str, url: str) -> None:
        self.putrequest_calls.append((method, url))

    def putheader(self, name: str, value: str) -> None:
        self.putheader_calls.append((name, value))

    def endheaders(self) -> None:
        self.endheaders_called = True

    def send(self, data: bytes) -> None:
        self.sent_data.append(data)

    def getresponse(self) -> Any:
        self.getresponse_called = True
        return self._response

    def close(self) -> None:
        self.closed = True


def _factory(response: Any) -> Any:
    """Return a connection_factory that always yields a FakeConnection."""
    conn_holder: list[FakeConnection] = []

    def make_conn() -> FakeConnection:
        c = FakeConnection(response)
        conn_holder.append(c)
        return c

    make_conn._holder = conn_holder  # type: ignore[attr-defined]
    return make_conn


# ── test base ────────────────────────────────────────────────────────


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ── URL parsing ──────────────────────────────────────────────────────


class TestURLParsing(_TempDirCase):
    def test_rejects_http_url(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_url="http://insecure.example.com/api")
        with self.assertRaises(ValueError):
            upload_snapshot(config, snap, connection_factory=_factory(FakeResponse(200)))

    def test_accepts_https_url(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_url="https://secure.example.com/api")
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        result = upload_snapshot(config, snap, connection_factory=_factory(resp))
        self.assertTrue(result.delivered)


class TestRequestPath(_TempDirCase):
    def test_path_includes_query_string(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(
            upload_url="https://host.example.com/api/v1/upload?key=abc&mode=fast"
        )
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(config, snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]
        method, path = conn.putrequest_calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/v1/upload?key=abc&mode=fast")

    def test_path_without_query_string(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_url="https://host.example.com/api/v1/upload")
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(config, snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]
        _, path = conn.putrequest_calls[0]
        self.assertEqual(path, "/api/v1/upload")


# ── headers ──────────────────────────────────────────────────────────


class TestHeaders(_TempDirCase):
    def test_required_headers_present(self) -> None:
        snap = _make_snapshot(self.dir, content=b"A" * 200)
        config = _make_config(
            upload_url="https://host.example.com/api",
            upload_token="secret-token-xyz",
        )
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(config, snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]

        headers = dict(conn.putheader_calls)

        self.assertIn("X-Upload-Token", headers)
        self.assertEqual(headers["X-Upload-Token"], "secret-token-xyz")

        self.assertIn("Content-Type", headers)
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))

        self.assertIn("Content-Length", headers)
        # Content-Length must be an exact integer, not a guess
        total_sent = sum(len(d) for d in conn.sent_data)
        self.assertEqual(int(headers["Content-Length"]), total_sent)

        self.assertIn("Content-Encoding", headers)
        self.assertEqual(headers["Content-Encoding"], "gzip")

        self.assertIn("X-Snapshot-SHA256", headers)
        self.assertEqual(headers["X-Snapshot-SHA256"], snap.sha256)

        self.assertIn("User-Agent", headers)
        # User-Agent must not contain the token
        self.assertNotIn("secret-token-xyz", headers["User-Agent"])


# ── chunked sending ──────────────────────────────────────────────────


class TestChunkedSending(_TempDirCase):
    def test_file_sent_in_chunks(self) -> None:
        # Random data does not compress, so the gzip output will be
        # larger than chunk_size and span multiple send() calls.
        import os as _os
        content = _os.urandom(500)
        snap = _make_snapshot(self.dir, content=content)
        config = _make_config(chunk_size=64)
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(config, snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]

        gzip_data = snap.gzip_path.read_bytes()

        # The gzip file content should be split across multiple send() calls.
        # sent_data[0] is the preamble, sent_data[-1] is the epilogue,
        # and the middle calls are file chunks.
        file_chunks = conn.sent_data[1:-1]
        self.assertGreater(len(file_chunks), 1, "File should be sent in multiple chunks")

        # Each chunk except the last should be exactly chunk_size
        for chunk in file_chunks[:-1]:
            self.assertEqual(len(chunk), 64)

        # Reassembled file content must match the gzip
        reassembled = b"".join(file_chunks)
        self.assertEqual(reassembled, gzip_data)

    def test_total_sent_matches_content_length(self) -> None:
        snap = _make_snapshot(self.dir, content=b"Y" * 300)
        config = _make_config(chunk_size=128)
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(config, snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]

        total = sum(len(d) for d in conn.sent_data)
        headers = dict(conn.putheader_calls)
        self.assertEqual(int(headers["Content-Length"]), total)


# ── success responses ────────────────────────────────────────────────


class TestSuccessResponse(_TempDirCase):
    def test_http_200_ok(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        result = upload_snapshot(
            _make_config(), snap, connection_factory=_factory(resp)
        )
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.delivered)
        self.assertEqual(result.response["status"], "ok")

    def test_http_201_created(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(201, json.dumps({"status": "ok"}).encode())
        result = upload_snapshot(
            _make_config(), snap, connection_factory=_factory(resp)
        )
        self.assertEqual(result.http_status, 201)
        self.assertTrue(result.delivered)

    def test_http_200_default_status_when_missing(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(200, json.dumps({}).encode())
        result = upload_snapshot(
            _make_config(), snap, connection_factory=_factory(resp)
        )
        self.assertTrue(result.delivered)


# ── HTTP 202 unsupported_schema ─────────────────────────────────────


class TestAcceptedWithAttention(_TempDirCase):
    def test_202_unsupported_schema_delivered(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(
            202, json.dumps({"status": "unsupported_schema"}).encode()
        )
        result = upload_snapshot(
            _make_config(), snap, connection_factory=_factory(resp)
        )
        self.assertEqual(result.http_status, 202)
        self.assertEqual(result.status, "unsupported_schema")
        self.assertTrue(result.delivered)


# ── permanent failures ───────────────────────────────────────────────


class TestPermanentFailure(_TempDirCase):
    def _run_status(self, status: int) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(status, json.dumps({"error": "denied"}).encode())
        with self.assertRaises(PermanentUploadError):
            upload_snapshot(
                _make_config(), snap, connection_factory=_factory(resp)
            )

    def test_401_unauthorized(self) -> None:
        self._run_status(401)

    def test_403_forbidden(self) -> None:
        self._run_status(403)

    def test_413_payload_too_large(self) -> None:
        self._run_status(413)

    def test_422_unprocessable(self) -> None:
        self._run_status(422)


# ── transient failures ───────────────────────────────────────────────


class TestTransientFailure(_TempDirCase):
    def _run_status(self, status: int) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(status, json.dumps({"error": "retry"}).encode())
        with self.assertRaises(TransientUploadError):
            upload_snapshot(
                _make_config(), snap, connection_factory=_factory(resp)
            )

    def test_408_timeout(self) -> None:
        self._run_status(408)

    def test_429_too_many_requests(self) -> None:
        self._run_status(429)

    def test_500_internal_server_error(self) -> None:
        self._run_status(500)

    def test_502_bad_gateway(self) -> None:
        self._run_status(502)

    def test_503_service_unavailable(self) -> None:
        self._run_status(503)


# ── malformed / oversized responses ─────────────────────────────────


class TestMalformedResponse(_TempDirCase):
    def test_invalid_json_raises_permanent_error(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(200, b"this is not json <<<")
        with self.assertRaises(PermanentUploadError) as ctx:
            upload_snapshot(
                _make_config(), snap, connection_factory=_factory(resp)
            )
        # Error message must be bounded and not contain raw body
        self.assertLess(len(str(ctx.exception)), 200)

    def test_non_object_json_raises_permanent_error(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(200, b"[1, 2, 3]")
        with self.assertRaises(PermanentUploadError):
            upload_snapshot(
                _make_config(), snap, connection_factory=_factory(resp)
            )


class TestOversizedResponse(_TempDirCase):
    def test_oversized_body_raises_error(self) -> None:
        snap = _make_snapshot(self.dir)
        # Response that never returns EOF and keeps producing data
        resp = InfiniteFakeResponse(200)
        config = _make_config(max_response_bytes=100, chunk_size=64)
        with self.assertRaises(PermanentUploadError):
            upload_snapshot(config, snap, connection_factory=_factory(resp))
        # Must not have read an unbounded amount
        self.assertLessEqual(resp.total_read, config.max_response_bytes + config.chunk_size)

    def test_oversized_body_does_not_leak_content(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = InfiniteFakeResponse(200)
        config = _make_config(max_response_bytes=100, chunk_size=64)
        with self.assertRaises(PermanentUploadError) as ctx:
            upload_snapshot(config, snap, connection_factory=_factory(resp))
        # Error message must not contain response content
        self.assertNotIn("xxxx", str(ctx.exception).lower())


# ── token leakage ────────────────────────────────────────────────────


class TestTokenLeakage(_TempDirCase):
    def test_token_absent_from_permanent_error(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_token=_LEAK_PROBE)
        resp = FakeResponse(403, json.dumps({"error": "forbidden"}).encode())
        with self.assertRaises(PermanentUploadError) as ctx:
            upload_snapshot(config, snap, connection_factory=_factory(resp))
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))
        self.assertNotIn(_LEAK_PROBE, repr(ctx.exception))

    def test_token_absent_from_transient_error(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_token=_LEAK_PROBE)
        resp = FakeResponse(500, json.dumps({"error": "server"}).encode())
        with self.assertRaises(TransientUploadError) as ctx:
            upload_snapshot(config, snap, connection_factory=_factory(resp))
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))
        self.assertNotIn(_LEAK_PROBE, repr(ctx.exception))

    def test_token_absent_from_malformed_error(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_token=_LEAK_PROBE)
        resp = FakeResponse(200, b"not json " + _LEAK_PROBE.encode())
        with self.assertRaises(PermanentUploadError) as ctx:
            upload_snapshot(config, snap, connection_factory=_factory(resp))
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))
        self.assertNotIn(_LEAK_PROBE, repr(ctx.exception))

    def test_token_absent_from_upload_result_repr(self) -> None:
        snap = _make_snapshot(self.dir)
        config = _make_config(upload_token=_LEAK_PROBE)
        # Server response echoes the token back
        resp = FakeResponse(
            200, json.dumps({"status": "ok", "echo": _LEAK_PROBE}).encode()
        )
        result = upload_snapshot(config, snap, connection_factory=_factory(resp))
        self.assertNotIn(_LEAK_PROBE, repr(result))


# ── connection lifecycle ─────────────────────────────────────────────


class TestConnectionLifecycle(_TempDirCase):
    def test_connection_closed_after_success(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(200, json.dumps({"status": "ok"}).encode())
        factory = _factory(resp)
        upload_snapshot(_make_config(), snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]
        self.assertTrue(conn.closed)

    def test_connection_closed_after_error(self) -> None:
        snap = _make_snapshot(self.dir)
        resp = FakeResponse(500, json.dumps({"error": "x"}).encode())
        factory = _factory(resp)
        with self.assertRaises(TransientUploadError):
            upload_snapshot(_make_config(), snap, connection_factory=factory)
        conn = factory._holder[0]  # type: ignore[attr-defined]
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
