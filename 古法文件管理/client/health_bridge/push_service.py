"""One-shot and watch orchestration for the health-bridge push client.

Coordinates snapshot preparation, upload transport, and state persistence.
Retry logic lives here (not in the transport layer) so that watch mode
can distinguish fatal auth errors from transient network failures and
snapshot-specific rejections.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from clients.health_bridge.push_config import PushConfig
from clients.health_bridge.push_snapshot import PreparedSnapshot, prepare_snapshot
from clients.health_bridge.push_state import PushState, load_state, save_state
from clients.health_bridge.push_transport import (
    PermanentUploadError,
    TransientUploadError,
    UploadResult,
    upload_snapshot,
)

logger = logging.getLogger("health_bridge.push_service")

_SHA_ABBREV_LEN = 12
_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 60.0

# Snapshot-specific rejections: the data itself was rejected, so watch
# mode should wait for the source to change before retrying.
_REJECTION_STATUSES = frozenset({413, 422})

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class PushOutcome(Enum):
    UPLOADED = "uploaded"
    DUPLICATE = "duplicate"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DRY_RUN = "dry_run"
    PERMANENT_FAILURE = "permanent_failure"
    TRANSIENT_EXHAUSTED = "transient_exhausted"


@dataclass
class Dependencies:
    """Injectable function references for testing.

    In production, all fields default to the real module-level functions.
    Tests replace individual fields to inject fakes without touching I/O.
    """

    prepare_snapshot: Callable[..., Any] = field(default=prepare_snapshot)
    upload_snapshot: Callable[..., Any] = field(default=upload_snapshot)
    load_state: Callable[..., PushState] = field(default=load_state)
    save_state: Callable[..., None] = field(default=save_state)
    clock: Callable[[], float] = field(default=time.time)
    random: Callable[[], float] = field(default=random.random)
    sleep: Callable[[float], None] = field(default=time.sleep)


def run_once(
    config: PushConfig,
    *,
    dry_run: bool = False,
    dependencies: Dependencies | None = None,
) -> PushOutcome:
    """Prepare a snapshot and upload it (or skip if unchanged).

    Returns a :class:`PushOutcome` for the CLI to map to an exit code.
    ``FileNotFoundError`` from snapshot preparation propagates to the
    caller so that ``run_watch`` can catch it and retry on the next poll.
    """
    deps = dependencies or Dependencies()
    state = deps.load_state(config.state_path)

    with deps.prepare_snapshot(config) as snapshot:
        if snapshot.sha256 == state.accepted_sha256:
            logger.info("snapshot unchanged sha=%s", _abbreviate(snapshot.sha256))
            return PushOutcome.DUPLICATE

        # Previously rejected and source unchanged — don't retry.
        if snapshot.sha256 == state.rejected_fingerprint:
            logger.info("snapshot still rejected sha=%s", _abbreviate(snapshot.sha256))
            return PushOutcome.PERMANENT_FAILURE

        if dry_run:
            new_state = PushState(
                accepted_sha256=snapshot.sha256,
                accepted_at=_iso_now(deps.clock()),
                server_status="dry_run",
            )
            deps.save_state(config.state_path, new_state)
            logger.info("dry-run complete sha=%s", _abbreviate(snapshot.sha256))
            return PushOutcome.DRY_RUN

        return _attempt_upload(config, snapshot, deps)


def _attempt_upload(
    config: PushConfig,
    snapshot: PreparedSnapshot,
    deps: Dependencies,
) -> PushOutcome:
    max_attempts = config.max_retries + 1

    for attempt in range(max_attempts):
        try:
            result: UploadResult = deps.upload_snapshot(config, snapshot)
        except PermanentUploadError as exc:
            return _handle_permanent_error(config, snapshot, exc, deps)
        except TransientUploadError:
            if attempt < max_attempts - 1:
                delay = _compute_backoff(attempt, deps.random())
                deps.sleep(delay)
                continue
            new_state = PushState(last_failure="transient retries exhausted")
            deps.save_state(config.state_path, new_state)
            logger.warning(
                "transient retries exhausted sha=%s",
                _abbreviate(snapshot.sha256),
            )
            return PushOutcome.TRANSIENT_EXHAUSTED

        # Success path — 200/201/202 with delivered=True.
        if result.status == "unsupported_schema":
            new_state = PushState(
                accepted_sha256=snapshot.sha256,
                accepted_at=_iso_now(deps.clock()),
                server_status="unsupported_schema",
            )
            deps.save_state(config.state_path, new_state)
            logger.warning(
                "snapshot accepted but schema unsupported sha=%s",
                _abbreviate(snapshot.sha256),
            )
            return PushOutcome.UNSUPPORTED_SCHEMA

        new_state = PushState(
            accepted_sha256=snapshot.sha256,
            accepted_at=_iso_now(deps.clock()),
            server_status=result.status,
        )
        deps.save_state(config.state_path, new_state)
        logger.info("snapshot uploaded sha=%s", _abbreviate(snapshot.sha256))
        return PushOutcome.UPLOADED

    # Unreachable — the loop either returns or raises.
    return PushOutcome.TRANSIENT_EXHAUSTED


def _handle_permanent_error(
    config: PushConfig,
    snapshot: PreparedSnapshot,
    exc: PermanentUploadError,
    deps: Dependencies,
) -> PushOutcome:
    """Record state for a permanent failure and return the outcome.

    Snapshot-specific rejections (413/422) store a rejected fingerprint
    so watch mode can suppress retries until the source changes.  Auth
    errors (401/403) store only a failure summary so watch mode exits.
    """
    status = _extract_http_status(str(exc))

    if status in _REJECTION_STATUSES:
        new_state = PushState(
            rejected_fingerprint=snapshot.sha256,
            rejected_reason=f"HTTP {status}",
            last_failure=str(exc),
        )
        logger.warning(
            "snapshot rejected sha=%s status=%s",
            _abbreviate(snapshot.sha256),
            status,
        )
    else:
        new_state = PushState(last_failure=str(exc))
        logger.warning("permanent failure sha=%s", _abbreviate(snapshot.sha256))

    deps.save_state(config.state_path, new_state)
    return PushOutcome.PERMANENT_FAILURE


def run_watch(
    config: PushConfig,
    *,
    dependencies: Dependencies | None = None,
) -> int:
    """Run the push client in watch mode.

    Returns the process exit code:
    - 0: clean interruption (Ctrl-C)
    - 2: fatal configuration/authentication/permanent failure
    - 3: transient retries exhausted
    """
    deps = dependencies or Dependencies()

    while True:
        try:
            outcome = run_once(config, dependencies=deps)

            if outcome == PushOutcome.TRANSIENT_EXHAUSTED:
                logger.warning("transient retries exhausted, exiting")
                return 3

            if outcome == PushOutcome.PERMANENT_FAILURE:
                state = deps.load_state(config.state_path)
                # No rejected fingerprint means auth/config error — fatal.
                if state.rejected_fingerprint is None:
                    logger.error("fatal permanent failure, exiting")
                    return 2
                # Snapshot rejection — wait for source change.

            deps.sleep(config.poll_interval_seconds)

        except FileNotFoundError:
            logger.warning("source file not found, will retry next poll")
            deps.sleep(config.poll_interval_seconds)

        except KeyboardInterrupt:
            logger.info("interrupted by user, exiting")
            return 0


def _abbreviate(sha256: str) -> str:
    """Return the first 12 hex characters of a SHA-256 digest."""
    return sha256[:_SHA_ABBREV_LEN]


def _iso_now(epoch: float) -> str:
    """Format an epoch timestamp as a UTC ISO-8601 string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _compute_backoff(attempt: int, jitter: float) -> float:
    """Exponential backoff with jitter, capped at _BACKOFF_CAP."""
    delay = _BACKOFF_BASE * (2 ** attempt) * jitter
    return min(delay, _BACKOFF_CAP)


def _extract_http_status(message: str) -> int | None:
    """Extract an HTTP status code from an exception message."""
    match = _HTTP_STATUS_RE.search(message)
    return int(match.group(1)) if match else None
