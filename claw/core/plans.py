"""Pure helpers for usage-tier plan cost gating.

Dependency-free (like claw/i18n.py) so the store layer, the agent runtime, and
the API routes can all import it without cycles. The DB model of a plan lives
in claw/db/models.py::PolicyPlan; this module only encodes the cost-ordering
rule that turns a plan's cost ceiling into an allow/deny decision on a model's
cost tier.
"""

from __future__ import annotations

# The model cost tiers, from cheapest to most expensive. This mirrors the
# LLMModel.cost enum ("low"|"medium"|"high"|"very_high"); a plan may use any
# model whose tier is at or below its ceiling.
COST_ORDER: tuple[str, ...] = ("low", "medium", "high", "very_high")
COST_RANK: dict[str, int] = {c: i for i, c in enumerate(COST_ORDER)}

# Fallback rank for an unrecognized value — treat it as the most expensive so
# an unknown/typo'd cost is denied rather than silently allowed.
_MAX_RANK = len(COST_ORDER) - 1


def cost_rank(cost: str | None) -> int:
    """Ordinal position of a cost tier (unknown → most expensive)."""
    return COST_RANK.get(cost or "", _MAX_RANK)


def cost_allowed(ceiling: str | None, cost: str | None) -> bool:
    """True if a model at ``cost`` is usable under a plan whose max tier is
    ``ceiling``. A null/empty ceiling means "no restriction" (unlimited), so
    every tier is allowed — this keeps the feature non-breaking when no plan
    (or an unlimited plan) applies."""
    if not ceiling:
        return True
    return cost_rank(cost) <= cost_rank(ceiling)


def builtin_plan_seeds() -> list[dict]:
    """Default usage tiers seeded on first startup (only when the table is
    empty). `Free` is the default plan; `Unlimited` (all-zero quotas +
    top-tier ceiling) removes every plan-specific restriction (cost ceiling,
    daily message/image caps) for power users. `turns_per_minute: 0` here
    means "no plan-specific throttle" — it does NOT override the operator's
    global `Settings.turns_per_minute` safety backstop, which every plan
    (including this one) is still bound by; raise the global setting if
    Unlimited/Max users need a higher per-minute ceiling than the default.
    Admins can rename/retune/add plans afterward — this is just a sensible
    starting ladder."""
    return [
        {"name": "Free", "rank": 0, "max_chat_cost": "low", "allow_image": False,
         "max_image_cost": "medium", "messages_per_day": 50, "images_per_day": 0,
         "turns_per_minute": 10, "is_default": True},
        {"name": "Plus", "rank": 1, "max_chat_cost": "medium", "allow_image": True,
         "max_image_cost": "low", "messages_per_day": 500, "images_per_day": 20,
         "turns_per_minute": 30, "is_default": False},
        {"name": "Pro", "rank": 2, "max_chat_cost": "high", "allow_image": True,
         "max_image_cost": "high", "messages_per_day": 2000, "images_per_day": 100,
         "turns_per_minute": 60, "is_default": False},
        {"name": "Max", "rank": 3, "max_chat_cost": "very_high", "allow_image": True,
         "max_image_cost": "very_high", "messages_per_day": 8000, "images_per_day": 400,
         "turns_per_minute": 120, "is_default": False},
        {"name": "Unlimited", "rank": 4, "max_chat_cost": "very_high", "allow_image": True,
         "max_image_cost": "very_high", "messages_per_day": 0, "images_per_day": 0,
         "turns_per_minute": 0, "is_default": False},
    ]
