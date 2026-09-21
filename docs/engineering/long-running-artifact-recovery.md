# Long-running PrivateClaw artifact work — dev remediation

## Confirmed failure chain

- Docker daemon was reachable; `claw-sandbox:latest` was absent on the dev host.
- `SandboxResult.render()` did not label nonzero exit codes as errors. The loop's string-based success check therefore cached failed executions, showed green tool badges and counted nonexistent writes as progress.
- The loop breaker compared exact consecutive calls. Alternating `echo`, `print`, and generator commands bypassed it although all failed against the same dependency.
- `read_docx` omitted tables and truncated paragraphs, with no continuation cursor. The agent tried shell extraction for missing source text, compounding the dependency failure.
- Normal-chat artifact budgets were separate from the control-plane team policy.
- A short continuation request did not match the artifact classifier; it fell back to an ordinary bounded turn. Terminal cleanup also discarded recovery data at budget exhaustion.

## Implemented in this dev patch

1. Nonzero shell outcomes are errors. Docker startup exit 125 / process launch failures carry a sandbox-unavailable marker. Docker uses `--pull=never`: provisioning is an operator action, not an accidental registry pull inside a user's tool call.
2. The loop stops on dependency failure, completes tool response pairs and checkpoints before handing control to the runtime. The runtime does bounded exponential-backoff health probes without spending LLM calls or replaying the user's shell side effect. A recovered capability resumes the same checkpoint; a persistent failure becomes `blocked` with a localized explanation.
3. Blocked jobs and limit checkpoints are retained. Explicit continuation resolves the latest retained job within the same user/session; cumulative budget exhaustion cannot be bypassed by asking to continue. Shutdown cancellation retains recovery data; explicit cancel clears it. Startup considers blocked/waiting jobs for recovery and validates ownership.
4. Newly created jobs snapshot saved control-plane token/time limits, automatic extension count and dependency recovery attempts. Extensions require a newly produced file; they remain bounded by cumulative time/token/USD limits. Existing jobs keep their original policy snapshot. Estimate multiplier/model output overrides are still specialist-specific, not a general planner for normal chat.
5. Word extraction includes paragraphs and tables in document order, off the event loop, with bounded cursor pages. A per-reader cache avoids parsing the same unchanged source on every page. Existing source reads that were already truncated cannot be retroactively repaired: source coverage must be rechecked.
6. Prompt guidance requires complete source coverage, normalized intermediate data, a deterministic generator and reopen validation. This is guidance, not a generic programmatic completeness verifier.
7. Built the missing dev sandbox image and smoke-tested real DOCX/JSON/XLSX generation and reopen validation with synthetic data in Docker. No production deployment.

## Remaining structural work for a durable general-purpose executor

The current implementation remains an in-process segmented executor; it is not a distributed durable workflow engine. Follow-up architecture should introduce:

- A dedicated jobs/steps/attempts schema, database leases, fencing tokens and a worker independent of WebSocket lifetimes. Recovery must be safe with multiple workers and interrupted database writes.
- A typed tool result contract (`ok`, `error_code`, `retryable`, `dependency`, `output_refs`) instead of rendered-string classification. Migration must cover all tool adapters, not only shell.
- A persisted phase manifest: source hash + extraction cursor/coverage; validated normalized data; generator hash; output checksum; reopen validation; exactly-once delivery key. Successful arbitrary tool calls are not equivalent to completed phases.
- A strategy controller that categorizes infrastructure, input, generation and validation failures. Retry infrastructure only after readiness changes; revise code for generation errors; request missing source data when necessary; stop on policy failures. Do not weaken isolation, access controls or budget ceilings as self-repair.
- A periodic dependency watcher for blocked jobs. Current automated probes are bounded; after they end, operator recovery plus continuation or service startup is needed. Restarting the service is not the desired long-term recovery interface.
- Dollar accounting per provider call/model, including partial calls and fallback rates; reservations before calls; persisted total time across crash/restart; enforce policy reductions on active work explicitly.
- Format-specific validators and acceptance gates. For BOM: all TOR sections/tables covered, each row has a source reference, quantities traceable, category totals consistent, expected sheets present. A generated file alone is not proof of a complete task.

## Operational boundaries and rollback

No schema migration added by this patch. Records remain in `app_settings`. Roll back code after draining jobs; old code will not understand blocked/waiting states. Retained checkpoints can contain source data and need a defined retention period and eventual dedicated encrypted storage. Existing unrelated schedule migrations are outside this patch.

The image is a dev infrastructure repair; no automatic Docker build or host-execution fallback was added to the agent. Testing a synthetic workbook proves the execution path works, not that the user's entire TOR BOM has passed source-coverage review.
