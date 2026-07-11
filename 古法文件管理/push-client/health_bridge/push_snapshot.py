"""Stable snapshot preparation for the health-bridge push client.

Copies the source SQLite database to a private staging area only after
confirming the file has not changed between two stat observations, then
validates the copy and produces a gzip-compressed archive with a SHA-256
digest.

The health-data rows inside the database are never queried or logged.
"""

from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Iterator

from health_bridge.push_config import PushConfig

# SQLite databases always start with these 16 bytes.
_SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class PreparedSnapshot:
    source_path: Path
    staged_db_path: Path
    gzip_path: Path
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int


@contextmanager
def prepare_snapshot(
    config: PushConfig,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[PreparedSnapshot]:
    source = config.source_path

    if not source.exists():
        raise FileNotFoundError(f"Source database not found: {source}")

    file_size = source.stat().st_size
    if file_size > config.max_uncompressed_bytes:
        raise ValueError(
            f"Source file is {file_size} bytes, exceeds limit "
            f"of {config.max_uncompressed_bytes} bytes"
        )

    # Two stat observations with a delay between them.  If size or mtime
    # changes, the file is being actively written and is not safe to copy.
    stat_first = source.stat()
    sleep(config.stability_delay_seconds)
    stat_second = source.stat()

    if (
        stat_first.st_size != stat_second.st_size
        or stat_first.st_mtime_ns != stat_second.st_mtime_ns
    ):
        raise RuntimeError(
            f"Source file changed during stability check "
            f"(size {stat_first.st_size} -> {stat_second.st_size}, "
            f"mtime {stat_first.st_mtime_ns} -> {stat_second.st_mtime_ns})"
        )

    with TemporaryDirectory(prefix="health-bridge-snapshot-") as tmpdir:
        staged = Path(tmpdir) / "staged.db"
        shutil.copyfile(source, staged)

        # Reject anything that is not a SQLite database before opening it.
        with open(staged, "rb") as fh:
            magic = fh.read(16)
        if magic != _SQLITE_MAGIC:
            raise ValueError(
                "Staged file does not have a valid SQLite magic header"
            )

        # Read-only URI prevents accidental writes and avoids creating
        # journal/WAL side-files in the staging directory.
        uri = staged.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            # quick_check may raise DatabaseError on severe corruption
            # instead of returning a non-"ok" row.
            try:
                row = conn.execute("PRAGMA quick_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise ValueError(
                    f"SQLite integrity check failed: {exc}"
                ) from exc
            if row is None or row[0] != "ok":
                detail = row[0] if row else "no result"
                raise ValueError(f"SQLite integrity check failed: {detail}")
        finally:
            conn.close()

        # Chunked SHA-256 so large databases do not load fully into memory.
        sha256 = hashlib.sha256()
        with open(staged, "rb") as fh:
            while True:
                chunk = fh.read(config.chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
        sha256_hex = sha256.hexdigest()

        # Chunked gzip output for the same reason.
        gzip_path = Path(tmpdir) / "staged.db.gz"
        with open(staged, "rb") as src, gzip.open(gzip_path, "wb") as dst:
            while True:
                chunk = src.read(config.chunk_size)
                if not chunk:
                    break
                dst.write(chunk)

        uncompressed_bytes = staged.stat().st_size
        compressed_bytes = gzip_path.stat().st_size

        yield PreparedSnapshot(
            source_path=source,
            staged_db_path=staged,
            gzip_path=gzip_path,
            sha256=sha256_hex,
            uncompressed_bytes=uncompressed_bytes,
            compressed_bytes=compressed_bytes,
        )
