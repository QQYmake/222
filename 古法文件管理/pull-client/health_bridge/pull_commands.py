"""Command handlers for the health-bridge pull client.

Each function maps to a CLI subcommand and delegates HTTP to the transport layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from health_bridge.pull_config import PullConfig
from health_bridge.pull_transport import PullTransport


@dataclass(frozen=True)
class RangeResult:
    """Result of a range query."""
    observations: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool


def cmd_latest(
    transport: PullTransport,
    config: PullConfig,
    obs_type: str | None,
) -> dict[str, Any]:
    """Fetch latest observations, optionally filtered by type.

    INPUT:  optional observation type
    OUTPUT: dict mapping type name to latest observation (or None)
    """
    params = {"type": obs_type} if obs_type else None
    body = transport.get("/latest", params=params)
    return json.loads(body.decode("utf-8"))


def cmd_range(
    transport: PullTransport,
    config: PullConfig,
    obs_type: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> RangeResult:
    """Query observations in a time range.

    INPUT:  type, optional from/to timestamps, limit, cursor
    OUTPUT: RangeResult with observations list and pagination cursor
    """
    params: dict[str, str] = {"type": obs_type, "limit": str(limit)}
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts
    if cursor:
        params["cursor"] = cursor

    body = transport.get("/data", params=params)
    data = json.loads(body.decode("utf-8"))

    return RangeResult(
        observations=data.get("observations", []),
        next_cursor=data.get("next_cursor"),
        has_more=data.get("next_cursor") is not None,
    )


def cmd_weeks(
    transport: PullTransport,
    config: PullConfig,
) -> list[str]:
    """List available ISO week archives.

    INPUT:  none
    OUTPUT: list of week identifiers (e.g. ["2026-W28"])
    """
    body = transport.get("/weeks")
    data = json.loads(body.decode("utf-8"))
    return data.get("weeks", [])


def cmd_archive(
    transport: PullTransport,
    config: PullConfig,
    week_id: str,
) -> str:
    """Download a weekly Markdown archive.

    INPUT:  week_id (e.g. "2026-W28")
    OUTPUT: Markdown text
    """
    body = transport.get(f"/archive/{week_id}")
    return body.decode("utf-8")
