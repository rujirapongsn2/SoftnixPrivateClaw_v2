"""The Auto model option: choose a chat model for one turn from the guardrail model's judgment.

Kept out of runtime.py so the agent loop only asks "which model?" and gets an answer.
"""

from dataclasses import dataclass, field
from typing import Literal

from claw.core.model_router import PURPOSES, Candidate, choose
from claw.core.plans import cost_rank
from claw.i18n import t
from claw.security.semantic_routing import route_message

AUTO_MODEL = "auto"

AutoError = Literal["", "no_local_model", "model_selection_required"]
_ERROR_KEY: dict[str, str] = {
    "no_local_model": "error.no_local_model",
    "model_selection_required": "error.model_selection_required",
}


@dataclass(frozen=True, slots=True)
class AutoRoute:
    """The outcome of Auto for one message: a model, or a reason the turn must stop."""

    model_id: str | None
    reason: str
    error: AutoError = ""
    score: float = 0.0
    checked: bool = False
    local_required: bool = False
    local_reasons: tuple[str, ...] = ()
    rule_scores: dict[str, float | None] = field(default_factory=dict)

    def message(self, locale: str) -> str:
        return t(_ERROR_KEY[self.error], locale)

    def audit_payload(self) -> dict:
        return {
            "model": self.model_id, "reason": self.reason, "score": self.score, "checked": self.checked,
            "local_required": self.local_required, "local_reasons": list(self.local_reasons),
            "rule_scores": self.rule_scores,
        }


UNAVAILABLE = AutoRoute(None, "unavailable")


async def candidates_for(llm_config, plan_chat_cost: str | None) -> list[Candidate]:
    """Admin-provisioned chat models this plan may use. Never the user's own (BYOK) models,
    because their Local AI label is self-declared and cannot gate sensitive data."""
    rows = await llm_config.enabled_models(None, max_cost=plan_chat_cost)
    fallback = await llm_config.fallback_model_for(plan_chat_cost)
    return [
        Candidate(
            model_id=row["model_id"],
            purposes=tuple(row.get("purposes") or ("general",)),
            data_locality=row.get("data_locality") or "external",
            cost_rank=cost_rank(row.get("cost")),
            is_default=bool(row.get("is_default")),
            is_fallback=row["model_id"] == fallback,
        )
        for row in rows
    ]


def _usable(candidates: list[Candidate]) -> bool:
    """Auto needs something to choose between and a fallback to land on."""
    return len(candidates) >= 2 and any(c.is_fallback for c in candidates)


async def auto_available(llm_config, semantic, plan_chat_cost: str | None) -> bool:
    if llm_config is None or semantic is None or semantic.status()["status"] != "ready":
        return False
    return _usable(await candidates_for(llm_config, plan_chat_cost))


async def route_auto(llm_config, semantic, text: str, plan_chat_cost: str | None, *, has_media: bool) -> AutoRoute:
    """Decide the model for one Auto message. Run as a task alongside the input guardrail check."""
    candidates = await candidates_for(llm_config, plan_chat_cost)
    if not _usable(candidates):
        return UNAVAILABLE
    decision = await route_message(semantic, text)
    judgment = decision.judgment
    if has_media:
        # The check reads text only. An attached image is a vision task whatever the words say.
        judgment = {name: 0.0 for name in PURPOSES} | {"multimodal": 1.0}
    choice = choose(candidates, judgment, require_local=decision.local_required)
    error: AutoError = ""
    if choice.model_id is None:
        error = "no_local_model" if choice.reason == "no_local_model" else "model_selection_required"
    return AutoRoute(
        choice.model_id, choice.reason, error, choice.score, decision.checked, decision.local_required,
        tuple(decision.local_reasons), decision.rule_scores,
    )
