"""HTTP transport layer for the health-bridge pull client.

Sends authenticated GET requests to the VPS API server.
Token values never appear in exception messages.
"""

from __future__ import annotations

import http.client
import ssl
import urllib.parse
from typing import Any, Callable, Mapping

from health_bridge.pull_config import PullConfig

_USER_AGENT = "health-bridge-pull/1.0"
_MAX_RESPONSE_BYTES = 1_048_576  # 1 MiB


class PullTransportError(RuntimeError):
    """Base class for transport errors."""


class AuthError(PullTransportError):
    """Authentication or authorization failure (401/403)."""


class NotFoundError(PullTransportError):
    """Resource not found (404)."""


class TransientError(PullTransportError):
    """Transient failure (429/5xx), retry may succeed."""


class TransportError(PullTransportError):
    """Unexpected transport failure."""


class PullTransport:
    """HTTP GET client for the health-bridge API."""

    def __init__(self, config: PullConfig):
        self._config = config

    def get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> bytes:
        """Send a GET request and return the raw response body.

        Raises AuthError, NotFoundError, TransientError, or TransportError.
        """
        # Build full URL.
        url = self._config.api_base + path
        if params:
            query = urllib.parse.urlencode(params)
            url += "?" + query

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query

        headers = {
            "Authorization": f"Bearer {self._config.read_token}",
            "User-Agent": _USER_AGENT,
        }

        if connection_factory is None:
            if parsed.scheme == "https":
                ctx = ssl.create_default_context()
                conn: Any = http.client.HTTPSConnection(
                    host, port, timeout=self._config.timeout_seconds, context=ctx,
                )
            else:
                conn = http.client.HTTPConnection(
                    host, port, timeout=self._config.timeout_seconds,
                )
        else:
            conn = connection_factory()

        try:
            conn.putrequest("GET", request_path)
            for name, value in headers.items():
                conn.putheader(name, value)
            conn.endheaders()

            response = conn.getresponse()
            status = response.status
            body = self._read_bounded(response)
        finally:
            conn.close()

        if status == 200:
            return body

        detail = body.decode("utf-8", errors="replace")

        if status in (401, 403):
            raise AuthError(f"Server rejected request (HTTP {status})")
        if status == 404:
            raise NotFoundError(f"Resource not found (HTTP {status})")
        if status == 429 or 500 <= status < 600:
            raise TransientError(f"Transient failure (HTTP {status})")

        raise TransportError(f"Unexpected HTTP status {status}")

    def _read_bounded(self, response: Any) -> bytes:
        """Read response body in chunks, bounded by max size."""
        body = bytearray()
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > _MAX_RESPONSE_BYTES:
                raise TransportError(
                    f"Response body exceeds maximum of {_MAX_RESPONSE_BYTES} bytes"
                )
        return bytes(body)
