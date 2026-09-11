# Bounded background team work

Chief of Staff and group coordinators now hand specialist work to persistent
Mission jobs by default. A receipt ends the chat turn immediately; the same
conversation can accept a different request or answer a progress question
while workers continue. Greetings and short direct answers remain chat turns.

## Routing and results

- `team_submit(goal, nodes)` saves and starts all steps of one request together.
  `depends_on` must express data dependencies, such as research → travel post.
- `delegate` and `delegate_many` use background handoffs when this feature is
  enabled. They return receipts, not specialist results. A multi-step job adds
  one coordinator report step after all submitted steps succeed.
- The report worker has no orchestration tools; its guard only allows reading
  existing results/files and recording the report. It cannot redo the work or
  make new external calls. No LLM polling or per-token coordinator wakeups.
- `team_status` reads queued/running/blocked/paused jobs with bot names and
  step states, plus recent outcomes. `mission_status` reads full results in
  pages. Group tools are scoped to the current group conversation.
- `team_cancel` prevents pending work from starting. A step already executing
  may finish; cancellation does not undo side effects. Cancelled background
  jobs are not restarted by `mission_start`.
- A background job uses one attempt per step and no automatic replanning.
  Failed upstream work never releases dependent tasks. User-requested repairs
  should inspect `mission_status` before using the existing repair controls.

Each worker receives the job goal, its own instruction, and references to
dependency results. It does not receive unrelated chat transcripts. File tools
use `.team-jobs/<job-id>/<step-id>/`; `input_files` and upstream artifacts are
copied to `inputs/` with a source-to-local-path manifest. Returned artifact paths
remain relative to the owner's workspace. This prevents routine output
collisions; it is **not a security boundary** for shell tools or connectors.
The existing `isolated_assignments` policy still takes precedence when enabled.
Shared persistent `project` tools are blocked within these job workspaces.

`finish_step` is required. Files are validated and snapshotted before publishing.
Completion, verification, and local delivery remain separate result fields.
The default deep verification setting is unchanged: structural/file validation
does not prove factual correctness or visual quality. Configure research or
document verification separately when needed.

If a worker ends without recording completion, it gets one additional model
call within the original time/iteration budget, with **only `finish_step`**
available. Earlier tool history remains in context. It cannot replay write,
shell, or connector actions in that recovery call. A truncated response keeps
its partial/failed status; truncated tool calls are never executed.

## Capacity and operation

```dotenv
SBOT_TEAM_WORK__ENABLED=true
SBOT_TEAM_WORK__MAX_PENDING_PER_OWNER=12
SBOT_TEAM_WORK__MAX_PENDING_TOTAL=128
SBOT_TEAM_WORK__MAX_PARALLEL_TOTAL=4
SBOT_TEAM_WORK__MAX_PARALLEL_PER_OWNER=2
SBOT_TEAM_WORK__MAX_STEPS=12
```

The step count excludes the automatic report step. At most one background
step uses a given bot at a time, with two executing steps per owner and four
globally by default. Queue admission counts queued, running, blocked and paused
missions. An exhausted queue returns an explicit rejection, not a receipt.
Mission token/time budgets remain enforced at scheduler boundaries; in-flight
calls can exceed a token ceiling before the next boundary. This is not a dollar
spend guarantee. Existing manual Mission planning APIs remain available.

Run **one application worker** for these limits. Admission and resource slots
are process-local; node leases and results are durable. Multiple application
workers would require shared admission/capacity enforcement before rollout.
Scheduling is bounded FIFO, not weighted fair scheduling between tenants.

Queued jobs resume after restart. An interrupted step whose attempt was already
claimed is not automatically repeated; after lease expiry its job fails for
inspection. Submission retries with identical arguments in the same chat turn
reuse the persisted job ID. A new user turn is a new request; two separately
sent messages are not deduplicated. A crash before submission was fully saved
does not return a successful receipt on retry.

Final reports use the existing durable delivery key to avoid duplicate stored
messages, and are visible in the originating conversation after reconnect.
This does not provide exactly-once execution of arbitrary external actions.
Ask mode confirms a background submission before the worker starts, using the
existing delegation approval policy.

The chat shows job titles, actual running bots, queued/paused state, and
expandable pending steps. Status refresh uses the existing API polling and
requires no model calls. Failed/completed details can be read through the tools.

To roll back routing, set `SBOT_TEAM_WORK__ENABLED=false` and restart. Existing
background jobs and reports remain in the database and continue recovery;
new `delegate` calls use the prior synchronous behavior. No database migration
is required. Rollout still requires a live provider/sandbox smoke test in the
target deployment; tests use scripted providers and isolated SQLite databases.
