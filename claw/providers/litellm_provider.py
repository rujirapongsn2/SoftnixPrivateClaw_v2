"""LiteLLM-backed provider with true token streaming and prompt caching."""

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


def _short_id() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


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
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        content_parts: list[str] = []
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
                    yield TextDelta(text=text)
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

        yield ChatResult(
            content="".join(content_parts) or None,
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
