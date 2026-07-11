"""Archive generator: produces per-ISO-week Markdown archives.

INPUT:  ISO week id (e.g. "2026-W28"), ServerConfig, DB connection
OUTPUT: Markdown file written to data_dir/archives/<week_id>.md (atomic write)

Pipeline:
  1. Query observations for the given ISO week, grouped by type
  2. Format into Markdown sections (heart_rate, steps, steps_daily, sleep)
  3. Write to temp file then atomically rename
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Sequence

from .config import ServerConfig
from .database import connect


def _week_range(week_id: str) -> tuple[str, str]:
    """Return (start_utc, end_utc) ISO timestamps for an ISO week.

    INPUT:  week_id like "2026-W28"
    OUTPUT: (start_iso, end_iso) in UTC, start = Monday 00:00:00 UTC,
            end = next Monday 00:00:00 UTC
    """
    import re
    from datetime import datetime, timedelta, timezone

    m = re.match(r"(\d{4})-W(\d{2})", week_id)
    if not m:
        raise ValueError(f"Invalid week_id: {week_id}")
    year, week = int(m.group(1)), int(m.group(2))

    # ISO week 1 contains the first Thursday.
    jan4 = datetime(year, 1, 4, tzinfo=timezone.utc)
    week1_monday = jan4 - timedelta(days=jan4.weekday())
    week_monday = week1_monday + timedelta(weeks=week - 1)
    week_next_monday = week_monday + timedelta(weeks=1)

    return week_monday.isoformat(), week_next_monday.isoformat()


def generate_archive(
    week_id: str,
    config: ServerConfig,
) -> Path:
    """Generate a Markdown archive for the given ISO week.

    INPUT:  week_id (e.g. "2026-W28"), ServerConfig
    OUTPUT: Path to the generated .md file (atomic write)
    """
    archive_dir = config.archives_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{week_id}.md"

    start_utc, end_utc = _week_range(week_id)

    with connect(config.db_path) as conn:
        # Fetch all observations in this week.
        rows = conn.execute(
            "SELECT type, timestamp_utc, timestamp_local, value, "
            "       raw_source, source_table, source_identity "
            "FROM observations "
            "WHERE timestamp_utc >= ? AND timestamp_utc < ? "
            "ORDER BY timestamp_utc",
            (start_utc, end_utc),
        ).fetchall()

    md = _format_markdown(week_id, start_utc, end_utc, rows)

    # Atomic write: temp file + rename.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(archive_dir), suffix=".md.tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(md)
        os.rename(tmp_path, str(target))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return target


def _format_markdown(
    week_id: str,
    start_utc: str,
    end_utc: str,
    rows: Sequence[tuple],
) -> str:
    """Format observations into Markdown.

    INPUT:  week_id, time range, rows from DB query
    OUTPUT: Markdown string
    """
    lines: list[str] = []
    lines.append(f"# Health Archive — {week_id}")
    lines.append("")
    lines.append(f"> Period: {start_utc} to {end_utc} (UTC)")
    lines.append(f"> Total observations: {len(rows)}")
    lines.append("")
    lines.append("> **Disclaimer:** This archive is auto-generated from "
                 "Gadgetbridge exports. Sleep stage codes are raw integers; "
                 "this is not a medical diagnosis.")
    lines.append("")

    # Group by type.
    by_type: dict[str, list[tuple]] = {}
    for row in rows:
        by_type.setdefault(row[0], []).append(row)

    type_labels = {
        "heart_rate": "Heart Rate",
        "steps": "Steps (per-sample)",
        "steps_daily": "Steps (Daily Summary)",
        "sleep_stage": "Sleep Stages",
    }

    for obs_type in ["heart_rate", "steps", "steps_daily", "sleep_stage"]:
        entries = by_type.get(obs_type, [])
        if not entries:
            continue
        label = type_labels.get(obs_type, obs_type)
        lines.append(f"## {label}")
        lines.append("")
        lines.append(f"| # | Timestamp (UTC) | Timestamp (Local) | Value |")
        lines.append(f"|---|-----------------|-------------------|-------|")
        for i, row in enumerate(entries, 1):
            _type, ts_utc, ts_local, value, raw, src_table, src_id = row
            lines.append(f"| {i} | {ts_utc} | {ts_local} | {value} |")
        lines.append("")

    if not by_type:
        lines.append("## No Data")
        lines.append("")
        lines.append("No observations recorded for this week.")
        lines.append("")

    return "\n".join(lines) + "\n"


def list_archives(config: ServerConfig) -> list[str]:
    """List all available archive week IDs.

    INPUT:  ServerConfig
    OUTPUT: sorted list of week_id strings (e.g. ["2026-W27", "2026-W28"])
    """
    archive_dir = config.archives_dir
    if not archive_dir.exists():
        return []
    weeks = sorted(
        p.stem for p in archive_dir.glob("*.md")
        if p.is_file()
    )
    return weeks


def read_archive(week_id: str, config: ServerConfig) -> str | None:
    """Read a specific archive's content.

    INPUT:  week_id, ServerConfig
    OUTPUT: Markdown content string, or None if not found
    """
    target = config.archives_dir / f"{week_id}.md"
    if not target.exists():
        return None
    return target.read_text(encoding="utf-8")
