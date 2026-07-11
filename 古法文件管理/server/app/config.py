"""Server-side configuration for Health-Bridge.

All paths, limits, and token locations are resolved here.
Token values are never placed in exception messages, repr, or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_DEFAULT_DATA_DIR = Path("/srv/health-bridge/data")
_DEFAULT_ARCHIVES_DIR = Path("/srv/health-bridge/archives")
_DEFAULT_LATEST_DIR = Path("/srv/health-bridge/latest")
_DEFAULT_MAX_DECOMPRESSED = 104_857_600  # 100 MiB
_DEFAULT_MAX_BODY = 104_857_600


@dataclass(frozen=True)
class ServerConfig:
    """Immutable server configuration."""

    data_dir: Path
    raw_dir: Path
    incoming_dir: Path
    db_path: Path
    archives_dir: Path
    latest_dir: Path
    max_decompressed_bytes: int
    max_body_bytes: int
    listen_host: str
    listen_port: int
    # repr=False so tokens never surface in logs or debug dumps.
    upload_token: str | None = field(default=None, repr=False)
    read_token: str | None = field(default=None, repr=False)
    schema_version: int = 1


def load_server_config(
    environ: Mapping[str, str] | None = None,
    *,
    data_dir: Path | str | None = None,
    upload_token: str | None = None,
    read_token: str | None = None,
) -> ServerConfig:
    """Build a ServerConfig from explicit args or environment variables.

    Environment variables (lower priority than explicit args):
        HEALTH_BRIDGE_DATA_DIR      — data directory
        HEALTH_BRIDGE_ARCHIVES_DIR  — archives directory
        HEALTH_BRIDGE_LATEST_DIR    — latest JSON directory
        HEALTH_BRIDGE_UPLOAD_TOKEN  — upload token value
        HEALTH_BRIDGE_READ_TOKEN    — read token value
        HEALTH_BRIDGE_LISTEN_HOST   — bind address (default 127.0.0.1)
        HEALTH_BRIDGE_LISTEN_PORT   — bind port (default 8765)
    """
    env = environ if environ is not None else os.environ

    resolved_data = Path(data_dir) if data_dir else Path(
        env.get("HEALTH_BRIDGE_DATA_DIR", str(_DEFAULT_DATA_DIR))
    )

    resolved_archives = Path(
        env.get("HEALTH_BRIDGE_ARCHIVES_DIR", str(_DEFAULT_ARCHIVES_DIR))
    )
    resolved_latest = Path(
        env.get("HEALTH_BRIDGE_LATEST_DIR", str(_DEFAULT_LATEST_DIR))
    )

    resolved_upload = upload_token or env.get("HEALTH_BRIDGE_UPLOAD_TOKEN")
    resolved_read = read_token or env.get("HEALTH_BRIDGE_READ_TOKEN")

    resolved_host = env.get("HEALTH_BRIDGE_LISTEN_HOST", "127.0.0.1")
    resolved_port = int(env.get("HEALTH_BRIDGE_LISTEN_PORT", "8765"))

    return ServerConfig(
        data_dir=resolved_data,
        raw_dir=resolved_data / "raw",
        incoming_dir=resolved_data / "incoming",
        db_path=resolved_data / "health.sqlite3",
        archives_dir=resolved_archives,
        latest_dir=resolved_latest,
        max_decompressed_bytes=_DEFAULT_MAX_DECOMPRESSED,
        max_body_bytes=_DEFAULT_MAX_BODY,
        listen_host=resolved_host,
        listen_port=resolved_port,
        upload_token=resolved_upload,
        read_token=resolved_read,
    )
