"""CLI entry point for the health-bridge pull client.

Parses command-line arguments, loads configuration, and delegates to
command handlers.  Business logic lives in pull_commands and pull_watch;
this module only handles argument parsing, config loading, and exit-code mapping.

Subcommands:
  latest   [type]                         — latest values per type
  range    type --from --to --limit       — time-range query
  weeks                                   — list available archive weeks
  archive  week_id                        — download weekly Markdown archive
  watch    types... --interval --output   — long-running poll mode

Exit codes:
  0   success
  1   configuration or auth error
  2   network error (connection, timeout)
  3   resource not found (404)
  130 Ctrl+C interrupt (watch mode)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from clients.health_bridge.pull_config import PullConfig, load_pull_config
from clients.health_bridge.pull_transport import (
    PullTransport,
    AuthError,
    NotFoundError,
    TransientError,
    TransportError,
)
from clients.health_bridge.pull_commands import (
    cmd_latest,
    cmd_range,
    cmd_weeks,
    cmd_archive,
)
from clients.health_bridge.pull_output import output_json, output_text
from clients.health_bridge.pull_watch import watch_loop

logger = logging.getLogger("health_bridge.pull_cli")

_DEFAULT_BASE_URL = "https://oh-my-frontweb.duckdns.org"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="health_pull.py",
        description="Pull health data from a remote Health-Bridge server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Common arguments for all subcommands.
    def add_common_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--config", type=Path, default=None,
            help="Path to JSON config file",
        )
        sub.add_argument(
            "--base-url", type=str, default=None,
            help=f"Server base URL (default: {_DEFAULT_BASE_URL})",
        )
        sub.add_argument(
            "--token", type=str, default=None,
            help="Read token (overrides HEALTH_READ_TOKEN env var)",
        )
        sub.add_argument(
            "--timeout", type=int, default=None,
            help="Request timeout in seconds (default: 30)",
        )
        sub.add_argument(
            "--output", type=Path, default=None,
            help="Write output to file instead of stdout",
        )
        sub.add_argument(
            "--insecure", action="store_true",
            help="Allow HTTP (for local testing)",
        )

    # latest
    sub_latest = subparsers.add_parser("latest", help="Get latest values")
    add_common_args(sub_latest)
    sub_latest.add_argument(
        "type", nargs="?", default=None,
        help="Observation type (heart_rate, steps, steps_daily, sleep_stage)",
    )

    # range
    sub_range = subparsers.add_parser("range", help="Query observations in a time range")
    add_common_args(sub_range)
    sub_range.add_argument("type", help="Observation type")
    sub_range.add_argument("--from", dest="from_ts", default=None, help="Start timestamp (ISO 8601)")
    sub_range.add_argument("--to", dest="to_ts", default=None, help="End timestamp (ISO 8601)")
    sub_range.add_argument("--limit", type=int, default=100, help="Max results (default: 100)")
    sub_range.add_argument("--cursor", default=None, help="Pagination cursor")

    # weeks
    sub_weeks = subparsers.add_parser("weeks", help="List available archive weeks")
    add_common_args(sub_weeks)

    # archive
    sub_archive = subparsers.add_parser("archive", help="Download a weekly Markdown archive")
    add_common_args(sub_archive)
    sub_archive.add_argument("week_id", help="ISO week identifier (e.g. 2026-W28)")

    # watch
    sub_watch = subparsers.add_parser("watch", help="Long-running poll mode")
    add_common_args(sub_watch)
    sub_watch.add_argument("types", nargs="+", help="Observation types to watch")
    sub_watch.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    sub_watch.add_argument("--output-dir", type=Path, required=True, help="Directory for output files")

    return parser


def _build_env(args: argparse.Namespace) -> dict[str, str]:
    """Build environment dict from args + os.environ."""
    env: dict[str, str] = {}
    # Start with existing environment.
    for key in ("HEALTH_PULL_BASE_URL", "HEALTH_READ_TOKEN", "HEALTH_PULL_TIMEOUT", "HEALTH_PULL_TIMEZONE"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    # Override with command-line args.
    if getattr(args, "base_url", None):
        env["HEALTH_PULL_BASE_URL"] = args.base_url
    if getattr(args, "token", None):
        env["HEALTH_READ_TOKEN"] = args.token
    if getattr(args, "timeout", None):
        env["HEALTH_PULL_TIMEOUT"] = str(args.timeout)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env = _build_env(args)
    allow_insecure = getattr(args, "insecure", False)

    try:
        config = load_pull_config(
            getattr(args, "config", None),
            env,
            allow_insecure=allow_insecure,
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    transport = PullTransport(config)
    output = getattr(args, "output", None)

    try:
        if args.command == "latest":
            result = cmd_latest(transport, config, args.type)
            output_json(result, output)
            return 0

        elif args.command == "range":
            result = cmd_range(
                transport, config, args.type,
                from_ts=args.from_ts, to_ts=args.to_ts,
                limit=args.limit, cursor=args.cursor,
            )
            output_json({
                "observations": result.observations,
                "next_cursor": result.next_cursor,
                "has_more": result.has_more,
            }, output)
            return 0

        elif args.command == "weeks":
            result = cmd_weeks(transport, config)
            output_json({"weeks": result}, output)
            return 0

        elif args.command == "archive":
            result = cmd_archive(transport, config, args.week_id)
            output_text(result, output)
            return 0

        elif args.command == "watch":
            return watch_loop(
                transport, config,
                types=args.types,
                interval=args.interval,
                output_dir=args.output_dir,
            )

    except AuthError as exc:
        print(f"Authentication error: {exc}", file=sys.stderr)
        return 1
    except NotFoundError as exc:
        print(f"Not found: {exc}", file=sys.stderr)
        return 3
    except TransientError as exc:
        print(f"Network error: {exc}", file=sys.stderr)
        return 2
    except TransportError as exc:
        print(f"Transport error: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
