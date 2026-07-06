#!/usr/bin/env bash
# Production serve wrapper for Softnix PrivateClaw (host-native).
# Sources .env and execs the venv's uvicorn. Used by the systemd unit
# (Linux) and the launchd agent (macOS) so both share one launch path.
set -euo pipefail

# Resolve the project root from this script's location (scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# Load .env (KEY=VALUE lines). `set -a` exports everything sourced.
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJECT_DIR/.env"
  set +a
fi

HOST="${CLAW_HOST:-0.0.0.0}"
PORT="${CLAW_PORT:-8700}"

exec "$PROJECT_DIR/.venv/bin/uvicorn" \
  claw.main:create_app --factory \
  --host "$HOST" --port "$PORT" \
  --proxy-headers --forwarded-allow-ips '*'
