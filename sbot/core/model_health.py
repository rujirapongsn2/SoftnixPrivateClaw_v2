"""Report failures from nested turns against the route they actually used."""

from typing import Any, TYPE_CHECKING
from collections.abc import Callable

from loguru import logger

from sbot.providers.base import ProviderError

if TYPE_CHECKING:
    from claw.core.model_health import ModelHealthService
    from sbot.core.loop import AgentLoop


async def run_with_model_health(
    loop: "AgentLoop",
    primary: dict[str, Any],
    fallback: dict[str, Any] | None,
    model_health: "ModelHealthService | None",
    turn_id: str,
    *args: Any,
    on_model_availability_changed: Callable[[str], None] | None = None,
    **kwargs: Any,
):
    failures: dict[str, tuple[ProviderError, Any]] = {}

    def on_failure(_model: str, error: ProviderError, is_fallback: bool) -> None:
        route = fallback if is_fallback else primary
        if model_health is not None and route and route.get("scope") == "global" and route.get("id"):
            failures[route["id"]] = (error, route.get("health_route"))

    try:
        return await loop.run_turn(turn_id, *args, **kwargs, on_provider_failure=on_failure)
    finally:
        # The callback records failures before fallback changes the active
        # route. Commit them before returning, including when both routes fail.
        # Health storage errors must not discard a successful fallback answer.
        reported = False
        for model_id, (error, snapshot) in failures.items():
            try:
                await model_health.report_failure(model_id, error, snapshot)
                reported = True
            except Exception:
                logger.opt(exception=True).warning("Nested turn health reporting failed: model_id={}", model_id)
        if reported and on_model_availability_changed is not None:
            try:
                on_model_availability_changed(turn_id)
            except Exception:
                logger.opt(exception=True).warning("Nested turn availability notification failed")
