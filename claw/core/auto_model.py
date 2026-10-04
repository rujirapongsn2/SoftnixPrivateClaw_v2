"""The Auto model option: choose a chat model for one turn from the guardrail model's judgment.

Kept out of runtime.py so the agent loop only asks "which model?" and gets an answer.
"""

from dataclasses import dataclass

from loguru import logger

from claw.core.model_router import PURPOSES, Candidate, Choice, choose
from claw.core.plans import cost_rank

AUTO_MODEL = "auto"


@dataclass(frozen=True, slots=True)
class AutoRoute:
    model_id: str | None
    reason: str
    # "" when a model was chosen (or Auto fell back to the normal default route);
    # otherwise the i18n error code for the turn.
    error: str = ""


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


async def auto_available(llm_config, semantic, plan_chat_cost: str | None) -> bool:
    """Auto needs a ready guardrail model, a fallback model, and something to choose between."""
    if llm_config is None or semantic is None or semantic.status()["status"] != "ready":
        return False
    if await llm_config.fallback_model_for(plan_chat_cost) is None:
        return False
    return len(await llm_config.enabled_models(None, max_cost=plan_chat_cost)) >= 2


async def resolve_auto(llm_config, decision, plan_chat_cost, *, has_media: bool) -> tuple[AutoRoute, Choice | None]:
    """Turn a routing decision into a model. `decision` is None when no check ran."""
    if decision is None:
        return AutoRoute(None, "unavailable"), None
    judgment = decision.judgment
    if has_media:
        # The check reads text only. An attached image is a vision task whatever the words say.
        judgment = {name: 0.0 for name in PURPOSES} | {"multimodal": 1.0}
    choice = choose(await candidates_for(llm_config, plan_chat_cost), judgment, require_local=decision.local_required)
    if choice.model_id is None:
        error = "no_local_model" if choice.reason == "no_local_model" else "model_selection_required"
        return AutoRoute(None, choice.reason, error), choice
    return AutoRoute(choice.model_id, choice.reason), choice


def log_choice(route: AutoRoute, choice: Choice | None, decision) -> dict:
    """The audit payload for one Auto decision."""
    payload = {
        "model": route.model_id,
        "reason": route.reason,
        "score": choice.score if choice else 0.0,
        "local_required": bool(decision and decision.local_required),
        "local_reasons": list(decision.local_reasons) if decision else [],
    }
    logger.info("Auto model route model={} reason={} local_required={}", route.model_id, route.reason, payload["local_required"])
    return payload
