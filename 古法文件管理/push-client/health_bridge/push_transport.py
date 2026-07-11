"""Streaming HTTPS multipart transport for the health-bridge push client.

Sends a gzip-compressed SQLite snapshot to the server via
multipart/form-data, streaming the file in chunks to avoid loading it
entirely into memory.  Token values never appear in exception messages
or repr.
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from health_bridge.push_config import PushConfig
from health_bridge.push_snapshot import PreparedSnapshot

_BOUNDARY = "----healthbridgesnapshotboundary----"
_USER_AGENT = "health-bridge/1.0"

# HTTP status codes that indicate permanent failure — retrying will not help.
_PERMANENT_STATUSES = frozenset({401, 403, 413, 422})
# HTTP status codes that indicate a transient condition — retry may succeed.
_TRANSIENT_STATUSES = frozenset({408, 429})


@dataclass(frozen=True)
class UploadResult:
    http_status: int
    status: str
    delivered: bool
    # repr=False so server-echoed secrets never surface in debug output.
    response: dict[str, object] = field(repr=False)


class TransientUploadError(RuntimeError):
    """Upload failed due to a transient condition (e.g. 5xx, 429)."""


class PermanentUploadError(RuntimeError):
    """Upload failed permanently (e.g. 401, 422, malformed response)."""


def upload_snapshot(
    config: PushConfig,
    snapshot: PreparedSnapshot,
    connection_factory: Callable[[], Any] | None = None,
) -> UploadResult:
    parsed = urllib.parse.urlparse(config.upload_url)
    if parsed.scheme != "https":
        raise ValueError(f"Upload URL must use HTTPS: {config.upload_url}")

    host = parsed.hostname or ""
    port = parsed.port or 443
    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query

    # Build the multipart preamble and epilogue as static byte strings so
    # the total Content-Length is known before the first byte is sent.
    preamble = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; '
        f'filename="snapshot.db.gz"\r\n'
        f"Content-Type: application/gzip\r\n"
        f"\r\n"
    ).encode("ascii")
    epilogue = f"\r\n--{_BOUNDARY}--\r\n".encode("ascii")

    gzip_size = snapshot.gzip_path.stat().st_size
    total_length = len(preamble) + gzip_size + len(epilogue)

    headers: dict[str, str] = {
        "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}",
        "Content-Length": str(total_length),
        "X-Upload-Token": config.upload_token or "",
        "X-Snapshot-SHA256": snapshot.sha256,
        "Content-Encoding": "gzip",
        "User-Agent": _USER_AGENT,
    }

    if connection_factory is None:
        ctx = ssl.create_default_context()
        conn: Any = http.client.HTTPSConnection(
            host,
            port,
            timeout=config.request_timeout_seconds,
            context=ctx,
        )
    else:
        conn = connection_factory()

    try:
        conn.putrequest("POST", request_path)
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders()

        # Stream the body: preamble, then file in chunks, then epilogue.
        conn.send(preamble)
        with open(snapshot.gzip_path, "rb") as fh:
            while True:
                chunk = fh.read(config.chunk_size)
                if not chunk:
                    break
                conn.send(chunk)
        conn.send(epilogue)

        response = conn.getresponse()
        try:
            status = response.status
            body = _read_bounded(response, config)
        finally:
            response.close()

        try:
            response_data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise PermanentUploadError(
                f"Response body is not valid JSON (HTTP {status})"
            )

        if not isinstance(response_data, dict):
            raise PermanentUploadError(
                f"Response body is not a JSON object (HTTP {status})"
            )

        if status in (200, 201):
            server_status = response_data.get("status", "ok")
            return UploadResult(
                http_status=status,
                status=str(server_status),
                delivered=True,
                response=response_data,
            )

        if status == 202:
            server_status = response_data.get("status", "accepted")
            return UploadResult(
                http_status=status,
                status=str(server_status),
                delivered=True,
                response=response_data,
            )

        if status in _PERMANENT_STATUSES:
            raise PermanentUploadError(
                f"Upload rejected by server (HTTP {status})"
            )

        if status in _TRANSIENT_STATUSES or 500 <= status < 600:
            raise TransientUploadError(
                f"Upload failed, retry may succeed (HTTP {status})"
            )

        raise PermanentUploadError(
            f"Unexpected HTTP status: {status}"
        )
    finally:
        conn.close()


def _read_bounded(response: Any, config: PushConfig) -> bytes:
    """Read the response body in chunks, bounded by max_response_bytes."""
    body = bytearray()
    while True:
        chunk = response.read(config.chunk_size)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > config.max_response_bytes:
            raise PermanentUploadError(
                f"Response body exceeds maximum of "
                f"{config.max_response_bytes} bytes"
            )
    return bytes(body)
