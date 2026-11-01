#!/usr/bin/env bash
# Production serve wrapper for Softnix PrivateClaw (host-native).
# Sources .env and execs the venv's uvicorn. Used by the systemd unit
# (Linux) and the launchd agent (macOS) so both share one launch path.
set -euo pipefail

# Resolve the project root from this script's location (scripts/ -> repo root).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# The sandbox network default is none; an older .env that says bridge
# explicitly is moved to none once (timestamped backup, one-line notice; a
# line ending in '# claw:keep-network' opts out). Done here as well as in
# `claw update` because the update that brings this change still runs the old
# scripts/claw. Never blocks startup.
if [[ -f "$PROJECT_DIR/.env" && -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  "$PROJECT_DIR/.venv/bin/python" -m claw.sandbox.network_migration --quiet "$PROJECT_DIR/.env" || true
fi

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
