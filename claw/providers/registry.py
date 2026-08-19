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
        # models; this override is only correct for streaming calls
        # (DashScope rejects enable_thinking=true on non-streaming calls).
        # apply_model_overrides() is only called from stream_chat(); the
        # non-streaming image-generation path deliberately skips it, see
        # LiteLLMProvider._image_via_chat.
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


# Matched anywhere in the model id, independent of find_spec()'s routing order.
# Text-only families first, with their vision-capable checkpoints listed as
# exceptions below (checked first in supports_vision).
_TEXT_ONLY_MODEL_TOKENS: tuple[str, ...] = ("deepseek",)
_VISION_MODEL_TOKENS: tuple[str, ...] = ("deepseek-vl",)


def find_spec(model: str) -> ProviderSpec | None:
    lowered = (model or "").lower()
    for spec in _SPECS:
        if any(token in lowered for token in spec.match):
            return spec
    return None


def supports_prompt_caching(model: str) -> bool:
    spec = find_spec(model)
    return spec is not None and spec.supports_prompt_caching


def supports_vision(model: str) -> bool:
    """Whether the model can accept image_url content blocks.

    Deliberately NOT spec-driven: _SPECS is first-match-wins on the ROUTING
    prefix, but vision capability follows the underlying model. A text-only
    DeepSeek reached through an OpenAI-compatible gateway is the id
    "openai/deepseek-v4-flash", which find_spec() resolves to the openai spec —
    so a per-spec flag would never see the deepseek part. Matching tokens
    anywhere in the id gets that case right regardless of how it's routed.

    Permissive by default: an unlisted model is unknown, not confirmed
    text-only, so it is allowed through and the reactive
    is_no_vision_support_error() check in claw/i18n.py is the safety net.
    """
    lowered = (model or "").lower()
    if any(token in lowered for token in _VISION_MODEL_TOKENS):
        return True
    return not any(token in lowered for token in _TEXT_ONLY_MODEL_TOKENS)


def apply_model_overrides(model: str, kwargs: dict) -> None:
    spec = find_spec(model)
    if not spec:
        return
    lowered = model.lower()
    for pattern, overrides in spec.model_overrides:
        if pattern in lowered:
            kwargs.update(overrides)
            return
