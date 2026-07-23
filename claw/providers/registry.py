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
    ProviderSpec(
        "openai", ("openai/", "gpt-", "o1", "o3", "o4"), model_overrides=(("gpt-5", {"temperature": 1.0}),)
    ),
    ProviderSpec("gemini", ("gemini/", "gemini-")),
    # "dashscope/" only, not a bare "qwen" token — Groq and OpenRouter both also
    # host models with "qwen" in the id, and this spec's overrides (streaming-
    # only enable_thinking) are specific to DashScope's actual API contract.
    # Checked before the "deepseek" spec below: DashScope also hosts DeepSeek
    # checkpoints (e.g. "dashscope/deepseek-r1"), whose id contains the bare
    # "deepseek-" substring that spec matches on — this must resolve to
    # dashscope (prompt caching, enable_thinking override) not deepseek.
    ProviderSpec(
        "dashscope",
        ("dashscope/",),
        supports_prompt_caching=True,
        # DashScope requires enable_thinking to be explicit for Qwen3
        # models; this app always streams, so True is always correct
        # here (DashScope only rejects enable_thinking=true for
        # non-streaming calls, which this provider never makes).
        model_overrides=(("qwen3", {"extra_body": {"enable_thinking": True}}),),
    ),
    ProviderSpec("deepseek", ("deepseek/", "deepseek-")),
    ProviderSpec("moonshot", ("moonshot/", "kimi-"), model_overrides=(("kimi-k2", {"temperature": 0.6}),)),
    # "zai/" only — genuinely routed through LiteLLM's native Z.AI transform,
    # which is confirmed to preserve cache_control. Checked before the bare
    # "glm-"/"zhipu/" catch-all below so those don't inherit prompt-caching
    # (e.g. "zai/glm-4.6" — the example used in the Admin.tsx preset — also
    # contains the bare "glm-" substring the zhipu spec below matches on, so
    # ordering here is what makes it resolve to zai, not zhipu).
    ProviderSpec("zai", ("zai/",), supports_prompt_caching=True),
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
