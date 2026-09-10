# Automatic resource management for enterprise team jobs

End users provide the desired outcome and missing business facts. They do not approve token budgets, provider output limits, internal file creation, or routine verification during a background job. Explicit business review gates and connector/security permissions remain separate.

## Decisions implemented

1. Submission allocates a bounded initial budget per step, capped by organization configuration.
2. Each specialist uses the selected model's output capacity from local provider metadata or an operator override. Unknown models retain the configured baseline; no capacity is guessed. Input context respects the configured context budget.
3. A truncated response can continue twice in the same conversation. Completed tool actions remain in context; partial tool calls are discarded. The output allowance grows within the known model limit; instructions request smaller writes and incremental findings. Time and iteration ceilings still apply.
4. At a job resource boundary, the runtime uses completed steps as verified progress and measured aggregate usage to estimate the remaining work. The estimate includes configurable headroom. The organization policy grants the extension without a user prompt.
5. An extension requires new completed steps since the previous extension. Failed exhausted steps, no progress, adjustment-count limits and organization ceilings stop automatic allocation. The original work and spend remain recorded. `policy:resources` records the decision and sets `operator_attention`; this is diagnostic state, not an implemented administrator notification service.
6. Status responses expose policy decisions. Ordinary resume follows the same policy. Cancelled jobs remain cancelled. An LLM-provided budget cannot exceed the organization ceiling.

All output delivery still requires the existing completion/evidence checks. Resource renewal cannot make a failed or missing deliverable count as completed. Renewals do not replay whole nodes or switch providers.

## Configuration

Integrated deployment uses `CLAW_TEAM_WORK__...`; standalone Bot Mode uses `SBOT_TEAM_WORK__...`:

- `AUTOMATIC_RESOURCES=true`
- `MAX_JOB_TOKENS=5000000`
- `MAX_JOB_SECONDS=21600`
- `MAX_RESOURCE_ADJUSTMENTS=4`
- `RESOURCE_HEADROOM=1.5`

Per-model overrides use `CLAW_LLM__MODEL_OUTPUT_LIMITS` (or `SBOT_LLM__MODEL_OUTPUT_LIMITS`) as a JSON object keyed by the full configured model ID. Provider metadata is the default; overrides support private gateways and newly released models without source changes.

Token accounting includes input and output, including repeated/cached prompt tokens as reported by the provider. It is not a monetary estimate. Scheduler ceilings are checked between nodes; an in-flight node may cross the allowance before its usage is reported. Node time/iteration limits bound that overshoot. There is no claim of a hard per-dollar or per-call spending reservation.

Legacy non-background mission budgets retain their explicit pause behavior. Business decision gates are not automatically approved. Automatic decomposition/replanning across new nodes, durable mid-node conversation checkpoints, provider failover and administrator alert delivery are not part of this change.

## Verification

Regression coverage includes successful automatic renewal across a dependency chain, no-progress rejection, organization ceilings, historical spend retention, bounded adjustment count, configured model output capacity and adaptive output continuation. No production document job was resumed or recreated to validate the policy.
