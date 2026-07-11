"""Tests for push_snapshot — stable snapshot preparation.

Written before implementation, following strict TDD.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clients.health_bridge.push_config import PushConfig
from clients.health_bridge.push_snapshot import PreparedSnapshot, prepare_snapshot


def _make_sqlite_db(path: Path) -> None:
    """Create a small synthetic SQLite database for testing."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    conn.executemany(
        "INSERT INTO t VALUES (?, ?)", [(i, f"name{i}") for i in range(10)]
    )
    conn.commit()
    conn.close()


def _make_config(
    source_path: Path,
    *,
    max_uncompressed_bytes: int = 104_857_600,
    stability_delay_seconds: float = 0.01,
    chunk_size: int = 4096,
) -> PushConfig:
    return PushConfig(
        source_path=source_path,
        upload_url="https://example.com/api",
        state_path=Path("/tmp/state.json"),
        poll_interval_seconds=900,
        stability_delay_seconds=stability_delay_seconds,
        request_timeout_seconds=120,
        max_retries=5,
        max_uncompressed_bytes=max_uncompressed_bytes,
        chunk_size=chunk_size,
        max_response_bytes=1_048_576,
        token_env="HEALTH_UPLOAD_TOKEN",
        token_file=None,
        upload_token="test-token",
    )


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestSourceMissing(_TempDirCase):
    def test_raises_file_not_found(self) -> None:
        missing = self.dir / "nonexistent.db"
        config = _make_config(source_path=missing)
        with self.assertRaises(FileNotFoundError):
            with prepare_snapshot(config):
                pass


class TestUncompressedLimit(_TempDirCase):
    def test_rejects_oversized_source(self) -> None:
        db_path = self.dir / "big.db"
        _make_sqlite_db(db_path)
        actual_size = db_path.stat().st_size
        config = _make_config(
            source_path=db_path,
            max_uncompressed_bytes=actual_size - 1,
        )
        with self.assertRaises(ValueError):
            with prepare_snapshot(config):
                pass


class TestStabilityCheck(_TempDirCase):
    """Two stat observations with a sleep between them; file must not change."""

    def test_detects_file_change_between_checks(self) -> None:
        db_path = self.dir / "changing.db"
        _make_sqlite_db(db_path)

        call_count = [0]

        def modifying_sleep(seconds: float) -> None:
            call_count[0] += 1
            # Append bytes to simulate an active writer
            with open(db_path, "ab") as f:
                f.write(b"\x00" * 100)

        config = _make_config(source_path=db_path, stability_delay_seconds=0.01)
        with self.assertRaises(RuntimeError):
            with prepare_snapshot(config, sleep=modifying_sleep):
                pass
        self.assertEqual(call_count[0], 1)

    def test_copies_only_after_two_matching_stats(self) -> None:
        db_path = self.dir / "stable.db"
        _make_sqlite_db(db_path)

        sleep_calls: list[float] = []

        def tracking_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        config = _make_config(source_path=db_path, stability_delay_seconds=3.5)
        with prepare_snapshot(config, sleep=tracking_sleep) as snap:
            self.assertIsInstance(snap, PreparedSnapshot)

        # Exactly one sleep between two stat observations
        self.assertEqual(len(sleep_calls), 1)
        self.assertEqual(sleep_calls[0], 3.5)


class TestSQLiteMagicHeader(_TempDirCase):
    """Reject files whose first 16 bytes are not the SQLite magic header."""

    def test_rejects_non_sqlite_file(self) -> None:
        bad_path = self.dir / "not_sqlite.db"
        bad_path.write_bytes(b"NOT SQLITE FORMAT" + b"\x00" * 100)
        config = _make_config(source_path=bad_path)
        with self.assertRaises(ValueError):
            with prepare_snapshot(config):
                pass

    def test_rejects_arbitrary_bytes(self) -> None:
        bad_path = self.dir / "random.db"
        bad_path.write_bytes(os.urandom(2048))
        config = _make_config(source_path=bad_path)
        with self.assertRaises(ValueError):
            with prepare_snapshot(config):
                pass


class TestPragmaQuickCheck(_TempDirCase):
    """PRAGMA quick_check must return 'ok' for the staged copy."""

    def test_accepts_valid_database(self) -> None:
        db_path = self.dir / "valid.db"
        _make_sqlite_db(db_path)
        config = _make_config(source_path=db_path)
        with prepare_snapshot(config) as snap:
            self.assertEqual(snap.source_path, db_path)

    def test_rejects_corrupted_database(self) -> None:
        db_path = self.dir / "corrupt.db"
        _make_sqlite_db(db_path)

        # Flip bytes after the 16-byte header to break integrity
        data = bytearray(db_path.read_bytes())
        for i in range(100, min(200, len(data))):
            data[i] ^= 0xFF
        db_path.write_bytes(data)

        config = _make_config(source_path=db_path)
        with self.assertRaises(ValueError):
            with prepare_snapshot(config):
                pass


class TestReadOnlyOpen(_TempDirCase):
    """The staged copy is opened via SQLite URI mode=ro (no side files)."""

    def test_no_side_files_created(self) -> None:
        db_path = self.dir / "readonly.db"
        _make_sqlite_db(db_path)
        config = _make_config(source_path=db_path)
        with prepare_snapshot(config) as snap:
            parent = snap.staged_db_path.parent
            all_files = {p.name for p in parent.iterdir()}
            expected = {snap.staged_db_path.name, snap.gzip_path.name}
            self.assertEqual(all_files, expected)


class TestSHA256(_TempDirCase):
    """The SHA-256 digest matches an independent computation."""

    def test_produces_correct_sha256(self) -> None:
        db_path = self.dir / "hash.db"
        _make_sqlite_db(db_path)

        expected_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()

        config = _make_config(source_path=db_path)
        with prepare_snapshot(config) as snap:
            self.assertEqual(snap.sha256, expected_hash)
            self.assertEqual(snap.uncompressed_bytes, db_path.stat().st_size)


class TestGzipOutput(_TempDirCase):
    """The gzip archive decompresses to the staged copy."""

    def test_gzip_decompresses_to_staged_copy(self) -> None:
        db_path = self.dir / "gz.db"
        _make_sqlite_db(db_path)
        original_bytes = db_path.read_bytes()

        config = _make_config(source_path=db_path)
        with prepare_snapshot(config) as snap:
            decompressed = gzip.decompress(snap.gzip_path.read_bytes())
            self.assertEqual(decompressed, original_bytes)
            self.assertEqual(snap.staged_db_path.read_bytes(), original_bytes)
            self.assertLess(snap.compressed_bytes, snap.uncompressed_bytes)


class TestCleanup(_TempDirCase):
    """Temporary files are removed after both success and failure."""

    def test_cleanup_after_success(self) -> None:
        db_path = self.dir / "success.db"
        _make_sqlite_db(db_path)
        config = _make_config(source_path=db_path)

        with prepare_snapshot(config) as snap:
            self.assertTrue(snap.staged_db_path.exists())
            self.assertTrue(snap.gzip_path.exists())
            staged = snap.staged_db_path
            gzip_p = snap.gzip_path

        self.assertFalse(staged.exists())
        self.assertFalse(gzip_p.exists())

    def test_cleanup_after_failure(self) -> None:
        bad_path = self.dir / "bad.db"
        bad_path.write_bytes(b"NOT SQLITE" + b"\x00" * 50)
        config = _make_config(source_path=bad_path)

        import clients.health_bridge.push_snapshot as snap_module

        created_dirs: list[Path] = []
        original_tempdir = snap_module.TemporaryDirectory

        class TrackingTempDir(original_tempdir):  # type: ignore[misc]
            def __init__(self, *args: object, **kwargs: object) -> None:
                super().__init__(*args, **kwargs)  # type: ignore[arg-type]
                created_dirs.append(Path(self.name))

        with patch.object(snap_module, "TemporaryDirectory", TrackingTempDir):
            with self.assertRaises(Exception):
                with prepare_snapshot(config):
                    pass

        for d in created_dirs:
            self.assertFalse(d.exists(), f"Temp dir {d} not cleaned up")


if __name__ == "__main__":
    unittest.main()
