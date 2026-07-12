#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

if command -v uv >/dev/null 2>&1 && [ -f "pyproject.toml" ]; then
  if [ -n "${PIP_TARGET:-}" ]; then
    echo "[setup] Deploy mode (uv): installing to PIP_TARGET=$PIP_TARGET"
    uv export --frozen --no-hashes --no-dev | uv pip install --no-cache --target "$PIP_TARGET" -r -
  else
    echo "[setup] Devbox mode (uv): installing to .venv"
    if [ -f "uv.lock" ]; then
      uv sync --frozen || uv sync
    else
      uv sync
    fi
    touch .venv/.uv_ready
  fi
elif [ -f "requirements.txt" ]; then
  echo "[setup] Fallback mode (pip): installing from requirements.txt"
  pip install -r requirements.txt
else
  echo "[setup] Warning: no pyproject.toml or requirements.txt found, skipping install"
fi
