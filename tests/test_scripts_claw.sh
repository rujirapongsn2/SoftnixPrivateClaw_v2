#!/usr/bin/env bash
# Regression tests for scripts/claw's port-ownership verification logic — the
# fix for: a restart/update reporting success because /api/ready answered OK,
# when that answer actually came from a STALE process left over from before
# the restart (see verify_restart in scripts/claw). Dependency-free (plain
# bash + coreutils), no test framework required.
#
# Run: bash tests/test_scripts_claw.sh
#
# shellcheck disable=SC2329  # these mocks ARE invoked, indirectly, by
# verify_restart (defined in the sourced scripts/claw) — shellcheck can't see
# that call graph across the dynamic `source`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAW="$SCRIPT_DIR/../scripts/claw"

pass_count=0
fail_count=0

ok() { pass_count=$((pass_count + 1)); echo "  ok   - $1"; }
fail() { fail_count=$((fail_count + 1)); echo "  FAIL - $1"; }

# --- 0. static checks -------------------------------------------------------

if bash -n "$CLAW" 2>/tmp/claw_syntax_err; then
  ok "bash -n scripts/claw (syntax)"
else
  fail "bash -n scripts/claw: $(cat /tmp/claw_syntax_err)"
fi
rm -f /tmp/claw_syntax_err

if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck "$CLAW" >/tmp/claw_shellcheck_out 2>&1; then
    ok "shellcheck scripts/claw"
  else
    fail "shellcheck scripts/claw:\n$(cat /tmp/claw_shellcheck_out)"
  fi
  rm -f /tmp/claw_shellcheck_out
else
  echo "  skip - shellcheck not installed"
fi

# --- load just the function definitions (everything before the arg-parsing /
# dispatch block at the bottom), so sourcing this file doesn't try to run a
# subcommand or read real supervisor state.
FUNCS_LINE="$(grep -n '^ARGS=()' "$CLAW" | head -1 | cut -d: -f1)"
FUNCS_FILE="$(mktemp)"
sed -n "1,$((FUNCS_LINE - 1))p" "$CLAW" > "$FUNCS_FILE"
# shellcheck source=/dev/null
source "$FUNCS_FILE"
rm -f "$FUNCS_FILE"

# --- 1. port_pid / pid_cmd against a real, known process --------------------

DUMMY_PORT=18765
python3 -c "
import http.server, socketserver
with socketserver.TCPServer(('127.0.0.1', $DUMMY_PORT), http.server.SimpleHTTPRequestHandler) as httpd:
    httpd.serve_forever()
" &
DUMMY_PID=$!
# give it a moment to bind
for _ in 1 2 3 4 5; do
  { exec 3<>"/dev/tcp/127.0.0.1/$DUMMY_PORT"; } 2>/dev/null && { exec 3>&-; break; }
  sleep 0.3
done

found_pid="$(port_pid "$DUMMY_PORT")"
if [[ "$found_pid" == "$DUMMY_PID" ]]; then
  ok "port_pid finds the real listener ($DUMMY_PID)"
else
  fail "port_pid($DUMMY_PORT) = '$found_pid', expected '$DUMMY_PID'"
fi

cmd_out="$(pid_cmd "$DUMMY_PID")"
if [[ "$cmd_out" == *python3* || "$cmd_out" == *Python* ]]; then
  ok "pid_cmd returns a sensible command line"
else
  fail "pid_cmd($DUMMY_PID) = '$cmd_out' (expected it to mention python)"
fi

empty_pid="$(port_pid 1)"  # privileged port almost certainly not bound by us
if [[ -z "$empty_pid" ]]; then
  ok "port_pid returns empty for an unbound port"
else
  echo "  skip - port 1 unexpectedly has a listener ($empty_pid); not a real failure"
fi

kill "$DUMMY_PID" 2>/dev/null || true
wait "$DUMMY_PID" 2>/dev/null || true

# --- 2. verify_restart: the exact bug this fixes ----------------------------
# Simulates: /api/ready answers OK (curl succeeds), but the process actually
# holding the port is NOT the one the restart believes it just started.

curl() {  # shadow curl so verify_restart's readiness poll always "succeeds"
  return 0
}
port() { echo "$MOCK_PORT"; }
port_pid() { echo "$MOCK_OWNER_PID"; }
pid_cmd() { echo "mock-stale-process"; }

MOCK_PORT=9999

# 2a. nohup mode: PIDFILE says one PID, port_pid reports a different one.
PIDFILE="$(mktemp)"
echo 111111 > "$PIDFILE"
MOCK_OWNER_PID=222222
mode() { echo nohup; }
if verify_restart >/tmp/verify_out 2>&1; then
  fail "verify_restart (nohup, PID mismatch) incorrectly returned success"
else
  if grep -q "not the PID this script just started" /tmp/verify_out && grep -q "222222" /tmp/verify_out; then
    ok "verify_restart (nohup) detects a stale process holding the port"
  else
    fail "verify_restart (nohup) failed for the wrong reason: $(cat /tmp/verify_out)"
  fi
fi
rm -f "$PIDFILE" /tmp/verify_out

# 2b. nohup mode: PIDFILE matches the port owner -> must succeed.
PIDFILE="$(mktemp)"
echo 333333 > "$PIDFILE"
MOCK_OWNER_PID=333333
if verify_restart >/tmp/verify_out 2>&1; then
  ok "verify_restart (nohup) succeeds when PID matches the port owner"
else
  fail "verify_restart (nohup, matching PID) incorrectly failed: $(cat /tmp/verify_out)"
fi
rm -f "$PIDFILE" /tmp/verify_out

# 2c. systemd mode: unit reports active, but MainPID != port owner (the
# exact production bug this was written to catch).
mode() { echo systemd; }
systemd_is_active() { return 0; }
systemd_main_pid() { echo 444444; }
MOCK_OWNER_PID=555555
if verify_restart >/tmp/verify_out 2>&1; then
  fail "verify_restart (systemd, MainPID mismatch) incorrectly returned success"
else
  if grep -q "not systemd's MainPID" /tmp/verify_out && grep -q "555555" /tmp/verify_out; then
    ok "verify_restart (systemd) detects MainPID/port-owner mismatch"
  else
    fail "verify_restart (systemd) failed for the wrong reason: $(cat /tmp/verify_out)"
  fi
fi
rm -f /tmp/verify_out

# 2d. systemd mode: MainPID matches the port owner -> must succeed.
MOCK_OWNER_PID=444444
if verify_restart >/tmp/verify_out 2>&1; then
  ok "verify_restart (systemd) succeeds when MainPID matches the port owner"
else
  fail "verify_restart (systemd, matching PID) incorrectly failed: $(cat /tmp/verify_out)"
fi
rm -f /tmp/verify_out

# 2e. systemd mode: unit not active at all -> must fail immediately, without
# even needing a port-owner mismatch to catch the problem.
systemd_is_active() { return 1; }
systemd_main_pid() { echo 0; }
if verify_restart >/tmp/verify_out 2>&1; then
  fail "verify_restart (systemd, inactive unit) incorrectly returned success"
else
  if grep -q "is not active after restart" /tmp/verify_out; then
    ok "verify_restart (systemd) detects an inactive unit"
  else
    fail "verify_restart (systemd, inactive) failed for the wrong reason: $(cat /tmp/verify_out)"
  fi
fi
rm -f /tmp/verify_out

echo ""
echo "== $pass_count passed, $fail_count failed =="
[[ "$fail_count" -eq 0 ]]
