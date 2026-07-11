"""Watch mode for the health-bridge pull client.

Periodically polls the latest endpoint for one or more data types,
atomically writes results to local files, and emits change notifications.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

from health_bridge.pull_config import PullConfig
from health_bridge.pull_transport import PullTransport
from health_bridge.pull_commands import cmd_latest


def watch_once(
    transport: PullTransport,
    config: PullConfig,
    types: list[str],
    output_dir: Path,
    last_hashes: dict[str, str],
) -> list[str]:
    """Execute one poll cycle for all types.

    INPUT:  list of types to poll, output directory, previous content hashes
    OUTPUT: list of change notification strings; updated hashes are written
            back to last_hashes (mutated in place)
    """
    notifications: list[str] = []

    for obs_type in types:
        try:
            result = cmd_latest(transport, config, obs_type)
        except Exception as exc:
            notifications.append(f"[error] {obs_type}: {exc}")
            continue

        raw_body = json.dumps(result, sort_keys=True).encode("utf-8")
        content_hash = hashlib.sha256(raw_body).hexdigest()

        if content_hash == last_hashes.get(obs_type):
            continue  # No change.

        last_hashes[obs_type] = content_hash

        value = result.get(obs_type)
        if value is None:
            continue  # Null value, nothing to write.

        # Atomic write: temp file → rename.
        out_file = output_dir / f"{obs_type}.json"
        _atomic_write(out_file, json.dumps(result, indent=2, ensure_ascii=False))

        # Build notification.
        ts = value.get("timestamp_local", value.get("timestamp_utc", "?"))
        val = value.get("value", {})
        val_str = _format_value(val)
        notifications.append(f"[{ts}] {obs_type}: {val_str}")

    return notifications


def watch_loop(
    transport: PullTransport,
    config: PullConfig,
    types: list[str],
    interval: int,
    output_dir: Path,
    max_iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run watch loop until interrupted.

    INPUT:  types, interval (seconds), output directory, optional max iterations
    OUTPUT: exit code (0 = normal, 130 = Ctrl+C)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    last_hashes: dict[str, str] = {}

    # Load existing hashes from existing files.
    for obs_type in types:
        out_file = output_dir / f"{obs_type}.json"
        if out_file.exists():
            raw = out_file.read_bytes()
            last_hashes[obs_type] = hashlib.sha256(raw).hexdigest()

    iteration = 0
    try:
        while True:
            notifications = watch_once(transport, config, types, output_dir, last_hashes)
            for note in notifications:
                print(note, flush=True)

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break

            sleep_fn(interval)
    except KeyboardInterrupt:
        print("\n[watch] Stopped.", file=sys.stderr)
        return 130

    return 0


def _atomic_write(path: Path, content: str) -> None:
    """Write file atomically: write to temp, fsync, rename."""
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


def _format_value(value: dict[str, Any]) -> str:
    """Format a value dict for display."""
    if "bpm" in value:
        return f"{value['bpm']} bpm"
    if "steps" in value:
        return f"{value['steps']} steps"
    if "stage" in value:
        return f"stage={value['stage']}"
    return json.dumps(value, ensure_ascii=False)
