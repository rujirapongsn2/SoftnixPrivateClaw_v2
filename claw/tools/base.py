"""Base tool contract with JSON-schema parameter validation."""

from abc import ABC, abstractmethod
from typing import Any


class ToolError(Exception):
    """Tool failed in a way the model should see and react to."""


class Tool(ABC):
    _TYPE_MAP = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    name: str
    description: str
    parameters: dict[str, Any]
    # When True, the loop passes a `progress` callback into execute() so a
    # long-running tool can stream sub-step updates to the Execution panel.
    wants_progress: bool = False

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
        if not isinstance(params, dict):
            return [f"parameters must be an object, got {type(params).__name__}"]
        return self._validate(params, {**(self.parameters or {}), "type": "object"}, "")

    def _validate(self, val: Any, schema: dict[str, Any], path: str) -> list[str]:
        t, label = schema.get("type"), path or "parameter"
        expected = self._TYPE_MAP.get(t)
        if expected and not isinstance(val, expected):
            return [f"{label} should be {t}"]
        errors: list[str] = []
        if "enum" in schema and val not in schema["enum"]:
            errors.append(f"{label} must be one of {schema['enum']}")
        if t == "object":
            props = schema.get("properties", {})
            for key in schema.get("required", []):
                if key not in val:
                    errors.append(f"missing required {(path + '.' + key) if path else key}")
            for key, item in val.items():
                if key in props:
                    errors.extend(self._validate(item, props[key], f"{path}.{key}" if path else key))
        if t == "array" and "items" in schema:
            for i, item in enumerate(val):
                errors.extend(self._validate(item, schema["items"], f"{label}[{i}]"))
        return errors
