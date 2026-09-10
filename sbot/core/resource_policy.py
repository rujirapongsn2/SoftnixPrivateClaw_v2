"""Deterministic organization-owned resource decisions, independent of model prose."""
import math


def resource_decision(policy, mission, nodes, history):
    spent = mission.spent or {}
    budget = dict(mission.budget or {})
    completed = [n for n in nodes if n.status == 'done']
    remaining = [n for n in nodes if n.status not in ('done', 'skipped')]
    # A completed, validated step is evidence of progress; repeated failures
    # and a model saying 'nearly done' cannot authorize another allocation.
    if not remaining:
        return None, 'no_remaining_work'
    if len(completed) <= history.get('completed', 0):
        return None, 'no_verified_progress'
    if history.get('adjustments', 0) >= policy.max_resource_adjustments:
        return None, 'adjustment_limit'
    if any(n.status == 'error' and n.attempts >= n.max_attempts for n in remaining):
        return None, 'step_needs_recovery'
    changed = False
    for key, metric, cap in (
        ('max_tokens', 'tokens', policy.max_job_tokens),
        ('max_wall_seconds', 'seconds', policy.max_job_seconds),
    ):
        used = spent.get(metric, 0)
        if budget.get(key) and used >= budget[key]:
            estimate = math.ceil(used / len(completed) * len(remaining) * policy.resource_headroom)
            proposed = min(cap, math.ceil(used + max(1, estimate)))
            if proposed <= used:
                return None, 'organization_limit'
            budget[key] = proposed
            changed = True
    return (budget, 'verified_progress') if changed else (None, 'nonrenewable_limit')
