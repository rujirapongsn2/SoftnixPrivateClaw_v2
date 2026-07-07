"""LiteLLM-backed provider with true token streaming and prompt caching."""

import re
import secrets
import string
from collections.abc import AsyncIterator
from typing import Any

import json_repair
from loguru import logger

from claw.providers.base import (
    ChatResult,
    LLMProvider,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
)
from claw.providers.registry import apply_model_overrides, supports_prompt_caching

_ALNUM = string.ascii_letters + string.digits
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name"})

# ---- text tool-call fallback -------------------------------------------------
# Some open models (gemma, some Qwen/Hermes builds) don't populate the OpenAI
# `tool_calls` field even when tools are advertised — they emit the call as a
# JSON blob in the text content (optionally fenced or wrapped in <tool_call>
# tags). Without normalization that JSON leaks to the user as the final answer
# and the tool never runs. We detect and convert those below.
_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z_]*\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```$")
_TOOLCALL_TAG_RE = re.compile(r"</?tool_call>", re.IGNORECASE)
# Markers that betray a text-encoded tool call within a leading JSON-ish blob.
_TOOL_MARKERS = ('"tool_calls"', '"name"', '"function"', '"arguments"', '"parameters"')
# How many chars to inspect before deciding a leading structured blob is a real
# answer (e.g. a JSON sample or fenced code) rather than a tool call.
_DECISION_WINDOW = 200


def _short_id() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _hold_decision(prefix: str) -> bool | None:
    """Should streaming of `prefix` be withheld pending a tool-call check?

    Returns True (withhold — looks like a text tool call), False (stream — it's
    a normal answer), or None (undecided — need more tokens). Only leading
    structured blobs are ever held; prose streams immediately.
    """
    s = prefix.lstrip()
    if not s:
        return None
    low = s[:_DECISION_WINDOW].lower()
    structured = s[0] in "{[" or low.startswith("<tool_call") or s.startswith("```")
    if not structured:
        return False
    if any(m in low for m in _TOOL_MARKERS) or "tool_call" in low or "tool_code" in low:
        return True
    if len(s) >= _DECISION_WINDOW:
        return False  # structured but no tool markers in the window → real content
    return None


def _extract_text_tool_calls(content: str, valid_names: set[str]) -> list[ToolCall]:
    """Parse tool calls a non-native model encoded as text. [] if none valid.

    Guarded by `valid_names`: a call is only accepted when its name matches a
    real tool, so a legitimate JSON answer is never mistaken for a tool call.
    """
    if not content or not valid_names:
        return []
    text = _TOOLCALL_TAG_RE.sub("", content.strip()).strip()
    if text.startswith("```"):
        text = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text)).strip()
    if not text or text[0] not in "{[":
        return []
    try:
        parsed = json_repair.loads(text)
    except Exception:
        return []
    if isinstance(parsed, dict):
        raw = parsed["tool_calls"] if isinstance(parsed.get("tool_calls"), list) else [parsed]
    elif isinstance(parsed, list):
        raw = parsed
    else:
        return []
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name") or item.get("name")
        if not isinstance(name, str) or name not in valid_names:
            continue
        args = fn.get("arguments")
        if args is None:
            args = fn.get("parameters")
        if isinstance(args, str):
            args = json_repair.loads(args) if args.strip() else {}
        if not isinstance(args, dict):
            args = {}
        calls.append(ToolCall(id=_short_id(), name=name, arguments=args))
    return calls


class LiteLLMProvider(LLMProvider):
    def __init__(
        self,
        api_key: str = "",
        api_base: str = "",
        default_model: str = "anthropic/claude-sonnet-4-5",
    ):
        self.api_key = api_key or None
        self.api_base = api_base or None
        self.default_model = default_model

        import litellm

        litellm.suppress_debug_info = True
        litellm.drop_params = True

    # -- message preparation ------------------------------------------------

    @staticmethod
    def _sanitize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip internal keys and normalize empty content (strict providers 400 on it)."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in _ALLOWED_MSG_KEYS}
            content = clean.get("content")
            if clean.get("role") == "assistant":
                if not content:
                    clean["content"] = None if clean.get("tool_calls") else "(empty)"
            elif isinstance(content, str) and not content:
                clean["content"] = "(empty)"
            out.append(clean)
        return out

    @staticmethod
    def _apply_cache_control(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Cache the static prefix (tools + system) and the conversation tail."""

        def mark(msg: dict[str, Any]) -> dict[str, Any] | None:
            content = msg.get("content")
            if isinstance(content, str) and content:
                return {
                    **msg,
                    "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}],
                }
            if isinstance(content, list) and content:
                new_content = list(content)
                new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                return {**msg, "content": new_content}
            return None

        out = list(messages)
        for i, msg in enumerate(out):
            if msg.get("role") == "system":
                out[i] = mark(msg) or msg
                break
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "system":
                continue
            marked = mark(out[i])
            if marked is not None:
                out[i] = marked
                break
        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}
        return out, new_tools

    # -- streaming ----------------------------------------------------------

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        from litellm import acompletion

        model = model or self.default_model
        if supports_prompt_caching(model):
            messages, tools = self._apply_cache_control(messages, tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        apply_model_overrides(model, kwargs)
        # Per-call creds (admin-configured provider) win over instance defaults.
        effective_key = api_key or self.api_key
        effective_base = api_base or self.api_base
        if effective_key:
            kwargs["api_key"] = effective_key
        if effective_base:
            kwargs["api_base"] = effective_base
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        valid_names = _tool_names(tools)
        content_parts: list[str] = []
        # Streaming gate for the text tool-call fallback: while a leading blob
        # might be a text-encoded tool call we withhold deltas (`hold`), only
        # flushing what's past `emit_len` once we're sure it's real content.
        hold: bool | None = None if valid_names else False
        emit_len = 0
        # index -> {"id": str, "name": str, "arguments": str}
        pending_tool_calls: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}

        try:
            stream = await acompletion(**kwargs)
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
                    }
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield ThinkingDelta(text=reasoning)
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    full = "".join(content_parts)
                    if hold is None:
                        hold = _hold_decision(full)
                    if hold is False and len(full) > emit_len:
                        yield TextDelta(text=full[emit_len:])
                        emit_len = len(full)
                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", 0) or 0
                    slot = pending_tool_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
        except Exception as exc:
            logger.warning("LLM stream failed for {}: {}", model, exc)
            raise ProviderError(str(exc)) from exc

        tool_calls: list[ToolCall] = []
        for index in sorted(pending_tool_calls):
            slot = pending_tool_calls[index]
            if not slot["name"]:
                continue
            args = json_repair.loads(slot["arguments"]) if slot["arguments"] else {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(ToolCall(id=slot["id"] or _short_id(), name=slot["name"], arguments=args))

        full_content = "".join(content_parts)
        content: str | None = full_content or None
        # No native tool calls but a model may have encoded them as text — try
        # to recover so the tool actually runs instead of leaking JSON to the UI.
        if not tool_calls:
            text_calls = _extract_text_tool_calls(full_content, valid_names)
            if text_calls:
                logger.info("Recovered {} text-encoded tool call(s) from {}", len(text_calls), model)
                tool_calls = text_calls
                content = None  # consumed the JSON; it was withheld from streaming
                finish_reason = "tool_calls"
        # Flush any withheld text that turned out to be a genuine answer (or that
        # accompanies native tool calls) so the UI isn't left missing content.
        if content is not None and len(full_content) > emit_len:
            yield TextDelta(text=full_content[emit_len:])

        yield ChatResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    def count_tokens(self, messages: list[dict[str, Any]], model: str | None = None) -> int:
        try:
            from litellm import token_counter

            return token_counter(model=model or self.default_model, messages=self._sanitize(messages))
        except Exception:
            # Fallback heuristic: ~4 chars per token.
            import json

            return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4
