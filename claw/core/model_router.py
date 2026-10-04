"""Pick a chat model for the Auto option from a purpose judgment.

Pure functions: the caller supplies the models it may use and the guardrail
model's judgment, and gets back a choice plus the reason, so every outcome is
testable and can be written to the audit log.
"""

from dataclasses import dataclass
from typing import Literal

# Order matters: it is the order of the `criteria` sent to the guardrail model,
# whose answer indexes its probabilities by position.
PURPOSES: tuple[str, ...] = ("general", "fast", "reasoning", "coding", "long_context", "multimodal")

PURPOSE_CRITERIA: tuple[str, ...] = (
    "General chat, writing, translation or a simple question",
    "Short routine task such as summarizing, classifying or extracting",
    "Deep multi-step reasoning, analysis, planning or math",
    "Writing or fixing source code, or running tools and commands",
    "Question about a long document or knowledge base",
    "Reading an image, screenshot or scanned page",
)

# Below this top probability the judgment is a guess, so the admin's default wins.
MIN_CONFIDENCE = 0.5

Reason = Literal["purpose", "low_confidence", "no_judgment", "no_local_model", "no_model"]


@dataclass(frozen=True, slots=True)
class Candidate:
    model_id: str
    purposes: tuple[str, ...]
    data_locality: str
    cost_rank: int
    is_default: bool = False
    is_fallback: bool = False


@dataclass(frozen=True, slots=True)
class Choice:
    model_id: str | None
    reason: Reason
    # The matched purpose probabilities that decided the pick, for the audit log.
    score: float = 0.0


def probabilities_by_purpose(probabilities: dict | list | None) -> dict[str, float] | None:
    """Map the guardrail model's positional probabilities onto purpose names.

    Returns None when the answer is missing or malformed, so callers treat it as
    no judgment instead of crashing a turn on a provider's odd payload.
    """
    if isinstance(probabilities, dict):
        values = [probabilities.get(str(i)) for i in range(len(PURPOSES))]
    elif isinstance(probabilities, list):
        values = list(probabilities)
    else:
        return None
    if len(values) != len(PURPOSES):
        return None
    out: dict[str, float] = {}
    for name, value in zip(PURPOSES, values):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            return None
        out[name] = float(value)
    return out


def choose(
    candidates: list[Candidate],
    judgment: dict[str, float] | None,
    *,
    require_local: bool,
) -> Choice:
    """Choose by purpose, then fall back to the default and the fallback model.

    A local requirement narrows the pool first and is never relaxed: when no
    local model remains the answer is `no_local_model`, not an external model.
    """
    pool = [c for c in candidates if c.data_locality == "local"] if require_local else list(candidates)
    if not pool:
        return Choice(None, "no_local_model" if require_local else "no_model")

    def default_or_fallback(reason: Reason) -> Choice:
        for pick in (next((c for c in pool if c.is_default), None), next((c for c in pool if c.is_fallback), None)):
            if pick is not None:
                return Choice(pick.model_id, reason)
        # Neither is usable. Taking the cheapest remaining model beats failing the turn.
        cheapest = min(pool, key=lambda c: (c.cost_rank, c.model_id))
        return Choice(cheapest.model_id, reason)

    if judgment is None:
        return default_or_fallback("no_judgment")
    if max(judgment.values()) < MIN_CONFIDENCE:
        return default_or_fallback("low_confidence")

    def score(c: Candidate) -> float:
        return sum(judgment.get(p, 0.0) for p in c.purposes)

    best = max(score(c) for c in pool)
    if best <= 0:
        return default_or_fallback("low_confidence")
    top = [c for c in pool if score(c) == best]
    pick = min(top, key=lambda c: (c.cost_rank, c.model_id))
    return Choice(pick.model_id, "purpose", round(best, 4))
