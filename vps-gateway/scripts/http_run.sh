#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

PORT=5000

usage() {
  echo "Usage: $0 -p <port>"
}

while getopts "p:h" opt; do
  case "$opt" in
    p)
      PORT="$OPTARG"
      ;;
    h)
      usage
      exit 0
      ;;
    \?)
      echo "Invalid option: -$OPTARG"
      usage
      exit 1
      ;;
  esac
done

export GATEWAY_HOST="${GATEWAY_HOST:-0.0.0.0}"
export GATEWAY_PORT="$PORT"

exec python -m uvicorn app.main:app --host "$GATEWAY_HOST" --port "$PORT"
