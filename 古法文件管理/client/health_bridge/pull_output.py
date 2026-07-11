"""Output formatting for the health-bridge pull client.

Handles writing results to stdout or files atomically.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def print_json(data: Any, indent: int = 2) -> None:
    """Pretty-print JSON to stdout."""
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    print(text)


def print_text(text: str) -> None:
    """Print plain text to stdout."""
    print(text)


def write_file(path: Path, content: str) -> None:
    """Atomically write content to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.rename(path)


def output_json(data: Any, output: Path | None = None) -> None:
    """Output JSON to stdout or file."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if output is not None:
        write_file(output, text)
    else:
        print(text)


def output_text(text: str, output: Path | None = None) -> None:
    """Output text to stdout or file."""
    if output is not None:
        write_file(output, text)
    else:
        print(text)
