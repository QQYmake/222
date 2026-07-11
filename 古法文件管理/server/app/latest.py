"""Latest JSON generator: produces a JSON snapshot of most recent values.

INPUT:  ServerConfig, DB connection
OUTPUT: JSON file written to data_dir/latest.json (atomic write),
        also returns the dict for API response.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .config import ServerConfig
from .database import connect


def generate_latest(config: ServerConfig) -> dict[str, Any]:
    """Generate latest.json containing the most recent observation per type.

    INPUT:  ServerConfig
    OUTPUT: dict with keys: heart_rate, steps, steps_daily, sleep_stage,
            each containing the most recent observation or null.
            Also writes the dict to data_dir/latest.json (atomic).
    """
    result: dict[str, Any] = {}

    with connect(config.db_path) as conn:
        for obs_type in ["heart_rate", "steps", "steps_daily", "sleep_stage"]:
            row = conn.execute(
                "SELECT timestamp_utc, timestamp_local, value, "
                "       raw_source, source_table, source_identity, "
                "       snapshot_hash "
                "FROM observations "
                "WHERE type = ? "
                "ORDER BY timestamp_utc DESC "
                "LIMIT 1",
                (obs_type,),
            ).fetchone()

            if row:
                result[obs_type] = {
                    "timestamp_utc": row[0],
                    "timestamp_local": row[1],
                    "value": json.loads(row[2]) if row[2] else None,
                    "raw_source": json.loads(row[3]) if row[3] else None,
                    "source_table": row[4],
                    "source_identity": row[5],
                    "snapshot_hash": row[6],
                }
            else:
                result[obs_type] = None

    # Atomic write to latest.json.
    latest_path = config.latest_dir / "latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(latest_path.parent), suffix=".json.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.rename(tmp_path, str(latest_path))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return result


def read_latest(config: ServerConfig) -> dict[str, Any] | None:
    """Read latest.json from disk.

    INPUT:  ServerConfig
    OUTPUT: parsed dict, or None if file doesn't exist
    """
    latest_path = config.latest_dir / "latest.json"
    if not latest_path.exists():
        return None
    return json.loads(latest_path.read_text(encoding="utf-8"))
