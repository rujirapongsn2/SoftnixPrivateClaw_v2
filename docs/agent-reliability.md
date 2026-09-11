# Agent completion and verification

## Runtime contract

`delegate`, `delegate_many` and Mission execution now classify results through
`TaskResult`. Completion, verification and delivery are independent. An empty
answer is a failure even when a user-facing fallback message is displayed.
Only a successful runtime completion record can replace missing closing prose.
A timeout without that record remains partial/failed.

Existing schemas/events remain compatible. New callers should provide:

- `required_files`: every requested deliverable; omit for conversational tasks.
- `acceptance_criteria`: concrete criteria, not a request to claim success.
- `input_files`: owner-workspace paths explicitly supplied to isolated workers.
- `delivery_target`: `{workspace_id, paths?: {source_path: local_relative_path}}`.
- `verification`: `{kind: "code", test_command: ["python", "-m", "pytest", "-q"]}`
  or `{kind: "research", source_urls: ["https://..."]}`.
- `id` on batch assignments: unique within the batch. Generated IDs are stable
  within the call when omitted. The batch reports expected/completed counts and
  every incomplete ID before returning individual answers.

Mission node additions are persisted in the existing `budget` JSON, avoiding a
schema migration. The full result contract is persisted under `contract:<node>`
in the blackboard, atomically with the lease-checked completion. Agent writes to
`result:`, `contract:`, `scope:` and `delivery:` keys are rejected. The existing `result:<node>` remains the full prose output.
Children and status responses receive contract references and independent status
metadata so prose preview truncation cannot hide a failure or missing evidence.
Legacy nodes without a contract remain unverified, not retroactively verified.

`finish_step` snapshots all files, validates the entire set, then publishes those
same bytes. Content-addressed publication reuses references on repeated delivery.
Every check is tied to a SHA-256. Existing local content with the expected hash is
accepted as already delivered; conflicting content is never overwritten. An
uncertain write response is not confirmation. Local delivery still enforces the
owner pairing, tool argument policy and the Local Agent's existing size and
write restrictions.

`blocked` alone is an external failure eligible for the scheduler's existing
recovery. `finish_step(status="blocked", needs_input=true, ...)` explicitly parks
for user input. A skipped node with required files does not satisfy dependents
and cannot cause an all-terminal graph to report success.

## Rollout

Basic empty-result classification, immutable file checks and optional result
metadata are active. Expensive/new execution behavior is opt-in:

```dotenv
SBOT_RELIABILITY__VERIFICATION_MODE=off
SBOT_RELIABILITY__PILOT_OWNER_IDS=["internal-owner-id"]
SBOT_RELIABILITY__RETRY_EMPTY_OUTPUT=false
SBOT_RELIABILITY__ISOLATED_ASSIGNMENTS=false
SBOT_RELIABILITY__VERIFIER_IMAGE=sbot-verifier:latest
# Optional: registered model with image support for document review
# SBOT_RELIABILITY__VERIFIER_MODEL=your-vision-model
SBOT_RELIABILITY__VERIFICATION_SECONDS=60
```

Use `shadow` for the internal/pilot group first: it records deep-check results
without gating publication. Use `enforce` only after evaluating real examples
and false rejections. Set `off` to stop deep verification without reverting the
completion fixes. An empty pilot list applies enabled settings to all owners.

Recovery performs at most one small output-recovery attempt, preserving the
original deadline and accumulating token cost. It does not restart a timed-out
assignment or rerun an entire completed task. Verification has a shared time
allowance across files, constrained by the enclosing deadline; its token usage
is charged with the worker's usage. Missing tools, unsupported models, malformed
verdicts and time exhaustion yield `not_verified`, never a fabricated pass.

Structured `Task reliability` log records include status, failure reason,
verification status, attempts, duration and tokens, without task content. The
same result metadata is available in persisted delegation messages and Mission
contracts. Compare first-attempt completion, empty output, repeats, duration and
token usage against the baseline. False-rejection rate requires human-labelled
pilot samples; it cannot be inferred from the judge's own verdicts.

## Verifiers

Build the separate worker:

```sh
docker build -f docker/verifier.Dockerfile -t sbot-verifier:latest .
```

The worker uses read-only input mounts, no network, dropped capabilities, a
read-only root and CPU/memory/PID/time limits. Office documents are converted by
LibreOffice, then all pages (up to 20) are rendered and supplied to a fresh,
tool-free model call. No worker conversation, charter or memory is supplied to
the reviewer. Large/unsupported payloads abstain instead of partially inspecting
and passing a whole document.

Code checks execute the supplied command on a temporary copy inside the worker.
The bundled environment includes Python and pytest. Missing language runtimes or
project dependencies abstain; provision a suitable verifier image for them.
After exit zero, a separate tool-free reviewer checks the supplied source/tests
against the request and acceptance criteria. Missing source or insufficient
coverage abstains. Include the tests and their inputs among required files.

Research checks fetch the specified public HTTPS sources independently using
the existing IP-pinned SSRF guard, including every redirect. They compare the
answer and available complete artifact text, dates, authority and contradictions
in a separate tool-free model call. Source URL, retrieval time and content hash
are recorded. Binary formats without complete text extraction remain unverified.
Model judgement remains fallible; retain source evidence for human inspection.

## Parallel execution

Opt-in file assignment isolation creates a separate `.assignments/<id>` directory
per worker. Explicit inputs are copied into `inputs/` and mounted read-only in
the container; output paths are rebased to the owner's workspace when persisted.
Workers do not receive connectors or persistent project access in this mode.
Host subprocess mode fails closed because it cannot enforce the read-only mount.
Mission successors receive predecessor artifacts as explicit input snapshots.

Persistent software projects use `sbot-worktree`, installed by the updated
`docker/developer.Dockerfile`. Create an assignment with `sbot-worktree create api`,
commit in its returned directory, then run `sbot-worktree integrate api -- python
-m pytest -q`. Integration tests a combined candidate before fast-forwarding the
main checkout. Dirty checkouts, conflicts, failed tests or a changed base prevent
promotion. Failed candidates are retained for inspection. Rebuild the developer
image before using these commands; existing running project containers retain
their previous image. File isolation remains separate from project worktrees.

No automatic conversion of all workflows to DAGs: use single-agent responses for
small tasks, parallel delegation for independent work and Missions for durable
dependencies. The existing chat continuation and collapsed tool-narration behavior
are preserved. New completion labels distinguish failures and incomplete delivery
and survive transcript reload; older messages have no invented verification badge.

## Validation

```sh
.venv/bin/python -m pytest tests/sbot_mode -q
SBOT_TEST_DOCKER_VERIFIER=1 .venv/bin/python -m pytest tests/sbot_mode/test_verifier_docker.py -q
cd web && node node_modules/typescript/bin/tsc --noEmit --incremental false
```

The opt-in integration test renders a real DOCX and executes passing/failing
Python tests in Docker. Its visual judge is a fake: it validates image handoff,
not live-model visual accuracy. Live-model evaluations, browser reconnect/mobile
QA and pilot metrics are rollout gates, not completed by unit tests.

Known baseline: `test_a_turn_on_a_small_model_is_packed_to_that_models_window`
also fails on unmodified HEAD (925 tokens versus the fixture's 800-token limit).
It is not caused by the completion-contract changes.
