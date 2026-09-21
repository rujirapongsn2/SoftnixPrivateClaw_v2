# Code Review: shared durable jobs — production hardening

## Summary

The hardened engine passes SQLite and PostgreSQL contract tests, full regression,
and isolated Chromium component checks. This is **not approved for full production
rollout**: several capabilities in the accepted plan remain unimplemented. Changes
are dev-only; the deployment profile is opt-in and defaults to disabled.

## High

- **`claw/core/runtime.py:process_turn` / `claw/jobs/runtime_bridge.py:admit` — general admission incomplete.**
  Normal chat routes artifact and explicit multi-source research requests into the common worker; general
  code/connector work still uses legacy paths. Artifact/research promotion now preserves
  prior receipts/checkpoints, but admission depends on a supported multi-step plan.
  Implement explicit task contracts and checkpoint-preserving promotion before
  claiming the shared system supports every requested workload.
- **`sbot/core/missions.py:submit_work` / `claw/jobs/provider.py` — automatic pre-admission recovery remains incomplete.**
  Lead call receipts now join the root ledger atomically at shared mission admission,
  with no duplicate turn billing. Reservations and usage now commit to an encrypted
  foreground journal before admission, with revision fencing and atomic adoption.
  Normal artifact/research orphans now resume automatically when safe; unfinished
  Bot DAG reconstruction and terminal foreground delivery reconciliation remain outstanding; uncertain side effects must not be replayed.
- **`claw/jobs/runtime_bridge.py:normal_execute` / `claw/jobs/bot_bridge.py:execute` — uncertain effects require manual investigation.**
  MCP/REST transport failures now pause before a model can resend; missing MCP
  sessions can reconnect without LLM calls. Remote receipt reconciliation and idempotency-aware
  retry are not connected across connector adapters. Do not enable automatic
  replay of unknown external actions. Implement provider-specific receipt contracts
  and lost-response tests before claiming autonomous recovery for those writes.
- **`claw/jobs/tool_results.py:observe` — strategy adaptation is bounded stopping, not complete replanning.**
  Failure fingerprints prevent loops, but a validated alternate-strategy transition
  and automatic progress-based initial allocation are still missing.

## Medium

- **`claw/jobs/validators.py` / `runtime_bridge.py:file_evidence` — limited validation scope.**
  File integrity, table structure and document coverage are checked, but semantic
  acceptance for arbitrary research/code/data tasks and selective invalidation of
  source/skill versions are incomplete. Keep the displayed validation scope honest.
- **Migration/import and operational acceptance.** Idempotent import of paused
  legacy jobs, data-bearing rollback, integrated specialist-room browser acceptance,
  and before/after workload metrics still need implementation/evidence. Empty-schema
  rollback and component API mocks cannot establish these properties.
- **`docker-compose.prod.yml:job-worker` — deployment prepared, not exercised.**
  Verify production topology, mounted paths/keys, process monitoring and bounded
  shutdown on the actual staging topology before enabling either admission flag.

## What's good

- Parallel worker suspension now fences sibling attempts atomically and preserves
  checkpoints; unknown external effects cannot be blindly replayed.
- Normal/Bot execution share reservations, leases and transactional outbox, with
  tests on both database engines.
- Active job controls remain visible on mobile, and stale action responses cannot
  overwrite the current session's cards.

## Evidence

- Latest full regression snapshot: **1,456 passed, 4 skipped, 3 warnings**.
- After the live-discovered classifier correction: **55 focused tests passed**.
- Real-provider research smoke: completed in **42.3 seconds**, two cited fetched
  sources and exactly one final delivery; no artifact requirement.
- Final PostgreSQL durability suite: **35 passed** (19.08 seconds).
- Chromium component E2E desktop/mobile: **2 passed** (48.07 seconds), mocked API.
- Actual specialist admission/legacy maintenance race test included in regression.
- PostgreSQL 16 empty DB: full upgrade, job migration downgrade/re-upgrade passed.
- Frontend build, targeted Ruff and diff whitespace checks passed.
- No production deployment or production data access performed.

Latest incremental checks: foreground tool/source reuse and atomic usage adoption,
unknown-usage reservation, ownership rollback, idempotent admission, and actual
Lead-to-specialist shared accounting are covered by deterministic integration tests.
Real-provider research smoke: 48.2 seconds, one delivery. No new migration in this pass.

Final verification for this increment:
- Regression snapshot: 1,461 passed, 2 skipped; four diagram-render checks could
  not launch Chromium inside the restricted sandbox. Rerunning the complete
  diagram-render file with local browser permission passed all 5 tests.
- After the final quota-classification change: 59 targeted durability, promotion
  and Bot integration tests passed. Selected Ruff and whitespace checks passed.
- PostgreSQL promotion contract suite: 37 passed; the temporary container was removed.
- Isolated dev API/worker restarted with the latest code at localhost:8701.
- Remaining root-accounting limitation: calls before job admission are captured
  in memory, not yet crash-durable reservations. This is not a production go decision.


## Pre-admission journal pass — 2026-09-20

This section supersedes earlier notes that foreground receipts are memory-only.
Root cause: an API crash before job admission lost the in-memory reservation tape,
so later recovery could undercount calls that had already reached the provider.

- Added `agent_foreground_journals` through additive revision `f7a92d03b614`.
  Both normal and Bot foreground turns commit reservations before provider calls,
  usage before consuming returned results, and source/tool checkpoints around tools.
- Payloads are encrypted. Revision checks fence stale writes; adoption marks the
  journal and creates the shared root/ledger in one transaction. Competing recovery
  claims cannot create two roots. Missing usage remains reserved, not zero.
- Successfully exited foreground turns close their journal. Process crashes leave
  retained evidence for inspection. There is no automatic replay of an unconfirmed
  foreground tool; adoption rejects that case in normal mode.
- Source/checkpoint payloads expire after 7 days; accounting metadata remains for
  the 30-day audit window. No output files are deleted by journal retention.
- Changed files: `claw/jobs/{models,foreground,provider,store,runtime_bridge}.py`,
  `claw/core/runtime.py`, `sbot/core/{runtime,loop}.py`, the new migration, and
  SQLite/PostgreSQL journal tests.

Validation: regression snapshot 1,464 passed / 2 skipped (browser suites excluded);
40 PostgreSQL durability/journal tests passed; 5 targeted journal tests passed,
including an actual subprocess exiting immediately after reservation and competing
admission claims. Ruff and whitespace checks passed. Tests use synthetic data.
Full PostgreSQL upgrade plus empty downgrade/re-upgrade passed. A populated
journal downgrade was rejected and its reservation remained intact.

Rollout requires applying the new additive migration before enabling these runtimes.
Rollback disables admission and stops workers, retaining journal/job tables; the
migration deliberately refuses to drop a nonempty journal. Empty-schema downgrade
is supported. Existing production data/topology has not been rehearsed.

Remaining limit: persistence and safe inspection/adoption are implemented; automatic
foreground orphan detection, validated replay planning and user-facing recovery
controls are not yet wired. This increment does not establish full production readiness.
No production service or data was changed.


## Automatic orphan detection — 2026-09-20

- The independent worker watcher now scans open foreground journals after 60
  seconds without heartbeat. Foreground execution pulses every 15 seconds during
  slow provider/tool calls. Admission rechecks freshness/revision inside the same
  queue transaction, so a heartbeat after the scan prevents takeover.
- Normal-chat journals with a completed multi-step artifact/research plan and no
  unconfirmed tool can enter the existing executor automatically. Persisted
  messages, tool receipts, sources, resource calls and elapsed active time carry
  forward. Outbox delivery and existing ownership/validator checks still apply.
- Unconfirmed tools and unsupported contracts become paused shared jobs, visible
  through the existing UI/API and cancellable. Bot foreground orphans retain their
  actor room and accounting but pause; automatic reconstruction of an unfinished
  Bot delegation/DAG is not implemented. No tool is blindly repeated.
- A terminal foreground marker prevents replay and double billing in the legacy
  delivery window. Such journals retain a delivery-verification reason for audit;
  automatic reconciliation of that narrow pre-admission delivery window remains
  outstanding and is not represented as a completed job.
- Revoked users cannot start recovery execution. Deleted/transferred sessions are
  retired from scanning while retaining their journal evidence.
- Added localized compact recovery reasons; no quota controls or approval flows.

Files: `claw/jobs/{foreground,provider,recovery,store,worker,application}.py`, both
chat runtimes, `web/src/DurableJobs.tsx`, and SQLite/PostgreSQL recovery tests.
No new schema migration beyond `f7a92d03b614` is required in this increment.
Rollback: disable admission flags and stop the shared worker; retain journals and
job tables. New shared jobs must not be handed to legacy executors.

Verification: regression snapshot 1,472 passed / 2 skipped (browser suites excluded),
48 PostgreSQL tests passed, latest targeted normal/Bot recovery tests 15 passed.
Frontend build, selected Ruff and whitespace checks passed. Build still reports its
existing large-chunk warning. Fault tests cover competing watchers, heartbeat during
admission, slow live calls, revoked ownership, unsafe effects, cancellation, actual
worker watcher invocation and one final delivery.

Production remains NO-GO for the full plan: dynamic Bot DAG recovery, terminal
foreground delivery reconciliation, general code/connector validators, strategy
replanning, and full deployment/rollback acceptance remain outstanding.

Isolated dev end-to-end smoke: a synthetic abandoned checkpoint was detected by
the independent worker and continued with the configured real provider. Completed
in 14.1 seconds, exactly one delivery, CSV contents verified, no Continue message.
This was fault injection, not a production crash or a full TOR workload.

### Follow-up: prepared Bot mission handoff

Closed the fully prepared DAG / queue-admission gap using a durable graph marker,
transactional graph/pristine-state checks, retained call reservations and the
original root mission ID. Two watchers cannot create a second root. Recovery of a
changed graph or an already-attempted node remains paused. Session language is
preserved rather than inferred from the English mission goal.

Files: `claw/jobs/bot_bridge.py`, `claw/jobs/recovery.py`, `claw/jobs/store.py`,
`tests/sbot_mode/test_shared_job_adapter.py`. Additive journal payload only; no new
migration. Older journals without the marker keep the existing inspection path.
Rollback retains adopted jobs in shared storage; do not pass them to legacy workers.
Production remains NO-GO for the complete plan, particularly terminal delivery
reconciliation and the remaining acceptance/deployment checks listed above.

Verification for this increment: regression 1,477 passed / 2 skipped / 4 warnings
(108.69 seconds; PostgreSQL and browser/diagram suites excluded). Final targeted
recovery/accounting suite: 60 passed, including execution of the recovered DAG and
exactly one delivery. Selected Ruff and `git diff --check` passed. Warnings include
SQLite test connection cleanup and the existing MCP client deprecation. PostgreSQL
and real-provider smoke results from earlier sections were not rerun for this change.

### Follow-up: atomic foreground final transcript (2026-09-21)

The known final-delivery/usage crash window is now covered for newly written
version-1 terminal payloads in both modes. Durable transcript, call-level known
usage, daily rollups and delivery receipt commit together. Tests cover two concurrent
deliveries, retry after commit, injected failure before commit, unknown reservations,
owner/user revocation, watcher recovery without provider calls, and live short-turn
accounting in both modes. Selected Ruff and whitespace checks pass.

Files: `claw/jobs/{foreground,provider,recovery}.py`, `claw/core/runtime.py`,
`sbot/core/runtime.py`, foreground journal/recovery and Bot adapter tests. No schema
migration. No restart or production deployment. Pending new terminal payloads require
a compatible worker during rollback; keep old workers from sweeping these journals.
Full production approval still requires the remaining acceptance work. Immediate
recovery WebSocket notification and reconciliation of unknown provider usage remain
open; old boolean-only terminal markers cannot reconstruct missing transcript text.

Verification snapshot: 1,482 passed / 2 skipped / 5 warnings in 109.93 seconds
(excludes PostgreSQL, browser and diagram suites). After the final access-check
adjustment, targeted journal/recovery/Bot coverage is rerun separately. The warning
categories are SQLite test connection cleanup and MCP API deprecation. This round
uses scripted providers and injected faults, not a production crash/live-provider
acceptance run. PostgreSQL test exports added but not executed this round.
Final targeted rerun: 30 passed (7.63 seconds); no failures.
