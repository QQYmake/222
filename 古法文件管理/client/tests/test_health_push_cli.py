"""Tests for health_push CLI entry point.

Uses unittest.mock to isolate from real config loading and service
execution.  No network or file I/O beyond temp state files.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from clients.health_bridge.push_config import PushConfig
from clients.health_bridge.push_service import PushOutcome
from clients.health_push import main


_DEFAULT_SOURCE = "/storage/emulated/0/Download/health/Gadgetbridge.db"
_DEFAULT_URL = "https://oh-my-frontweb.duckdns.org/health/api/v1/upload"


def _make_config(**overrides) -> PushConfig:
    defaults = dict(
        source_path=Path(_DEFAULT_SOURCE),
        upload_url=_DEFAULT_URL,
        state_path=Path("/tmp/push-state.json"),
        poll_interval_seconds=900,
        stability_delay_seconds=5,
        request_timeout_seconds=120,
        max_retries=5,
        max_uncompressed_bytes=104_857_600,
        chunk_size=1_048_576,
        max_response_bytes=1_048_576,
        token_env="HEALTH_UPLOAD_TOKEN",
        token_file=None,
        upload_base_url="https://oh-my-frontweb.duckdns.org",
        upload_token="test-token",
    )
    defaults.update(overrides)
    return PushConfig(**defaults)


# ── Help output ──────────────────────────────────────────────────────

class TestHelpOutput(unittest.TestCase):
    def test_help_contains_default_source(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertIn(_DEFAULT_SOURCE, fake_out.getvalue())

    def test_help_contains_default_url(self):
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            with self.assertRaises(SystemExit):
                main(["--help"])
        self.assertIn(_DEFAULT_URL, fake_out.getvalue())


# ── once command ─────────────────────────────────────────────────────

class TestOnceCommand(unittest.TestCase):
    def _run_once(self, outcome: PushOutcome) -> int:
        config = _make_config()

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            return outcome

        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            return main(["once"])

    def test_once_uploaded_exit_0(self):
        self.assertEqual(self._run_once(PushOutcome.UPLOADED), 0)

    def test_once_duplicate_exit_0(self):
        self.assertEqual(self._run_once(PushOutcome.DUPLICATE), 0)

    def test_once_unsupported_schema_exit_0(self):
        self.assertEqual(self._run_once(PushOutcome.UNSUPPORTED_SCHEMA), 0)

    def test_once_permanent_failure_exit_2(self):
        self.assertEqual(self._run_once(PushOutcome.PERMANENT_FAILURE), 2)

    def test_once_transient_exhausted_exit_3(self):
        self.assertEqual(self._run_once(PushOutcome.TRANSIENT_EXHAUSTED), 3)

    def test_once_passes_dry_run_false(self):
        config = _make_config()
        captured = []

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            captured.append(dry_run)
            return PushOutcome.UPLOADED

        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            main(["once"])

        self.assertFalse(captured[0])


# ── dry-run command ──────────────────────────────────────────────────

class TestDryRunCommand(unittest.TestCase):
    def test_dry_run_exit_0(self):
        config = _make_config(upload_token=None)
        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_once", return_value=PushOutcome.DRY_RUN):
            exit_code = main(["dry-run"])
        self.assertEqual(exit_code, 0)

    def test_dry_run_passes_dry_run_true(self):
        config = _make_config(upload_token=None)
        captured = []

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            captured.append(dry_run)
            return PushOutcome.DRY_RUN

        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            main(["dry-run"])

        self.assertTrue(captured[0])

    def test_dry_run_no_token_needed(self):
        """dry-run should work even with an empty environment."""
        config = _make_config(upload_token=None)
        load_calls = []

        def fake_load(path, env, *, dry_run):
            load_calls.append(dry_run)
            return config

        with patch("clients.health_push.load_push_config", side_effect=fake_load), \
             patch("clients.health_push.run_once", return_value=PushOutcome.DRY_RUN):
            main(["dry-run"], environ={})

        self.assertTrue(load_calls[0])


# ── watch command ────────────────────────────────────────────────────

class TestWatchCommand(unittest.TestCase):
    def test_watch_returns_run_watch_exit_code(self):
        config = _make_config()
        for expected in (0, 2, 3):
            with patch("clients.health_push.load_push_config", return_value=config), \
                 patch("clients.health_push.run_watch", return_value=expected):
                exit_code = main(["watch"])
            self.assertEqual(exit_code, expected)

    def test_watch_passes_config(self):
        config = _make_config()
        captured = []

        def fake_run_watch(cfg, *, dependencies=None):
            captured.append(cfg)
            return 0

        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_watch", side_effect=fake_run_watch):
            main(["watch"])

        self.assertEqual(captured[0], config)


# ── --source override ────────────────────────────────────────────────

class TestSourceOverride(unittest.TestCase):
    def test_source_override_applied(self):
        original = _make_config(source_path=Path("/original/path.db"))
        captured = []

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            captured.append(cfg)
            return PushOutcome.UPLOADED

        with patch("clients.health_push.load_push_config", return_value=original), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            exit_code = main(["once", "--source", "/override/path.db"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured[0].source_path, Path("/override/path.db"))

    def test_no_source_uses_config_default(self):
        original = _make_config(source_path=Path("/original/path.db"))
        captured = []

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            captured.append(cfg)
            return PushOutcome.UPLOADED

        with patch("clients.health_push.load_push_config", return_value=original), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            exit_code = main(["once"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured[0].source_path, Path("/original/path.db"))

    def test_source_override_works_for_watch(self):
        original = _make_config(source_path=Path("/original/path.db"))
        captured = []

        def fake_run_watch(cfg, *, dependencies=None):
            captured.append(cfg)
            return 0

        with patch("clients.health_push.load_push_config", return_value=original), \
             patch("clients.health_push.run_watch", side_effect=fake_run_watch):
            main(["watch", "--source", "/override/path.db"])

        self.assertEqual(captured[0].source_path, Path("/override/path.db"))

    def test_source_override_works_for_dry_run(self):
        original = _make_config(
            source_path=Path("/original/path.db"), upload_token=None,
        )
        captured = []

        def fake_run_once(cfg, *, dry_run=False, dependencies=None):
            captured.append(cfg)
            return PushOutcome.DRY_RUN

        with patch("clients.health_push.load_push_config", return_value=original), \
             patch("clients.health_push.run_once", side_effect=fake_run_once):
            main(["dry-run", "--source", "/override/path.db"])

        self.assertEqual(captured[0].source_path, Path("/override/path.db"))


# ── --config option ──────────────────────────────────────────────────

class TestConfigOption(unittest.TestCase):
    def test_config_passed_to_load_push_config(self):
        config = _make_config()
        captured = []

        def fake_load(path, env, *, dry_run):
            captured.append(path)
            return config

        with patch("clients.health_push.load_push_config", side_effect=fake_load), \
             patch("clients.health_push.run_once", return_value=PushOutcome.UPLOADED):
            main(["once", "--config", "/path/to/config.json"])

        self.assertEqual(captured[0], Path("/path/to/config.json"))

    def test_no_config_passes_none(self):
        config = _make_config()
        captured = []

        def fake_load(path, env, *, dry_run):
            captured.append(path)
            return config

        with patch("clients.health_push.load_push_config", side_effect=fake_load), \
             patch("clients.health_push.run_once", return_value=PushOutcome.UPLOADED):
            main(["once"])

        self.assertIsNone(captured[0])


# ── config errors ────────────────────────────────────────────────────

class TestConfigError(unittest.TestCase):
    def test_value_error_exit_2(self):
        with patch("clients.health_push.load_push_config",
                   side_effect=ValueError("bad config")):
            exit_code = main(["once"])
        self.assertEqual(exit_code, 2)

    def test_permission_error_exit_2(self):
        with patch("clients.health_push.load_push_config",
                   side_effect=PermissionError("bad perms")):
            exit_code = main(["once"])
        self.assertEqual(exit_code, 2)

    def test_file_not_found_error_exit_2(self):
        with patch("clients.health_push.load_push_config",
                   side_effect=FileNotFoundError("no config")):
            exit_code = main(["once"])
        self.assertEqual(exit_code, 2)


# ── source file not found in once mode ───────────────────────────────

class TestSourceNotFound(unittest.TestCase):
    def test_source_not_found_exit_2(self):
        config = _make_config()
        with patch("clients.health_push.load_push_config", return_value=config), \
             patch("clients.health_push.run_once",
                   side_effect=FileNotFoundError("no source file")):
            exit_code = main(["once"])
        self.assertEqual(exit_code, 2)


# ── subcommand required ──────────────────────────────────────────────

class TestSubcommandRequired(unittest.TestCase):
    def test_no_subcommand_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
