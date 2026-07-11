"""CLI entry point for the health-bridge push client.

Parses command-line arguments, loads configuration, and delegates to
the service layer.  Business logic lives in push_service; this module
only handles argument parsing, config loading, and exit-code mapping.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from clients.health_bridge.push_config import PushConfig, load_push_config
from clients.health_bridge.push_service import PushOutcome, run_once, run_watch

logger = logging.getLogger("health_bridge.cli")

_DEFAULT_SOURCE = "/storage/emulated/0/Download/health/Gadgetbridge.db"
_DEFAULT_BASE_URL = "https://oh-my-frontweb.duckdns.org"
_DEFAULT_URL = f"{_DEFAULT_BASE_URL}/health/api/v1/upload"

_EXIT_CODES: dict[PushOutcome, int] = {
    PushOutcome.UPLOADED: 0,
    PushOutcome.DUPLICATE: 0,
    PushOutcome.UNSUPPORTED_SCHEMA: 0,
    PushOutcome.DRY_RUN: 0,
    PushOutcome.PERMANENT_FAILURE: 2,
    PushOutcome.TRANSIENT_EXHAUSTED: 3,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health_push.py",
        description="Push Gadgetbridge database snapshots to a remote server.",
        epilog=(
            "Built-in defaults (overridable via config file or --source):\n"
            f"  source:      {_DEFAULT_SOURCE}\n"
            f"  upload_url:  {_DEFAULT_URL}\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for cmd in ("once", "watch", "dry-run"):
        sub = subparsers.add_parser(cmd, help=f"Run in {cmd} mode")
        sub.add_argument(
            "--config", type=Path, default=None,
            help="Path to JSON config file",
        )
        sub.add_argument(
            "--source", type=Path, default=None,
            help="Override source database path",
        )

    return parser


def main(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dry_run = args.command == "dry-run"
    env = environ if environ is not None else os.environ

    try:
        config = load_push_config(args.config, env, dry_run=dry_run)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.source is not None:
        config = replace(config, source_path=args.source)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.command == "watch":
        return run_watch(config)

    try:
        outcome = run_once(config, dry_run=dry_run)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return _EXIT_CODES.get(outcome, 2)


if __name__ == "__main__":
    sys.exit(main())
