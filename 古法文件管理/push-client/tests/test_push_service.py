"""Tests for push_service — one-shot and watch orchestration.

Written before implementation, following strict TDD.  Uses injected
fake snapshot/transport/state/clock/random/sleep; no network or file
I/O beyond temp state files.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

from health_bridge.push_config import PushConfig
from health_bridge.push_service import (
    Dependencies,
    PushOutcome,
    run_once,
    run_watch,
)
from health_bridge.push_snapshot import PreparedSnapshot
from health_bridge.push_state import PushState, load_state, save_state
from health_bridge.push_transport import (
    PermanentUploadError,
    TransientUploadError,
    UploadResult,
)

_LEAK_PROBE = "LEAK-PROBE-zzz999zzz"


# ── helpers ──────────────────────────────────────────────────────────


def _make_config(
    *,
    state_path: Path,
    source_path: Path = Path("/tmp/source.db"),
    poll_interval_seconds: float = 0.01,
    max_retries: int = 3,
    upload_token: str = "test-token-abc123",
) -> PushConfig:
    return PushConfig(
        source_path=source_path,
        upload_url="https://upload.example.com/api/v1/upload",
        state_path=state_path,
        poll_interval_seconds=poll_interval_seconds,
        stability_delay_seconds=0.01,
        request_timeout_seconds=120,
        max_retries=max_retries,
        max_uncompressed_bytes=104_857_600,
        chunk_size=4096,
        max_response_bytes=1_048_576,
        token_env="HEALTH_UPLOAD_TOKEN",
        token_file=None,
        upload_token=upload_token,
    )


def _make_snapshot(
    sha256: str = "a" * 64,
    tmpdir: Path | None = None,
) -> PreparedSnapshot:
    """Build a PreparedSnapshot pointing at a real gzip file."""
    if tmpdir is None:
        tmpdir = Path(tempfile.mkdtemp())
    tmpdir.mkdir(parents=True, exist_ok=True)
    gzip_path = tmpdir / "snapshot.db.gz"
    with gzip.open(gzip_path, "wb") as f:
        f.write(b"test content")
    return PreparedSnapshot(
        source_path=tmpdir / "source.db",
        staged_db_path=tmpdir / "staged.db",
        gzip_path=gzip_path,
        sha256=sha256,
        uncompressed_bytes=12,
        compressed_bytes=gzip_path.stat().st_size,
    )


class _FakeSnapshotFactory:
    """Replaces prepare_snapshot; yields a PreparedSnapshot or raises."""

    def __init__(self, snapshot: PreparedSnapshot):
        self._snapshot = snapshot

    def __call__(self, config: PushConfig, **kwargs: Any) -> Any:
        @contextmanager
        def cm(config_inner: PushConfig, **kw: Any) -> Iterator[PreparedSnapshot]:
            yield self._snapshot

        return cm(config, **kwargs)


class _ScriptedTransport:
    """Returns scripted UploadResults or raises scripted exceptions.

    Each call to __call__ pops from the front of the script list, so
    retry logic can be tested by chaining outcomes.
    """

    def __init__(self, script: list[Any]):
        self._script = list(script)
        self.calls: list[PreparedSnapshot] = []

    def __call__(self, config: PushConfig, snapshot: PreparedSnapshot, **kwargs: Any) -> UploadResult:
        self.calls.append(snapshot)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def remaining(self) -> int:
        return len(self._script)


class _FakeClock:
    """Returns increasing timestamps and records every read."""

    def __init__(self, start: float = 1_000_000.0):
        self._now = start
        self.reads: list[float] = []

    def __call__(self) -> float:
        self.reads.append(self._now)
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeSleep:
    """Records all sleep durations without actually sleeping."""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _RecordingRandom:
    """Returns a fixed jitter value for deterministic backoff testing."""

    def __init__(self, value: float = 0.5):
        self._value = value

    def __call__(self) -> float:
        return self._value


# ── test base ────────────────────────────────────────────────────────


class _ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state_path = self.dir / "state.json"
        self.snapshot_dir = self.dir / "snapshots"
        self.snapshot_dir.mkdir()
        self.snapshot = _make_snapshot(tmpdir=self.snapshot_dir)
        self.clock = _FakeClock()
        self.sleep = _FakeSleep()
        self.rand = _RecordingRandom()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _deps(
        self,
        transport_script: list[Any],
        snapshot: PreparedSnapshot | None = None,
        state: PushState | None = None,
    ) -> tuple[Dependencies, _ScriptedTransport]:
        snap = snapshot or self.snapshot
        snapshot_factory = _FakeSnapshotFactory(snap)
        transport = _ScriptedTransport(transport_script)
        if state is not None:
            save_state(self.state_path, state)
        deps = Dependencies(
            prepare_snapshot=snapshot_factory,
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )
        return deps, transport

    def _config(self, **kwargs: Any) -> PushConfig:
        defaults: dict[str, Any] = dict(
            state_path=self.state_path,
            source_path=self.dir / "source.db",
        )
        defaults.update(kwargs)
        return _make_config(**defaults)


# ── one-shot: duplicate skip ─────────────────────────────────────────


class TestDuplicateSkip(_ServiceCase):
    def test_prepared_sha_equals_accepted_skips_transport(self) -> None:
        sha = "b" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        existing = PushState(accepted_sha256=sha, accepted_at="2025-01-01T00:00:00Z")
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
            state=existing,
        )
        outcome = run_once(self._config(), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.DUPLICATE)
        # Transport must not have been called.
        self.assertEqual(len(transport.calls), 0)
        # State unchanged.
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)


# ── one-shot: HTTP 200/201 updates accepted state ─────────────────────


class TestSuccessUpdatesState(_ServiceCase):
    def test_http_200_updates_accepted_sha(self) -> None:
        sha = "c" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.UPLOADED)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)
        self.assertEqual(loaded.server_status, "ok")
        self.assertIsNotNone(loaded.accepted_at)
        # accepted_at should look like an ISO timestamp.
        self.assertIn("T", loaded.accepted_at or "")

    def test_http_201_updates_accepted_sha(self) -> None:
        sha = "d" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(201, "created", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.UPLOADED)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)
        self.assertEqual(loaded.server_status, "created")


# ── one-shot: HTTP 202 unsupported_schema ──────────────────────────────


class TestUnsupportedSchema(_ServiceCase):
    def test_202_unsupported_schema_delivered_with_warning(self) -> None:
        sha = "e" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(
                    202, "unsupported_schema", True,
                    {"status": "unsupported_schema", "detail": "schema v2 not supported"},
                ),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.UNSUPPORTED_SCHEMA)
        loaded = load_state(self.state_path)
        # Accepted hash is recorded so we don't re-send the same snapshot.
        self.assertEqual(loaded.accepted_sha256, sha)
        # Server status reflects the warning.
        self.assertEqual(loaded.server_status, "unsupported_schema")


# ── one-shot: permanent error does not retry ─────────────────────────


class TestPermanentNoRetry(_ServiceCase):
    def test_401_no_retry(self) -> None:
        snap = _make_snapshot(sha256="f" * 64, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                PermanentUploadError("Upload rejected by server (HTTP 401)"),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(max_retries=5), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.PERMANENT_FAILURE)
        # Only one attempt.
        self.assertEqual(len(transport.calls), 1)

    def test_403_no_retry(self) -> None:
        snap = _make_snapshot(sha256="1" * 64, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                PermanentUploadError("Upload rejected by server (HTTP 403)"),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(max_retries=5), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.PERMANENT_FAILURE)
        self.assertEqual(len(transport.calls), 1)

    def test_422_no_retry(self) -> None:
        snap = _make_snapshot(sha256="2" * 64, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                PermanentUploadError("Upload rejected by server (HTTP 422)"),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(max_retries=5), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.PERMANENT_FAILURE)
        self.assertEqual(len(transport.calls), 1)

    def test_permanent_failure_records_state(self) -> None:
        sha = "3" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                PermanentUploadError("Upload rejected by server (HTTP 422)"),
            ],
            snapshot=snap,
        )
        run_once(self._config(max_retries=5), dependencies=deps)
        loaded = load_state(self.state_path)
        self.assertIsNotNone(loaded.last_failure)
        self.assertIn("422", loaded.last_failure or "")


# ── one-shot: max_retries semantics ─────────────────────────────────


class TestMaxRetriesSemantics(_ServiceCase):
    def test_max_retries_means_additional_attempts(self) -> None:
        """max_retries=2 means 1 initial + 2 retries = 3 total attempts."""
        snap = _make_snapshot(sha256="4" * 64, tmpdir=self.snapshot_dir)
        script: list[Any] = [
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
        ]
        deps, transport = self._deps(transport_script=script, snapshot=snap)
        outcome = run_once(self._config(max_retries=2), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.TRANSIENT_EXHAUSTED)
        self.assertEqual(len(transport.calls), 3)

    def test_max_retries_zero_single_attempt(self) -> None:
        snap = _make_snapshot(sha256="5" * 64, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                TransientUploadError("HTTP 500"),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(max_retries=0), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.TRANSIENT_EXHAUSTED)
        self.assertEqual(len(transport.calls), 1)

    def test_succeeds_on_retry(self) -> None:
        sha = "6" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                TransientUploadError("HTTP 500"),
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(max_retries=3), dependencies=deps)
        self.assertEqual(outcome, PushOutcome.UPLOADED)
        self.assertEqual(len(transport.calls), 2)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)


# ── one-shot: exponential backoff with cap and jitter ────────────────


class TestBackoff(_ServiceCase):
    def test_exponential_backoff_capped_and_jittered(self) -> None:
        snap = _make_snapshot(sha256="7" * 64, tmpdir=self.snapshot_dir)
        script: list[Any] = [
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
        ]
        deps, transport = self._deps(transport_script=script, snapshot=snap)
        run_once(self._config(max_retries=3), dependencies=deps)

        # 3 retries → 3 sleep calls (not 4, last attempt doesn't sleep).
        self.assertEqual(len(self.sleep.calls), 3)

        # Exponential: base * 2^attempt * jitter, capped at some max.
        # With _RecordingRandom returning 0.5 and base=1.0:
        #   retry 0: 1.0 * 2^0 * 0.5 = 0.5
        #   retry 1: 1.0 * 2^1 * 0.5 = 1.0
        #   retry 2: 1.0 * 2^2 * 0.5 = 2.0
        self.assertAlmostEqual(self.sleep.calls[0], 0.5, places=2)
        self.assertAlmostEqual(self.sleep.calls[1], 1.0, places=2)
        self.assertAlmostEqual(self.sleep.calls[2], 2.0, places=2)

    def test_backoff_capped_at_maximum(self) -> None:
        snap = _make_snapshot(sha256="8" * 64, tmpdir=self.snapshot_dir)
        # Many retries so backoff reaches the cap.
        script: list[Any] = [TransientUploadError("HTTP 500")] * 10
        deps, transport = self._deps(transport_script=script, snapshot=snap)
        run_once(self._config(max_retries=9), dependencies=deps)

        # None of the sleeps should exceed a reasonable cap (60s).
        for delay in self.sleep.calls:
            self.assertLessEqual(delay, 60.0)


# ── one-shot: dry run ────────────────────────────────────────────────


class TestDryRun(_ServiceCase):
    def test_dry_run_does_not_call_transport(self) -> None:
        sha = "9" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )
        outcome = run_once(self._config(), dry_run=True, dependencies=deps)
        self.assertEqual(outcome, PushOutcome.DRY_RUN)
        self.assertEqual(len(transport.calls), 0)
        # State should still record the snapshot as if delivered.
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)


# ── watch mode: recovery after failure ───────────────────────────────


class TestWatchRecovery(_ServiceCase):
    def test_watch_recovers_after_transient_failure(self) -> None:
        sha1 = "a" * 64
        sha2 = "b" * 64
        snap1 = _make_snapshot(sha256=sha1, tmpdir=self.snapshot_dir / "v1")
        (self.snapshot_dir / "v1").mkdir(exist_ok=True)

        # First poll: transient failure.
        # Second poll: success (different snapshot).
        snap2 = _make_snapshot(sha256=sha2, tmpdir=self.snapshot_dir / "v2")
        (self.snapshot_dir / "v2").mkdir(exist_ok=True)

        snapshots = [snap1, snap2]
        transports: list[Any] = [
            TransientUploadError("HTTP 500"),
            UploadResult(200, "ok", True, {"status": "ok"}),
        ]

        call_count = [0]

        class _SwitchingSnapshotFactory:
            def __call__(self_inner, config: PushConfig, **kwargs: Any) -> Any:
                idx = call_count[0]
                snap = snapshots[min(idx, len(snapshots) - 1)]
                return _FakeSnapshotFactory(snap)(config, **kwargs)

        transport = _ScriptedTransport(transports)

        # A counter to limit watch iterations.
        poll_count = [0]

        deps = Dependencies(
            prepare_snapshot=_SwitchingSnapshotFactory(),
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )

        # To limit watch iterations, patch sleep to raise KeyboardInterrupt
        # after enough polls.
        original_sleep = self.sleep

        def _interrupt_after(n: int) -> Callable[[float], None]:
            state = {"count": 0}

            def _sleep(seconds: float) -> None:
                state["count"] += 1
                original_sleep(seconds)
                if state["count"] >= n:
                    raise KeyboardInterrupt

            return _sleep

        deps.sleep = _interrupt_after(2)

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        self.assertEqual(exit_code, 0)
        # The transient error was recovered via retry within the first poll.
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha1)

    def test_watch_survives_source_missing(self) -> None:
        """Watch must not terminate when source file is absent."""

        call_count = [0]

        class _MissingThenOkSnapshot:
            def __init__(self_inner):
                self_inner._ok = _FakeSnapshotFactory(
                    _make_snapshot(sha256="c" * 64, tmpdir=self.snapshot_dir / "ok")
                )
                (self.snapshot_dir / "ok").mkdir(exist_ok=True)

            def __call__(self_inner, config: PushConfig, **kwargs: Any) -> Any:
                call_count[0] += 1
                if call_count[0] == 1:
                    raise FileNotFoundError("Source database not found")
                return self_inner._ok(config, **kwargs)

        transport = _ScriptedTransport([
            UploadResult(200, "ok", True, {"status": "ok"}),
        ])

        deps = Dependencies(
            prepare_snapshot=_MissingThenOkSnapshot(),
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )

        original_sleep = self.sleep

        def _interrupt_after(n: int) -> Callable[[float], None]:
            state = {"count": 0}

            def _sleep(seconds: float) -> None:
                state["count"] += 1
                original_sleep(seconds)
                if state["count"] >= n:
                    raise KeyboardInterrupt

            return _sleep

        deps.sleep = _interrupt_after(2)

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        self.assertEqual(exit_code, 0)
        # The missing-file iteration should not have called transport.
        self.assertEqual(len(transport.calls), 1)

    def test_watch_survives_transient_upload_error(self) -> None:
        """Transient upload errors in watch mode are logged, not fatal."""

        sha = "d" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)

        snapshot_factory = _FakeSnapshotFactory(snap)

        transport = _ScriptedTransport([
            TransientUploadError("HTTP 500"),
            TransientUploadError("HTTP 500"),
            UploadResult(200, "ok", True, {"status": "ok"}),
        ])

        deps = Dependencies(
            prepare_snapshot=snapshot_factory,
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )

        original_sleep = self.sleep

        def _interrupt_after(n: int) -> Callable[[float], None]:
            state = {"count": 0}

            def _sleep(seconds: float) -> None:
                state["count"] += 1
                original_sleep(seconds)
                if state["count"] >= n:
                    raise KeyboardInterrupt

            return _sleep

        deps.sleep = _interrupt_after(3)

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        self.assertEqual(exit_code, 0)
        loaded = load_state(self.state_path)
        self.assertEqual(loaded.accepted_sha256, sha)


# ── watch mode: auth failure terminates ──────────────────────────────


class TestWatchAuthFailure(_ServiceCase):
    def test_auth_failure_terminates_watch(self) -> None:
        """401/403 during watch must return exit code 2."""
        sha = "e" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                PermanentUploadError("Upload rejected by server (HTTP 401)"),
            ],
            snapshot=snap,
        )
        exit_code = run_watch(self._config(), dependencies=deps)
        self.assertEqual(exit_code, 2)


# ── watch mode: permanent rejection (413/422) waits for source change ─

class TestWatchRejectedSnapshot(_ServiceCase):
    def test_422_records_rejected_fingerprint_and_waits(self) -> None:
        """422 records rejected fingerprint; watch waits for source change."""
        sha1 = "f" * 64
        sha2 = "a1" + "b" * 62

        snap1 = _make_snapshot(sha256=sha1, tmpdir=self.snapshot_dir / "r1")
        (self.snapshot_dir / "r1").mkdir(exist_ok=True)
        snap2 = _make_snapshot(sha256=sha2, tmpdir=self.snapshot_dir / "r2")
        (self.snapshot_dir / "r2").mkdir(exist_ok=True)

        snapshots = [snap1, snap2, snap2]
        transports: list[Any] = [
            PermanentUploadError("Upload rejected by server (HTTP 422)"),
            UploadResult(200, "ok", True, {"status": "ok"}),
        ]
        call_count = [0]

        class _SwitchingSnapshotFactory:
            def __call__(self_inner, config: PushConfig, **kwargs: Any) -> Any:
                idx = call_count[0]
                call_count[0] += 1
                snap = snapshots[min(idx, len(snapshots) - 1)]
                return _FakeSnapshotFactory(snap)(config, **kwargs)

        transport = _ScriptedTransport(transports)

        deps = Dependencies(
            prepare_snapshot=_SwitchingSnapshotFactory(),
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )

        original_sleep = self.sleep

        def _interrupt_after(n: int) -> Callable[[float], None]:
            state = {"count": 0}

            def _sleep(seconds: float) -> None:
                state["count"] += 1
                original_sleep(seconds)
                if state["count"] >= n:
                    raise KeyboardInterrupt

            return _sleep

        deps.sleep = _interrupt_after(2)

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        # Watch exited cleanly via KeyboardInterrupt.
        self.assertEqual(exit_code, 0)
        # The rejected fingerprint should be recorded.
        loaded = load_state(self.state_path)
        # Either the second snapshot succeeded (clearing rejected_fingerprint)
        # or the first snapshot's rejection is recorded.
        self.assertTrue(
            loaded.accepted_sha256 == sha2 or loaded.rejected_fingerprint is not None
        )

    def test_rejected_snapshot_not_retried_each_poll(self) -> None:
        """Once a snapshot is rejected, it should not be re-uploaded on
        every poll iteration — only when the source changes."""
        sha = "1" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)

        existing = PushState(
            accepted_sha256=None,
            rejected_fingerprint=sha,
            rejected_reason="HTTP 422",
        )

        transport = _ScriptedTransport([])

        snapshot_factory = _FakeSnapshotFactory(snap)

        deps = Dependencies(
            prepare_snapshot=snapshot_factory,
            upload_snapshot=transport,
            load_state=lambda p: load_state(p),
            save_state=lambda p, s: save_state(p, s),
            clock=self.clock,
            random=self.rand,
            sleep=self.sleep,
        )

        original_sleep = self.sleep

        def _interrupt_after(n: int) -> Callable[[float], None]:
            state = {"count": 0}

            def _sleep(seconds: float) -> None:
                state["count"] += 1
                original_sleep(seconds)
                if state["count"] >= n:
                    raise KeyboardInterrupt

            return _sleep

        deps.sleep = _interrupt_after(3)

        # Save the rejected state before running watch.
        save_state(self.state_path, existing)

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        self.assertEqual(exit_code, 0)
        # Transport should never have been called because the snapshot
        # hash matches the rejected fingerprint.
        self.assertEqual(len(transport.calls), 0)


# ── watch mode: keyboard interrupt clean exit ────────────────────────


class TestKeyboardInterrupt(_ServiceCase):
    def test_keyboard_interrupt_returns_zero(self) -> None:
        sha = "2" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )

        # First poll succeeds, then the poll_interval sleep raises.
        original_sleep = self.sleep

        def _sleep_then_interrupt(seconds: float) -> None:
            original_sleep(seconds)
            raise KeyboardInterrupt

        deps.sleep = _sleep_then_interrupt

        exit_code = run_watch(self._config(poll_interval_seconds=0.01), dependencies=deps)
        self.assertEqual(exit_code, 0)


# ── watch mode: transient exhausted exit code ─────────────────────────


class TestWatchTransientExhausted(_ServiceCase):
    def test_transient_exhausted_exit_code_3(self) -> None:
        """When max_retries is exhausted in watch mode with a transient
        error, and the config policy says to terminate, return 3."""
        sha = "3" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                TransientUploadError("HTTP 500"),
                TransientUploadError("HTTP 500"),
                TransientUploadError("HTTP 500"),
                TransientUploadError("HTTP 500"),
            ],
            snapshot=snap,
        )
        exit_code = run_watch(self._config(max_retries=2), dependencies=deps)
        self.assertEqual(exit_code, 3)


# ── token / data leakage in logs ─────────────────────────────────────


class TestNoTokenLeakage(_ServiceCase):
    def test_token_not_in_log_records(self) -> None:
        """Token must never appear in log output."""
        import io
        import logging

        sha = "4" * 64
        snap = _make_snapshot(sha256=sha, tmpdir=self.snapshot_dir)
        config = self._config(upload_token=_LEAK_PROBE)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("health_bridge.push_service")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            run_once(config, dependencies=deps)
            log_output = buf.getvalue()
            self.assertNotIn(_LEAK_PROBE, log_output)
        finally:
            logger.removeHandler(handler)

    def test_only_abbreviated_hash_in_logs(self) -> None:
        """Logs must contain only abbreviated SHA-256, never the full hash."""
        import io
        import logging

        full_sha = "5" * 64
        snap = _make_snapshot(sha256=full_sha, tmpdir=self.snapshot_dir)
        deps, transport = self._deps(
            transport_script=[
                UploadResult(200, "ok", True, {"status": "ok"}),
            ],
            snapshot=snap,
        )

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger("health_bridge.push_service")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            run_once(self._config(), dependencies=deps)
            log_output = buf.getvalue()
            # The full 64-char hex string must not appear.
            self.assertNotIn(full_sha, log_output)
        finally:
            logger.removeHandler(handler)


# ── Dependencies default ─────────────────────────────────────────────


class TestDependenciesDefault(unittest.TestCase):
    def test_default_dependencies_uses_real_functions(self) -> None:
        """When no Dependencies is passed, production functions are used."""
        deps = Dependencies()
        # Should have callable attributes.
        self.assertTrue(callable(deps.prepare_snapshot))
        self.assertTrue(callable(deps.upload_snapshot))
        self.assertTrue(callable(deps.load_state))
        self.assertTrue(callable(deps.save_state))
        self.assertTrue(callable(deps.clock))
        self.assertTrue(callable(deps.random))
        self.assertTrue(callable(deps.sleep))


if __name__ == "__main__":
    unittest.main()
