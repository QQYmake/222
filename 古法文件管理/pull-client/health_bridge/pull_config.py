"""Configuration loading for the health-bridge pull client.

Reads environment variables and optional JSON config file.
The read token is never placed in config files, repr, or logs.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_DEFAULT_BASE_URL = "https://oh-my-frontweb.duckdns.org"
_API_PATH = "/health/api/v1"

# Environment variable names.
_BASE_URL_ENV_VAR = "HEALTH_PULL_BASE_URL"
_TOKEN_ENV_VAR = "HEALTH_READ_TOKEN"
_TIMEOUT_ENV_VAR = "HEALTH_PULL_TIMEOUT"
_TIMEZONE_ENV_VAR = "HEALTH_PULL_TIMEZONE"

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "base_url": _DEFAULT_BASE_URL,
    "timeout_seconds": 30,
    "timezone": "Asia/Shanghai",
}


@dataclass(frozen=True)
class PullConfig:
    base_url: str
    api_base: str
    timeout_seconds: int
    timezone: str
    dry_run: bool = False
    # repr=False so the token never surfaces in logs or debug dumps.
    read_token: str = field(default="", repr=False)


def load_pull_config(
    config_path: Path | None,
    env: Mapping[str, str],
    dry_run: bool = False,
    allow_insecure: bool = False,
) -> PullConfig:
    """Load pull client configuration.

    Priority: env vars > config file > built-in defaults.
    Token always comes from env, never from config file.
    """
    # Start with built-in defaults.
    merged: dict[str, Any] = dict(_BUILTIN_DEFAULTS)

    # Override with config file values (non-secret).
    if config_path is not None:
        path = config_path.expanduser()
        text = path.read_text(encoding="utf-8")
        file_data = json.loads(text)
        if not isinstance(file_data, dict):
            raise ValueError(f"Config file must be a JSON object: {path}")
        # Ignore any token field in config file for security.
        file_data.pop("read_token", None)
        file_data.pop("token", None)
        merged.update(file_data)

    # Override with environment variables (highest priority).
    if env.get(_BASE_URL_ENV_VAR):
        merged["base_url"] = env[_BASE_URL_ENV_VAR]
    if env.get(_TIMEOUT_ENV_VAR):
        merged["timeout_seconds"] = int(env[_TIMEOUT_ENV_VAR])
    if env.get(_TIMEZONE_ENV_VAR):
        merged["timezone"] = env[_TIMEZONE_ENV_VAR]

    base_url = str(merged["base_url"]).rstrip("/")
    timeout_seconds = int(merged["timeout_seconds"])
    timezone = str(merged["timezone"])
    api_base = base_url + _API_PATH

    # Token always from environment.
    read_token = env.get(_TOKEN_ENV_VAR, "")

    # Validation.
    if not dry_run and not read_token:
        raise ValueError(
            "Read token is required. Set the HEALTH_READ_TOKEN environment variable."
        )

    if not allow_insecure and not base_url.startswith("https://"):
        raise ValueError(
            f"Base URL must use HTTPS in production: {base_url}"
        )

    return PullConfig(
        base_url=base_url,
        api_base=api_base,
        timeout_seconds=timeout_seconds,
        timezone=timezone,
        dry_run=dry_run,
        read_token=read_token,
    )
