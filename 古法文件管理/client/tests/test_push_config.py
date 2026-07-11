"""Tests for push_config — configuration loading and validation.

Every test here is written before the implementation exists, following
strict TDD: these must fail first, then pass once push_config.py is written.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from clients.health_bridge.push_config import PushConfig, load_push_config

# A sentinel token used to verify it never leaks into messages or repr.
_LEAK_PROBE = "LEAK-PROBE-zzz999zzz"


def _write_json(tmpdir: str, name: str, payload: dict) -> Path:
    p = Path(tmpdir) / name
    p.write_text(json.dumps(payload))
    return p


def _write_token_file(tmpdir: str, name: str, content: str, mode: int = 0o600) -> Path:
    p = Path(tmpdir) / name
    p.write_text(content)
    os.chmod(p, mode)
    return p


class DefaultsTests(unittest.TestCase):
    """Built-in defaults are correct when no config file is supplied."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_defaults_with_no_config_file(self):
        cfg = load_push_config(None, {}, dry_run=True)

        self.assertEqual(cfg.source_path,
                         Path("/storage/emulated/0/Download/health/Gadgetbridge.db"))
        self.assertEqual(cfg.upload_url,
                         "https://oh-my-frontweb.duckdns.org/health/api/v1/upload")
        self.assertEqual(cfg.state_path,
                         Path("~/.local/state/health-bridge/push-state.json").expanduser())
        self.assertEqual(cfg.poll_interval_seconds, 900)
        self.assertEqual(cfg.stability_delay_seconds, 5)
        self.assertEqual(cfg.request_timeout_seconds, 120)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.max_uncompressed_bytes, 104_857_600)
        self.assertEqual(cfg.chunk_size, 1_048_576)
        self.assertEqual(cfg.max_response_bytes, 1_048_576)
        self.assertEqual(cfg.token_env, "HEALTH_UPLOAD_TOKEN")
        self.assertIsNone(cfg.token_file)
        self.assertIsNone(cfg.upload_token)

    def test_state_path_tilde_expanded(self):
        cfg = load_push_config(None, {}, dry_run=True)
        self.assertNotIn("~", str(cfg.state_path))
        self.assertTrue(str(cfg.state_path).startswith(str(Path.home())))


class JsonLoadingTests(unittest.TestCase):
    """Non-secret values are loaded from a JSON config file."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_json_overrides_defaults(self):
        payload = {
            "source_path": "/data/custom.db",
            "upload_url": "https://custom.host/api",
            "state_path": "/tmp/state.json",
            "poll_interval_seconds": 30,
            "stability_delay_seconds": 2,
            "request_timeout_seconds": 45,
            "max_retries": 3,
            "max_uncompressed_bytes": 2048,
            "chunk_size": 4096,
            "max_response_bytes": 8192,
            "token_env": "CUSTOM_TOKEN",
            "token_file": None,
        }
        path = _write_json(self._tmp, "cfg.json", payload)
        cfg = load_push_config(path, {}, dry_run=True)

        self.assertEqual(cfg.source_path, Path("/data/custom.db"))
        self.assertEqual(cfg.upload_url, "https://custom.host/api")
        self.assertEqual(cfg.state_path, Path("/tmp/state.json"))
        self.assertEqual(cfg.poll_interval_seconds, 30.0)
        self.assertEqual(cfg.stability_delay_seconds, 2.0)
        self.assertEqual(cfg.request_timeout_seconds, 45.0)
        self.assertEqual(cfg.max_retries, 3)
        self.assertEqual(cfg.max_uncompressed_bytes, 2048)
        self.assertEqual(cfg.chunk_size, 4096)
        self.assertEqual(cfg.max_response_bytes, 8192)
        self.assertEqual(cfg.token_env, "CUSTOM_TOKEN")

    def test_partial_json_keeps_remaining_defaults(self):
        path = _write_json(self._tmp, "partial.json", {
            "upload_url": "https://partial.host/api",
        })
        cfg = load_push_config(path, {}, dry_run=True)

        self.assertEqual(cfg.upload_url, "https://partial.host/api")
        # Untouched fields keep defaults.
        self.assertEqual(cfg.poll_interval_seconds, 900)
        self.assertEqual(cfg.chunk_size, 1_048_576)


class TokenFromEnvTests(unittest.TestCase):
    """Token is read from the environment variable with highest priority."""

    def test_env_var_provides_token(self):
        cfg = load_push_config(
            None, {"HEALTH_UPLOAD_TOKEN": "env-secret"}, dry_run=False)
        self.assertEqual(cfg.upload_token, "env-secret")

    def test_custom_env_var_name(self):
        path = _write_json(tempfile.mkdtemp(), "c.json",
                           {"token_env": "MY_TOKEN"})
        cfg = load_push_config(path, {"MY_TOKEN": "custom-env"}, dry_run=False)
        self.assertEqual(cfg.upload_token, "custom-env")
        path.unlink()

    def test_env_var_overrides_token_file(self):
        tmp = tempfile.mkdtemp()
        token_file = _write_token_file(tmp, "tok", "file-secret\n")
        cfg_path = _write_json(tmp, "cfg.json", {"token_file": str(token_file)})

        cfg = load_push_config(
            cfg_path, {"HEALTH_UPLOAD_TOKEN": "env-secret"}, dry_run=False)
        self.assertEqual(cfg.upload_token, "env-secret")


class TokenFromFileTests(unittest.TestCase):
    """Token falls back to a file when the env var is absent."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_read_token_from_file(self):
        token_file = _write_token_file(self._tmp, "tok", "file-secret\n")
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        cfg = load_push_config(cfg_path, {}, dry_run=False)
        self.assertEqual(cfg.upload_token, "file-secret")

    def test_trailing_newline_stripped(self):
        token_file = _write_token_file(self._tmp, "tok", "abc\n\n\n")
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        cfg = load_push_config(cfg_path, {}, dry_run=False)
        self.assertEqual(cfg.upload_token, "abc")

    def test_no_trailing_newline_preserved(self):
        token_file = _write_token_file(self._tmp, "tok", "plain-token")
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        cfg = load_push_config(cfg_path, {}, dry_run=False)
        self.assertEqual(cfg.upload_token, "plain-token")

    @unittest.skipUnless(os.name != "nt",
                         "POSIX mode check is not applicable on Windows")
    def test_reject_token_file_with_group_permissions(self):
        token_file = _write_token_file(
            self._tmp, "tok", "secret\n", mode=0o640)
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        with self.assertRaises(PermissionError):
            load_push_config(cfg_path, {}, dry_run=False)

    @unittest.skipUnless(os.name != "nt",
                         "POSIX mode check is not applicable on Windows")
    def test_reject_token_file_with_world_permissions(self):
        token_file = _write_token_file(
            self._tmp, "tok", "secret\n", mode=0o604)
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        with self.assertRaises(PermissionError):
            load_push_config(cfg_path, {}, dry_run=False)

    @unittest.skipUnless(os.name != "nt",
                         "POSIX mode check is not applicable on Windows")
    def test_accept_owner_only_token_file(self):
        token_file = _write_token_file(
            self._tmp, "tok", "secret\n", mode=0o600)
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        cfg = load_push_config(cfg_path, {}, dry_run=False)
        self.assertEqual(cfg.upload_token, "secret")

    @unittest.skipUnless(os.name != "nt",
                         "POSIX mode check is not applicable on Windows")
    def test_accept_owner_read_only_token_file(self):
        token_file = _write_token_file(
            self._tmp, "tok", "secret\n", mode=0o400)
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        cfg = load_push_config(cfg_path, {}, dry_run=False)
        self.assertEqual(cfg.upload_token, "secret")


class TokenRequirementTests(unittest.TestCase):
    """Non-dry-run mode requires a token; dry-run does not."""

    def test_missing_token_rejected_when_not_dry_run(self):
        with self.assertRaises(ValueError) as ctx:
            load_push_config(None, {}, dry_run=False)
        self.assertIn("token", str(ctx.exception).lower())

    def test_dry_run_allows_missing_token(self):
        cfg = load_push_config(None, {}, dry_run=True)
        self.assertIsNone(cfg.upload_token)


class UrlValidationTests(unittest.TestCase):
    """Upload URL must use HTTPS."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reject_http_url(self):
        path = _write_json(self._tmp, "cfg.json",
                           {"upload_url": "http://insecure.example.com/api"})
        with self.assertRaises(ValueError):
            load_push_config(path, {}, dry_run=True)

    def test_reject_ftp_url(self):
        path = _write_json(self._tmp, "cfg.json",
                           {"upload_url": "ftp://example.com/api"})
        with self.assertRaises(ValueError):
            load_push_config(path, {}, dry_run=True)

    def test_accept_https_url(self):
        path = _write_json(self._tmp, "cfg.json",
                           {"upload_url": "https://secure.example.com/api"})
        cfg = load_push_config(path, {}, dry_run=True)
        self.assertEqual(cfg.upload_url, "https://secure.example.com/api")


class PositiveValueValidationTests(unittest.TestCase):
    """Intervals, timeouts, size limits, and chunk sizes must be positive."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _load_with(self, **overrides):
        payload = {"upload_url": "https://h.example.com/api"}
        payload.update(overrides)
        path = _write_json(self._tmp, "cfg.json", payload)
        return path

    def test_reject_zero_and_negative_poll_interval(self):
        for v in (0, -1, -0.5):
            path = self._load_with(poll_interval_seconds=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_zero_and_negative_stability_delay(self):
        for v in (0, -1):
            path = self._load_with(stability_delay_seconds=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_zero_and_negative_timeout(self):
        for v in (0, -1):
            path = self._load_with(request_timeout_seconds=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_zero_and_negative_max_uncompressed_bytes(self):
        for v in (0, -1):
            path = self._load_with(max_uncompressed_bytes=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_zero_and_negative_chunk_size(self):
        for v in (0, -1):
            path = self._load_with(chunk_size=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_zero_and_negative_max_response_bytes(self):
        for v in (0, -1):
            path = self._load_with(max_response_bytes=v)
            with self.assertRaises(ValueError):
                load_push_config(path, {}, dry_run=True)

    def test_reject_negative_max_retries(self):
        path = self._load_with(max_retries=-1)
        with self.assertRaises(ValueError):
            load_push_config(path, {}, dry_run=True)

    def test_zero_max_retries_allowed(self):
        path = self._load_with(max_retries=0)
        cfg = load_push_config(path, {}, dry_run=True)
        self.assertEqual(cfg.max_retries, 0)


class TokenLeakageTests(unittest.TestCase):
    """Token value must never appear in exception messages or repr."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_token_absent_from_url_validation_error(self):
        path = _write_json(self._tmp, "cfg.json",
                           {"upload_url": "http://leak.example.com/api"})
        with self.assertRaises(ValueError) as ctx:
            load_push_config(
                path, {"HEALTH_UPLOAD_TOKEN": _LEAK_PROBE}, dry_run=False)
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))

    def test_token_absent_from_missing_token_error(self):
        # Use a custom env var name that does not exist in environ.
        path = _write_json(self._tmp, "cfg.json",
                           {"token_env": "NONEXISTENT_TOKEN_VAR"})
        with self.assertRaises(ValueError) as ctx:
            load_push_config(path, {}, dry_run=False)
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))

    @unittest.skipUnless(os.name != "nt",
                         "POSIX mode check is not applicable on Windows")
    def test_token_absent_from_permission_error(self):
        token_file = _write_token_file(
            self._tmp, "tok", f"{_LEAK_PROBE}\n", mode=0o644)
        cfg_path = _write_json(self._tmp, "cfg.json",
                               {"token_file": str(token_file)})
        with self.assertRaises(PermissionError) as ctx:
            load_push_config(cfg_path, {}, dry_run=False)
        self.assertNotIn(_LEAK_PROBE, str(ctx.exception))

    def test_token_absent_from_repr(self):
        cfg = load_push_config(
            None, {"HEALTH_UPLOAD_TOKEN": _LEAK_PROBE}, dry_run=False)
        self.assertNotIn(_LEAK_PROBE, repr(cfg))

    def test_token_absent_from_str(self):
        cfg = load_push_config(
            None, {"HEALTH_UPLOAD_TOKEN": _LEAK_PROBE}, dry_run=False)
        self.assertNotIn(_LEAK_PROBE, str(cfg))


class FrozenDataclassTests(unittest.TestCase):
    """PushConfig is immutable."""

    def test_frozen(self):
        cfg = load_push_config(None, {}, dry_run=True)
        with self.assertRaises((AttributeError, TypeError)):
            cfg.poll_interval_seconds = 999  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
