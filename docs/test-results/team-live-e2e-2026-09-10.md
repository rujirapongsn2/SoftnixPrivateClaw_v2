# Live team E2E — 2026-09-10

Scope: local application runtime → configured live model via OpenRouter → real
Docker sandbox → SQLite job state and transcript delivery. Synthetic test user,
fresh database and workspace; no production account or database mutations.
This is backend E2E, not browser/UI or production deployment validation.

Model: `openrouter/anthropic/claude-sonnet-5` from the existing CLAW environment.
Docker: 29.7.2, `claw-sandbox:latest`, sandbox networking disabled.

## Result

All nine checks passed on the second run:

- Docker Python execution, file creation and host readback.
- Live model connection.
- Chat submission returned a persisted job receipt.
- A second unrelated job was accepted while the first remained in progress.
- Both background jobs completed.
- Both used `result.txt`, retaining separate `ALPHA_42` and `BETA_99` contents.
- Exactly two final job reports were stored in the originating conversation.
- Dependency chain generated 42, passed its file snapshot to the next worker,
  generated 84, and completed the coordinator summary (which also states 84).
- A queued job was cancelled without another model call.

Successful run: 94.92 seconds; 19 model calls; provider-reported usage 87,037
input and 4,921 output tokens. This is token usage, not a calculated invoice.
Including the first diagnostic run: 30 calls, 150,337 input / 6,574 output tokens.

## Defect found and fixed

The first run created both files but failed while rebasing artifact paths:
macOS `/var/...` and `/private/var/...` referred to the same workspace but failed
a lexical `relative_to` comparison. `rebase_result` now resolves both roots
before containment checks. A regression test also confirms that escaping the
assignment directory is still rejected. The targeted team/verification suite
passed 34 tests after the fix.

## Evidence and remaining coverage

[Raw successful-run report](team-live-e2e-2026-09-10.json) contains check results,
job IDs, node outputs, usage and timestamps. Reusable opt-in runner:
`scripts/team-live-e2e.py` (maximum 24 calls per invocation; fresh test DB).

Not covered in this live run: browser clicks/WebSocket rendering, production
provider credentials, service restart/crash recovery, concurrent tenants or
multiple application workers, intentional provider failures, or load testing.
The preexisting small-model prompt-window test failure is not resolved here.
The runtime status response correctly listed both jobs as running, but added
an unverified claim that Alpha was still in its sleep phase; granular progress
wording remains model-dependent.
