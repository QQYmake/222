"""Configuration loading for the health-bridge push client.

The push client reads a JSON config file for non-secret values and resolves
the upload token from the environment (highest priority) or a file on disk.
Token values are never placed in exception messages, repr, or logs.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_DEFAULT_BASE_URL = "https://oh-my-frontweb.duckdns.org"
_UPLOAD_PATH = "/health/api/v1/upload"

# Environment variable name for overriding the upload base URL.
_BASE_URL_ENV_VAR = "HEALTH_UPLOAD_BASE_URL"

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "source_path": "/storage/emulated/0/Download/health/Gadgetbridge.db",
    "upload_base_url": _DEFAULT_BASE_URL,
    "state_path": "~/.local/state/health-bridge/push-state.json",
    "poll_interval_seconds": 900,
    "stability_delay_seconds": 5,
    "request_timeout_seconds": 120,
    "max_retries": 5,
    "max_uncompressed_bytes": 104_857_600,
    "chunk_size": 1_048_576,
    "max_response_bytes": 1_048_576,
    "token_env": "HEALTH_UPLOAD_TOKEN",
    "token_file": None,
}

_GROUP_OTHER_MASK = stat.S_IRWXG | stat.S_IRWXO


@dataclass(frozen=True)
class PushConfig:
    source_path: Path
    upload_url: str
    state_path: Path
    poll_interval_seconds: float
    stability_delay_seconds: float
    request_timeout_seconds: float
    max_retries: int
    max_uncompressed_bytes: int
    chunk_size: int
    max_response_bytes: int
    token_env: str
    token_file: Path | None
    upload_base_url: str = _DEFAULT_BASE_URL
    # repr=False so the token never surfaces in logs or debug dumps.
    upload_token: str | None = field(default=None, repr=False)


def load_push_config(
    path: Path | None,
    environ: Mapping[str, str],
    *,
    dry_run: bool,
) -> PushConfig:
    merged: dict[str, Any] = dict(_BUILTIN_DEFAULTS)
    file_values: dict[str, Any] = {}

    if path is not None:
        with open(path, encoding="utf-8") as fh:
            file_values = json.load(fh)
        merged.update(file_values)

    # Resolve the upload base URL.  Priority (highest first):
    #   1. HEALTH_UPLOAD_BASE_URL environment variable
    #   2. upload_base_url from the config file
    #   3. Built-in default
    upload_base_url: str = (
        environ.get(_BASE_URL_ENV_VAR)
        or merged.get("upload_base_url", _DEFAULT_BASE_URL)
    )

    # Determine the final upload_url.  If the config file explicitly
    # provides upload_url, it takes full precedence (backward compat).
    # Otherwise, construct it from the resolved base URL.
    if "upload_url" in file_values:
        upload_url: str = file_values["upload_url"]
    else:
        upload_url = upload_base_url.rstrip("/") + _UPLOAD_PATH

    _require_https(upload_url)

    poll_interval = float(merged["poll_interval_seconds"])
    stability_delay = float(merged["stability_delay_seconds"])
    request_timeout = float(merged["request_timeout_seconds"])
    max_retries = int(merged["max_retries"])
    max_uncompressed = int(merged["max_uncompressed_bytes"])
    chunk_size = int(merged["chunk_size"])
    max_response = int(merged["max_response_bytes"])

    _require_positive("poll_interval_seconds", poll_interval)
    _require_positive("stability_delay_seconds", stability_delay)
    _require_positive("request_timeout_seconds", request_timeout)
    _require_positive("max_uncompressed_bytes", max_uncompressed)
    _require_positive("chunk_size", chunk_size)
    _require_positive("max_response_bytes", max_response)
    _require_non_negative("max_retries", max_retries)

    token_env: str = merged["token_env"]
    token_file_raw = merged.get("token_file")
    token_file = (
        Path(token_file_raw).expanduser() if token_file_raw is not None else None
    )

    upload_token = _resolve_token(token_env, token_file, environ)

    if upload_token is None and not dry_run:
        raise ValueError(
            f"No upload token available. Set the {token_env} environment "
            f"variable or provide a readable token_file in the config."
        )

    return PushConfig(
        source_path=Path(merged["source_path"]).expanduser(),
        upload_url=upload_url,
        state_path=Path(merged["state_path"]).expanduser(),
        poll_interval_seconds=poll_interval,
        stability_delay_seconds=stability_delay,
        request_timeout_seconds=request_timeout,
        max_retries=max_retries,
        max_uncompressed_bytes=max_uncompressed,
        chunk_size=chunk_size,
        max_response_bytes=max_response,
        token_env=token_env,
        token_file=token_file,
        upload_base_url=upload_base_url,
        upload_token=upload_token,
    )


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise ValueError(
            f"upload_url must use HTTPS; refusing: {url}"
        )


def _require_positive(name: str, value: float | int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _require_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def _resolve_token(
    token_env: str,
    token_file: Path | None,
    environ: Mapping[str, str],
) -> str | None:
    # Environment takes precedence so operators can override per-deployment
    # without editing config files.
    if token_env in environ and environ[token_env]:
        return environ[token_env]

    if token_file is None:
        return None

    _enforce_token_file_permissions(token_file)

    return token_file.read_text(encoding="utf-8").rstrip("\r\n")


def _enforce_token_file_permissions(token_file: Path) -> None:
    # On Windows the POSIX mode bits are meaningless; Windows ACLs govern
    # access instead.  We document that the file should live under the user's
    # private profile but do not attempt an ACL check here.
    if os.name == "nt":
        return

    mode = os.stat(token_file).st_mode
    if mode & _GROUP_OTHER_MASK:
        raise PermissionError(
            f"Token file {token_file} is accessible by group or others "
            f"(mode {oct(mode & 0o777)}); restrict to owner-only access "
            f"(e.g. chmod 600)."
        )
