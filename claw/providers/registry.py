"""Provider quirk registry — spec-driven, no if-elif chains in call sites."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    # Substrings matched against the model id (lowercase).
    match: tuple[str, ...]
    supports_prompt_caching: bool = False
    # Per-model parameter overrides: (substring, {param: value}).
    model_overrides: tuple[tuple[str, dict], ...] = field(default_factory=tuple)


_SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec("anthropic", ("anthropic/", "claude"), supports_prompt_caching=True),
    ProviderSpec("openrouter", ("openrouter/",), supports_prompt_caching=True),
    ProviderSpec("openai", ("openai/", "gpt-", "o1", "o3", "o4"),
                 model_overrides=(("gpt-5", {"temperature": 1.0}),)),
    ProviderSpec("gemini", ("gemini/", "gemini-")),
    ProviderSpec("deepseek", ("deepseek/", "deepseek-")),
    ProviderSpec("moonshot", ("moonshot/", "kimi-"),
                 model_overrides=(("kimi-k2", {"temperature": 0.6}),)),
    ProviderSpec("zhipu", ("zhipu/", "glm-")),
    ProviderSpec("groq", ("groq/",)),
)


def find_spec(model: str) -> ProviderSpec | None:
    lowered = (model or "").lower()
    for spec in _SPECS:
        if any(token in lowered for token in spec.match):
            return spec
    return None


def supports_prompt_caching(model: str) -> bool:
    spec = find_spec(model)
    return spec is not None and spec.supports_prompt_caching


def apply_model_overrides(model: str, kwargs: dict) -> None:
    spec = find_spec(model)
    if not spec:
        return
    lowered = model.lower()
    for pattern, overrides in spec.model_overrides:
        if pattern in lowered:
            kwargs.update(overrides)
            return
