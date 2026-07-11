"""Tests for atomic state persistence of the health-data push client."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from clients.health_bridge.push_state import PushState, load_state, save_state


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestLoadMissingFile(_TempDirCase):
    """A missing state file yields an empty PushState."""

    def test_returns_empty_state(self) -> None:
        path = self.dir / "state.json"
        state = load_state(path)
        self.assertIsInstance(state, PushState)
        self.assertIsNone(state.accepted_sha256)
        self.assertIsNone(state.accepted_at)
        self.assertIsNone(state.server_status)
        self.assertIsNone(state.rejected_fingerprint)
        self.assertIsNone(state.rejected_reason)
        self.assertIsNone(state.last_failure)


class TestRoundTrip(_TempDirCase):
    """save → load preserves every field."""

    def test_round_trip_all_fields(self) -> None:
        original = PushState(
            accepted_sha256="abc123",
            accepted_at="2025-01-01T00:00:00Z",
            server_status="ok",
            rejected_fingerprint="snap-xyz",
            rejected_reason="schema mismatch",
            last_failure="timeout after 30s",
        )
        path = self.dir / "state.json"
        save_state(path, original)
        loaded = load_state(path)

        self.assertEqual(loaded, original)

    def test_round_trip_empty_state(self) -> None:
        original = PushState()
        path = self.dir / "state.json"
        save_state(path, original)
        loaded = load_state(path)
        self.assertEqual(loaded, original)


class TestAtomicReplace(_TempDirCase):
    """No temporary files are left after save."""

    def test_no_temp_files_remain(self) -> None:
        path = self.dir / "state.json"
        save_state(path, PushState(accepted_sha256="deadbeef"))

        siblings = list(path.parent.iterdir())
        self.assertEqual(len(siblings), 1)
        self.assertTrue(siblings[0] == path)

    def test_partial_write_does_not_corrupt(self) -> None:
        """If a previous valid state exists, a failed save must not clobber it."""
        path = self.dir / "state.json"
        good = PushState(accepted_sha256="original", accepted_at="2025-01-01")
        save_state(path, good)

        # Simulate a crash mid-write: leave a truncated temp file behind.
        leftover = path.parent / "state.json.tmp"
        leftover.write_text('{"accepted_sha256": "parti')

        loaded = load_state(path)
        self.assertEqual(loaded, good)


class TestParentDirAutoCreate(_TempDirCase):
    """save_state creates missing parent directories."""

    def test_creates_nested_dirs(self) -> None:
        path = self.dir / "deep" / "nested" / "state.json"
        save_state(path, PushState(accepted_sha256="nested"))
        self.assertTrue(path.exists())
        loaded = load_state(path)
        self.assertEqual(loaded.accepted_sha256, "nested")


class TestCorruptState(_TempDirCase):
    """A corrupt state file raises a clear error and leaves the file intact."""

    def test_corrupt_raises_and_preserves(self) -> None:
        path = self.dir / "state.json"
        corrupt_content = '{"accepted_sha256": "abc", broken'
        path.write_text(corrupt_content)

        with self.assertRaises(Exception) as ctx:
            load_state(path)
        # The error message should hint at the nature of the problem.
        self.assertIn("state", str(ctx.exception).lower())

        # Original file must not be overwritten.
        self.assertEqual(path.read_text(), corrupt_content)


class TestStateContents(_TempDirCase):
    """The on-disk JSON contains only allowed keys — never tokens."""

    def test_only_allowed_keys(self) -> None:
        state = PushState(
            accepted_sha256="sha-value",
            accepted_at="2025-01-01T00:00:00Z",
            server_status="ok",
            rejected_fingerprint="fp-value",
            rejected_reason="bad payload",
            last_failure="conn refused",
        )
        path = self.dir / "state.json"
        save_state(path, state)

        raw = json.loads(path.read_text())
        allowed = {
            "accepted_sha256",
            "accepted_at",
            "server_status",
            "rejected_fingerprint",
            "rejected_reason",
            "last_failure",
        }
        self.assertEqual(set(raw.keys()), allowed)

    def test_no_token_field_present(self) -> None:
        save_state(self.dir / "state.json", PushState(accepted_sha256="x"))
        raw = self.dir.joinpath("state.json").read_text()
        self.assertNotIn("token", raw.lower())


class TestFilePermissions(_TempDirCase):
    """On POSIX the state file should be mode 0600."""

    @unittest.skipUnless(sys.platform != "win32", "POSIX-only")
    def test_mode_0600(self) -> None:
        path = self.dir / "state.json"
        save_state(path, PushState())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)


class TestImmutability(unittest.TestCase):
    """PushState is a frozen dataclass."""

    def test_frozen(self) -> None:
        state = PushState()
        with self.assertRaises(Exception):
            state.accepted_sha256 = "mutated"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
