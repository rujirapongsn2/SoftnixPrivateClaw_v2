"""Auto routing check: what kind of task is this message, and must it stay on Local AI."""

from dataclasses import dataclass, field

from loguru import logger

from claw.core.model_router import PURPOSE_CRITERIA, probabilities_by_purpose
from claw.security.semantic_rules import questions_for_rules, routing_rules, should_alert

# Auto sits in front of every answer, so it gets a much smaller budget than a rule that can
# block. Past it the turn goes to the default model.
ROUTING_BUDGET_SECONDS = 1.5


@dataclass
class RoutingDecision:
    """What the routing check learned about one message."""

    checked: bool = False
    reason: str = ""
    judgment: dict[str, float] | None = None  # purpose name -> probability
    local_required: bool = False
    local_reasons: list[str] = field(default_factory=list)
    rule_scores: dict[str, float | None] = field(default_factory=dict)
    provider: str = ""

    def require_local(self, why: str) -> None:
        self.local_required = True
        self.local_reasons.append(why)


async def route_message(monitor, text: str) -> RoutingDecision:
    """Judge `text` in one bounded request: the purpose question plus every routing rule.

    Never raises. Anything that stops the check from telling us the data is safe means Local
    AI: a secret or PII pattern, a failed provider call while a routing rule is live, and any
    unexpected error while the live rules are unknown or non-empty.
    """
    decision = RoutingDecision()
    if monitor.status()["status"] != "ready":
        return decision
    if monitor.matches_sensitive(text):
        decision.require_local("sensitive_pattern")
    live = None  # unknown until the rule store has answered
    try:
        rules = routing_rules(await monitor.rule_store.enabled()) if monitor.rule_store is not None else []
        live = [rule for rule in rules if not rule.get("dry_run", True)]
        questions = {
            "purpose": {
                "type": "score",
                "instructions": "Which description best fits the task that `text` asks for?",
                "criteria": list(PURPOSE_CRITERIA),
            },
            **questions_for_rules(rules),
        }
        result = await monitor.ask(text, questions, rules, budget=ROUTING_BUDGET_SECONDS)
        decision.provider = result.get("provider", "")
        if result.get("status") != "checked":
            decision.reason = result.get("reason", "error")
            if live:
                decision.require_local("check_failed")
            return decision
        decision.checked = True
        purpose = result.get("choices", {}).get("purpose") or {}
        decision.judgment = probabilities_by_purpose(purpose.get("probabilities"))
        scores = result.get("scores", {})
        for rule in rules:
            score = scores.get(rule["id"])
            decision.rule_scores[rule["name"]] = round(score, 3) if score is not None else None
            if score is not None and should_alert(score, float(rule.get("scale", 0))) and not rule.get("dry_run", True):
                decision.require_local(rule["name"])
    except Exception:
        logger.exception("Auto routing check failed")
        decision.reason = "error"
        if live is None or live:
            decision.require_local("check_failed")
    return decision
