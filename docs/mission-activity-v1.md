# Specialist room observation, version 1

Mission nodes publish display-only observation rows to the owner's canonical
specialist room. The originating room receives a link. The scheduler, dependency
graph, model prompts and number of model calls are unchanged.

- Assignment, input references, required files, attempt, queued/running status,
  user-facing text, tool names/statuses, final result and artifacts are recorded.
- Assignment cards appear when a node is claimed, including waits for execution
  slots. Pending dependencies do not yet have activity cards.
- `__summary` retains existing leader final delivery without an observer card.
- Status is system state, not a fabricated acknowledgement from the model.
  Private reasoning, tool arguments and raw tool results are not exposed.
- A bounded snapshot keeps the latest 12,000 text characters, a 20,000-character
  final-result preview and 100 tool calls. The authoritative full result remains
  on the mission node. No historical activity is reconstructed.
- Snapshots checkpoint at most twice per second without blocking mission work.
  The visible room polls every three seconds while work is active, every ten
  seconds while idle and every thirty seconds while hidden. The live endpoint
  covers the latest 100 observations; older
  completed cards remain in normal paginated history.
- Identity includes mission, node and attempt. Reconnects update an existing row;
  retries have separate cards. Executor completion is shown as `finishing` until
  read-time reconciliation confirms the persisted scheduler state, avoiding a
  false success when committing a node fails or an attempt is superseded.
- The `observation` role is excluded from model history and memory consolidation.
  Observation does not start chat turns, emit busy events or invoke specialists.
- Writes are bounded by timeouts and isolated from task success. A failed
  checkpoint can leave incomplete observation history, but must not fail work.
- Observation tasks are tracked by MissionService and cancelled during shutdown,
  so database writes cannot leak past application teardown.
- Session ownership is checked at the endpoint; bot ownership and current group
  membership are checked before exposing an assignment.

No schema migration is required. Deploy the backend and frontend together; a
backend restart is needed for new mission executions to use the observer. Existing
finished jobs are not backfilled. V1 observes work; approving/changing an assignment
from the specialist room is outside this version.

Validation: `pytest tests/sbot_mode/test_mission_activity.py`;
`npm --prefix web run test:mission-activity`; `npm --prefix web run build`.
