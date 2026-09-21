# Shared durable jobs — dev implementation and acceptance status

Updated 2026-09-20. No production deployment. Feature flags remain off by default.
The isolated local test instance enables both flags with a separate SQLite DB,
workspace roots, and API/worker processes. It does not modify the installed service
on port 8700 or its data. **This is a dev pilot, not completion of every A–D criterion.**

## Production hardening pass — 2026-09-20

Release decision: **NO-GO for the complete A–D rollout**. The runtime is safer and
has broader verification, but the functional gaps below remain. No production
service, database or user data has been modified.

Changes in this pass:

- Suspension, resource exhaustion, dependency expiry and unsupported checkpoints
  now fence parallel attempts in the same transaction. Safe sibling checkpoints
  are retained; unknown external effects remain paused. This prevents stale
  workers settling results and abandoned execution slots.
- Worker consumers/watchers retry transient database connection failures with
  bounded backoff, reacquiring through the lease protocol rather than replaying
  tools. Shutdown interrupts backoff.
- Shared Bot admission leaves the legacy mission `planned` until common queue
  admission atomically changes it to `queued`. A regression interleaves legacy
  maintenance at the handoff boundary to prove it cannot execute the same work.
- PostgreSQL schema creation uses a portable Boolean default for usage accounting.
- UI lists all active jobs ahead of recent completed jobs; in-flight actions are
  disabled, stale responses cannot overwrite another session, and pause/recovery
  reasons have Thai and English labels.
- `docker-compose.prod.yml` includes an opt-in `durable-jobs` worker profile with
  no HTTP ports, the same workspace/configuration as API, and restart supervision.
  Flags remain false. The Compose configuration was validated with synthetic
  settings only; the profile has **not** been deployed.
- Three outdated connector catalog assertions now verify the current typed field
  schema and built-in GitHub server command; no connector behavior was changed.

Observed verification in this pass:

- Full regression: **1,443 passed, 4 skipped, 7 warnings** (112.17 seconds).
  Browser-render tests were rerun with permission to launch local Chromium.
- Final PostgreSQL contract suite: **35 passed** (19.08 seconds).
- Full Alembic upgrade from an empty PostgreSQL 16 database passed. Downgrade of
  the new job migration to `d4f1c8a92b57` and re-upgrade passed on the disposable
  database. This is not a data-bearing production rollback rehearsal.
- Actual Chromium component E2E: **2 passed** at widths 390 and 1280, checking
  progress, network failure/reconnect, cancel, single delivery refresh and no
  horizontal overflow. API responses were synthetic; this does not establish
  complete chat/specialist-room acceptance.
- TypeScript/Vite build, selected Ruff checks and `git diff --check` pass.

## Workload and connector pass — 2026-09-20

- Explicit multi-source research requests now enter the common normal-chat worker
  and can finish with a cited chat answer, without manufacturing a file. Successful
  web fetches are cached in encrypted checkpoints across worker claims. Citation
  validation checks retrieved provenance; it does not certify factual completeness.
- Research calls omit file-writing/shell definitions. Verified source retrieval can
  count toward slice progress. Candidate validation rechecks local source versions.
- Both MCP and REST adapters check current connector access before RPC. A missing
  MCP connection waits for a reconnect probe without model calls. A timeout, lost
  response or error after dispatch pauses with `uncertain_effect`; it cannot cause
  a model-driven duplicate write. Remote receipts/idempotency adapters are still
  needed to resolve uncertain outcomes automatically.
- Nested specialist admission cannot create a new root budget. Initial foreground
  Team Lead accounting and dynamic expansion of the existing root remain open.
- The local dev launcher supports `--daemon`, keeping the isolated supervisor
  independent of the invoking terminal. Production remains untouched.
- Regression snapshot: **1,456 passed, 4 skipped, 3 warnings**. Connector suite:
  **109 passed**; after the research classifier fix, targeted runtime/artifact
  tests: **55 passed**. All use fixtures except separately documented live smoke.
- The first live research test found a classifier bug: “Do not create files” plus
  source URLs ending in `.html` selected artifact delivery. Source URLs and explicit
  no-file clauses are now excluded from that classifier, with regression tests.
- Repeated live-provider research smoke after the fix: **completed in 42.3 seconds**,
  one outbox delivery, one user message and one assistant message. Both requested
  Python documentation URLs appear in the final answer; stored validation scope
  explicitly excludes factual completeness. No files, shell or connector writes.
  Report: `/private/tmp/privateclaw-durable-dev/live-research-smoke.json`.

## Root cause

The original request repeatedly invoked a missing sandbox image. Model/tool loops
consumed their execution-slice and cumulative allowances without useful progress.
Increasing allowances cannot repair that dependency. The normal runtime and Bot
Mode also had separate in-process recovery paths, so API restarts, continuation
classification, context retention and reporting had different behavior.

## Implemented

- `claw/jobs/{models,store,contracts,graph}.py`: additive encrypted jobs, steps,
  attempts, provider reservations, events and transactional delivery outbox. Global
  queue admission and fencing cover both modes. Lease/heartbeat default to 60/15s.
- `worker.py`, `application.py`: standalone application worker; does not start API,
  cron, Telegram or the legacy mission scheduler. Graceful stop fences subsequent
  writes through lease expiry; unfinished checkpoints remain available.
- `runtime_bridge.py`, `claw/{main,core/runtime,core/loop}.py`: flagged normal-chat
  artifact admission, atomic request/queue insert, checkpoint facade, one execution
  slice per claim, dependency release, final candidate revalidation without another
  model call, and outbox delivery. Short chat retains its previous path.
- `bot_bridge.py`, `sbot/{main,core/missions,core/specialist,core/loop}.py`: flagged
  `team_submit` DAG admission; legacy scheduler/reporter skip adopted jobs even if
  flags are later disabled. Specialist checkpoints retain tool results and
  `finish_step` records. Fenced settlement updates the legacy mission read model.
  Existing room observations remain separate from leader coordination.
- `provider.py`: reserves before provider invocations, including loop fallback and
  retries; known usage is reconciled once by call ID and included in normal usage
  records/rollups transactionally. Unknown usage retains its reservation. Internal
  LiteLLM retries are disabled in the shared-worker context.
- `approval.py`, `api.py`: durable action approval keyed to the exact command,
  separate from resource allocation. Ownership, stale decisions and duplicate
  decisions are checked. Cancellation blocks new scheduling and delivery.
- `tool_results.py`, both shell adapters: sandbox provisioning errors yield to
  dependency waiting without another model retry. Typed execution errors are
  bounded across command changes. Legacy tools explicitly remain unclassified.
- Shared DOCX extraction caches source text/version and verified character coverage
  in encrypted checkpoints; resumes do not reparse the same version. Source changes
  and incomplete attached DOCX coverage prevent final normal-chat delivery.
- `workbook.py`: trusted deterministic JSON → XLSX generator in a cancellable child
  process. Validates row widths, sheet names and size limits, writes atomically,
  reopens output, hashes input/output, and treats source strings as literal cells.
  It does not execute model-authored Python outside the sandbox. It currently
  supports literal cells, not a formula-authoring API.
- `web/src/DurableJobs.tsx`, both Chat components, API helpers/styles: shared compact
  status/cancel/approval UI and durable polling after reconnect; delivered messages
  reload from the transcript. Details are collapsed. Status follows job locale.

## Verification

All counts below are observed runs, not estimates. A full run was interrupted after
an intermediate development error; it is not included as a successful run.

- Full regression snapshot: **1430 passed, 2 skipped, 3 failed**. All three failures
  are the previously observed connector-preset expectations in
  `tests/test_connector_presets.py` (`env_fields` / old GitHub server command).
- Latest focused normal/kernel/resilience/shared-adapter run: **44 passed**.
- Bot mission/engine/shared-adapter regression: **69 passed**; after adding the
  real specialist runtime and unavailable Docker cases, shared-adapter tests: **3 passed**.
- Bot sandbox/project/verification regression: **38 passed, 1 skipped**. Both modes
  now share sandbox infrastructure classification and cancellation cleanup.
- Selected-file Ruff checks and `git diff --check` pass. Frontend TypeScript + Vite
  production build passes; Vite still reports its large-chunk warning.
- Kernel tests cover real transactional races, fences, killed worker recovery,
  provider slowdown, unknown usage, cancellation, dependency recovery/expiry,
  duplicate delivery, owner isolation and SQLite migration/downgrade preservation.
- Adapter tests exercise the actual normal runtime, approval across claims, the
  actual specialist/finish_step runtime, dependency recovery, candidate-only
  recovery, document cache reuse, and deterministic workbook generation.
- **Real provider smoke**, using synthetic data over HTTP + WebSocket to API and a
  separate worker process: completed in **10.1s**, one final delivery, one user and
  one assistant message. Reopened XLSX and verified the Data sheet contains exactly
  `(Name, Qty)`, `(Alpha, 2)`, `(Beta, 3)`. No shell, external connector write, or
  manual Continue was used. Report: `/private/tmp/privateclaw-durable-dev/live-smoke.json`.
- Browser UI automation is unavailable in this session (no enabled browser surface).
  Desktop/mobile visual and interactive acceptance has **not** been claimed.

## Running and rollback

For an isolated local pilot, from the repository:

```sh
.venv/bin/python scripts/durable-dev.py --directory /private/tmp/privateclaw-durable-dev --port 8701
```

The supervisor starts API and worker separately on localhost, creates only the
isolated DB, and retains it between restarts. Credentials are not written to logs.
The installed service at 8700 is untouched. Use a different directory for a new
empty pilot. The two flags are `CLAW_DURABLE_JOBS_PRIVATECLAW` and
`CLAW_DURABLE_JOBS_SBOT`; both default to false outside this supervisor.

For a separately managed dev database, apply the additive Alembic migration
`e6b8d1c03a72` (parent `d4f1c8a92b57`) before starting
`.venv/bin/python -m claw.jobs.worker`. The worker itself does not migrate the DB.

Rollback: disable admission flags, stop the worker, retain its tables/checkpoints
and output files, and route **new** work to the old runtimes. Do not give shared
job checkpoints to an older executor. Do not downgrade/drop these tables while
jobs or audit records must be retained. SQLite migration rehearsal has passed;
PostgreSQL empty-database migration/rollback passed in the hardening pass; a representative data-bearing rollout rehearsal remains unverified.

## Remaining acceptance gaps — do not promote to production yet

1. Normal-chat automatic admission currently covers artifact and explicit multi-source research requests. General
   code/connector admission remains incomplete. Foreground artifact/research promotion now
   adopts completed tool results, source caches and prior usage after a multi-step plan. The Bot adapter covers `team_submit`, not
   every legacy foreground delegation path.
2. `team_submit` now atomically adopts the foreground lead call receipts into the
   specialist root ledger and suppresses duplicate legacy billing. Calls before
   admission now persist in an encrypted journal. Automatic foreground recovery
   scheduling/UX and dynamic DAG expansion are still outstanding.
3. Deterministic validators establish file integrity/schema and DOCX read coverage,
   not semantic correctness or complete BOM mapping. Explicit Bot code/research
   validation contracts use the existing verifier; broad task-specific validator
   coverage, source/skill version invalidation and formula validation need more work.
4. Typed migration covers shell/generator outcomes; MCP and REST calls now conservatively
   pause on uncertain post-dispatch results and check current access. Remote receipt
   reconciliation and idempotent auto-retry are not implemented for every connector.
5. Error-fingerprint bounds prevent repeated failures. A fully autonomous strategy
   replanner, progress-based initial estimation/extension and monetary-price ledger
   are not complete; the worker currently enforces cumulative token/time limits.
6. Idempotent import of paused legacy jobs, complete permission-revocation tests,
   full PostgreSQL service-failure injection, integrated chat/specialist-room UI tests and
   controlled before/after metrics on the entire fixture set remain outstanding.
7. Live smoke covers a small synthetic workbook, not the user's full TOR, every
   provider, or live connector side effects. Production readiness is not established.


## Foreground promotion and lead accounting — 2026-09-20

- A successful multi-step plan can promote an initially short normal chat into the
  shared worker for the supported artifact/research contracts. The handoff persists
  messages, completed tool receipts, written paths, parsed DOCX/web source caches,
  elapsed processing time and model call receipts in one transaction. It does not
  append the user's message twice. Unsupported acceptance families keep their
  existing route instead of being declared complete by a generic text validator.
- Each foreground provider invocation records a distinct receipt, including retry
  and fallback invocations. Missing usage retains its reservation after adoption.
  Provider-internal hidden retries are disabled while capturing receipts.
- Shared Bot admission adopts initial lead usage into the same root job as its
  specialists. `team_submit` is terminal, so the foreground acknowledgement needs
  no extra model call. Legacy turn billing is skipped only after successful adoption.
  Unrelated asynchronous bookkeeping does not inherit the foreground receipt tape.
- Resource-limit resume rechecks unchanged cumulative limits. Reconciled unknown
  usage can free capacity; resume never resets spend and still rejects uncertain
  external effects. This does not automatically resolve unknown provider usage.
- No schema change is required beyond the existing additive shared-job migration.
  Fence-zero resource rows represent pre-admission calls, associated with the first
  job step for accounting. Ordinary submission fingerprints remain compatible.
  Rollback: disable admission, stop worker safely, retain shared checkpoints/ledger;
  never route adopted jobs back to a legacy executor.

Evidence: 37 PostgreSQL contract tests passed; focused promotion/connector/Bot suites
passed. Real-provider research on isolated dev completed in 48.2 seconds with one
outbox delivery. This smoke verifies normal research, not foreground promotion;
promotion and initial-lead accounting use deterministic provider fixtures.
The foreground tape is not crash durable before job admission. Production still
requires the remaining acceptance items above; no production service was changed.

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

### Prepared Bot admission recovery (2026-09-20)

The foreground journal now records an immutable hash of a fully persisted Bot
mission before common-queue submission. If the API process disappears in that
window, the shared watcher adopts the original mission ID and DAG with its original
call receipts and outstanding reservations. No `team_submit` replay or replacement
budget is required. Admission rechecks node instructions, dependencies, acceptance
metadata in node budgets, session ownership, and absence of earlier execution in
its queue transaction. Recovery checks current bot/group access; execution retains
its existing authorization checks. Recovered jobs retain the session locale.

The fault suite injects a crash before queue commit, races two recovery watchers,
and verifies one root job, one conserved reservation, and no legacy scheduler
claim. Changed instructions or earlier node attempts produce a paused inspection
job rather than execution. No new schema or production configuration changes.

Scope limit: crashes before the prepared journal marker still pause for inspection;
this does not reconstruct arbitrary incomplete mission graphs. Terminal foreground
transcript delivery reconciliation remains outstanding. This increment has not
been restarted into the isolated dev service or deployed to production.

Verification: 1,477 regression tests passed, 2 skipped; final focused recovery suite
60 passed. Four regression warnings (SQLite test cleanup and MCP deprecation).
Ruff and whitespace checks passed. Browser/PostgreSQL suites excluded this round;
no claim of new real-provider or deployed-dev verification for this increment.

### Atomic foreground terminal delivery (2026-09-21)

Normal chat and Bot Mode now journal the final transcript before delivery. The
message batch, known provider usage/rollups, turn metrics and a delivered receipt
commit in one database transaction. Competing live/recovery deliveries serialize
on the common queue lock and use deterministic message identities. A crash before
commit leaves the journal recoverable; a crash after commit cannot publish or bill
it again. Runtime legacy usage recording is skipped only after this path succeeds.
The close/finally path preserves a pending terminal payload after delivery failure.

The independent watcher delivers abandoned terminal payloads without calling the
model or rerunning tools. Current owner, active user, bot and group checks apply.
Unknown provider usage remains in journal receipts for reconciliation, rather than
being converted to zero. This does not yet implement provider-specific unknown
usage reconciliation. Older boolean-only terminal checkpoints remain inspect-only.

No new schema migration. Payload version 1 is additive. Rollback must drain or
retain pending terminal journals with a compatible worker; older workers cannot
recover these payloads safely and must not be allowed to sweep them. No production
deployment or dev service restart performed in this increment.

Limitations: recovery commits transcript history; immediate WebSocket notification
of recovered foreground delivery has not been added (history reload shows it).
This is delivery durability, not a new semantic validator of ordinary replies.

Verification: regression snapshot 1,482 passed / 2 skipped / 5 warnings; final
focused journal/recovery/Bot suite 30 passed. Ruff and whitespace checks passed.
PostgreSQL, browser and real-provider acceptance were not rerun this increment.
