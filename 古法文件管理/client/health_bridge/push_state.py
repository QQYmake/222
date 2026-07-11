"""Atomic on-disk persistence for the health-data push client.

State is stored as JSON and written atomically so that a crash mid-write
can never leave a truncated state file in place of a valid one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class PushState:
    """Snapshot of the last push outcome.

    ``rejected_fingerprint`` is a snapshot fingerprint (or hash) used to
    suppress retries when the underlying data has not changed.
    """

    accepted_sha256: str | None = None
    accepted_at: str | None = None
    server_status: str | None = None
    rejected_fingerprint: str | None = None
    rejected_reason: str | None = None
    last_failure: str | None = None


# Keys that are allowed in the JSON representation.  Anything else —
# especially credential-like fields — is rejected on read.
_ALLOWED_KEYS = frozenset(PushState.__dataclass_fields__.keys())


def load_state(path: Path) -> PushState:
    """Load state from *path*.

    Returns an empty :class:`PushState` when the file does not exist.
    Raises ``ValueError`` when the file exists but contains invalid JSON
    or unexpected keys; the file itself is never modified.
    """
    if not path.exists():
        return PushState()

    text = path.read_text(encoding="utf-8")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Corrupt state file {path}: invalid JSON — {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Corrupt state file {path}: expected a JSON object, got "
            f"{type(data).__name__}"
        )

    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"Corrupt state file {path}: unexpected keys {sorted(unknown)}"
        )

    return PushState(**{k: data[k] for k in _ALLOWED_KEYS if k in data})


def save_state(path: Path, state: PushState) -> None:
    """Atomically persist *state* to *path*.

    Writes a sibling temporary file, flushes to disk, then atomically
    replaces the destination via :func:`os.replace`.
    """
    payload = json.dumps(asdict(state), indent=2, sort_keys=True)

    # Ensure the parent directory exists before creating the temp file
    # in the same directory (required for os.replace atomicity).
    path.parent.mkdir(parents=True, exist_ok=True)

    # NamedTemporaryFile so the OS cleans up on crash; but we keep it
    # in the same directory to guarantee the rename stays on one filesystem.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        _apply_mode(tmp_path)
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure path.
        # We intentionally swallow removal errors so the original
        # exception propagates to the caller.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _apply_mode(path: Path) -> None:
    """Restrict file permissions to owner-only on POSIX systems."""
    if sys.platform == "win32":
        return
    os.chmod(path, 0o600)
