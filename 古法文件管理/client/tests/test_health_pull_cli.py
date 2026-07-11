"""Integration tests for the health_pull CLI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add client dir to path for imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from client.health_pull import main, build_parser


class TestCLIArgumentParsing:
    """CLI argument parsing."""

    def test_latest_with_type(self):
        parser = build_parser()
        args = parser.parse_args(["latest", "heart_rate"])
        assert args.command == "latest"
        assert args.type == "heart_rate"

    def test_latest_without_type(self):
        parser = build_parser()
        args = parser.parse_args(["latest"])
        assert args.command == "latest"
        assert args.type is None

    def test_range_with_all_params(self):
        parser = build_parser()
        args = parser.parse_args([
            "range", "heart_rate",
            "--from", "2026-07-01T00:00:00+08:00",
            "--to", "2026-07-08T00:00:00+08:00",
            "--limit", "50",
        ])
        assert args.command == "range"
        assert args.type == "heart_rate"
        assert args.from_ts == "2026-07-01T00:00:00+08:00"
        assert args.to_ts == "2026-07-08T00:00:00+08:00"
        assert args.limit == 50

    def test_weeks(self):
        parser = build_parser()
        args = parser.parse_args(["weeks"])
        assert args.command == "weeks"

    def test_archive(self):
        parser = build_parser()
        args = parser.parse_args(["archive", "2026-W28"])
        assert args.command == "archive"
        assert args.week_id == "2026-W28"

    def test_watch(self):
        parser = build_parser()
        args = parser.parse_args([
            "watch", "heart_rate", "steps",
            "--interval", "30",
            "--output-dir", "./latest",
        ])
        assert args.command == "watch"
        assert args.types == ["heart_rate", "steps"]
        assert args.interval == 30
        assert args.output_dir.name == "latest"

    def test_insecure_flag(self):
        parser = build_parser()
        args = parser.parse_args(["weeks", "--insecure"])
        assert args.insecure is True


class TestCLIConfigError:
    """Configuration error handling."""

    def test_missing_token_returns_1(self, capsys):
        # No token, no env var → config error.
        env_backup = dict(os.environ)
        try:
            os.environ.pop("HEALTH_READ_TOKEN", None)
            os.environ.pop("HEALTH_PULL_BASE_URL", None)
            exit_code = main(["weeks", "--insecure", "--base-url", "http://localhost:8765"])
            assert exit_code == 1
            captured = capsys.readouterr()
            assert "Configuration error" in captured.err
        finally:
            os.environ.clear()
            os.environ.update(env_backup)


class TestCLIIntegration:
    """Full CLI integration with mocked transport."""

    def test_latest_command(self, capsys):
        with patch("client.health_pull.PullTransport") as MockTransport:
            mock_instance = MockTransport.return_value
            mock_instance.get.return_value = json.dumps({
                "heart_rate": {"value": {"bpm": 84}, "timestamp_utc": "2026-07-11T06:35:00+00:00"},
            }).encode()

            exit_code = main([
                "latest", "heart_rate",
                "--insecure",
                "--base-url", "http://localhost:8765",
                "--token", "test-token",
            ])

            assert exit_code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["heart_rate"]["value"]["bpm"] == 84

    def test_weeks_command(self, capsys):
        with patch("client.health_pull.PullTransport") as MockTransport:
            mock_instance = MockTransport.return_value
            mock_instance.get.return_value = json.dumps({"weeks": ["2026-W28"]}).encode()

            exit_code = main([
                "weeks",
                "--insecure",
                "--base-url", "http://localhost:8765",
                "--token", "test-token",
            ])

            assert exit_code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["weeks"] == ["2026-W28"]

    def test_archive_command(self, capsys):
        with patch("client.health_pull.PullTransport") as MockTransport:
            mock_instance = MockTransport.return_value
            mock_instance.get.return_value = b"# Health Archive 2026-W28\n..."

            exit_code = main([
                "archive", "2026-W28",
                "--insecure",
                "--base-url", "http://localhost:8765",
                "--token", "test-token",
            ])

            assert exit_code == 0
            captured = capsys.readouterr()
            assert "2026-W28" in captured.out

    def test_range_command(self, capsys):
        with patch("client.health_pull.PullTransport") as MockTransport:
            mock_instance = MockTransport.return_value
            mock_instance.get.return_value = json.dumps({
                "observations": [
                    {"type": "heart_rate", "value": {"bpm": 79}},
                ],
                "next_cursor": None,
            }).encode()

            exit_code = main([
                "range", "heart_rate", "--limit", "10",
                "--insecure",
                "--base-url", "http://localhost:8765",
                "--token", "test-token",
            ])

            assert exit_code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert len(data["observations"]) == 1

    def test_output_to_file(self, tmp_path):
        with patch("client.health_pull.PullTransport") as MockTransport:
            mock_instance = MockTransport.return_value
            mock_instance.get.return_value = json.dumps({"weeks": ["2026-W28"]}).encode()

            out_file = tmp_path / "output.json"
            exit_code = main([
                "weeks",
                "--insecure",
                "--base-url", "http://localhost:8765",
                "--token", "test-token",
                "--output", str(out_file),
            ])

            assert exit_code == 0
            assert out_file.exists()
            data = json.loads(out_file.read_text())
            assert data["weeks"] == ["2026-W28"]
