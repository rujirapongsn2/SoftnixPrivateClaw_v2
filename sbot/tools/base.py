"""Base tool contract with JSON-schema parameter validation."""

from abc import ABC, abstractmethod
from typing import Any

from claw.tools.schema_validation import Verdict, check


class ToolError(Exception):
    """Tool failed in a way the model should see and react to."""


class Tool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]
    # When True, the loop passes a `progress` callback into execute() so a
    # long-running tool can stream sub-step updates to the Execution panel.
    wants_progress: bool = False
    # A durable handoff or explicit completion needs no extra model round.
    ends_turn_on_success: bool = False
    # True only when an identical repeat cannot add an effect (read-only or
    # idempotent). Such a call is sent again even after an unknown outcome.
    idempotent: bool = False

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """Run the tool; return a string result for the model."""

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return self.check_params(params).messages

    def check_params(self, params: dict[str, Any]) -> Verdict:
        """Like validate_params, but tells a broken tool schema (`schema_problem`)
        apart from arguments that don't fit it (`errors`)."""
        if not isinstance(params, dict):
            return Verdict(errors=(f"parameters must be an object, got {type(params).__name__}",))
        return check(params, {**(self.parameters or {}), "type": "object"})
