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

# Degeneration guard: some models (seen with qwen builds) fall into a repetition
# loop and emit the same glyph until they hit max_tokens (e.g. "ณณณณ…"×3600),
# producing a garbage answer that also poisons the next turn's context. If a
# single character repeats this many times in a row we cut the stream off and
# drop the runaway tail. The bound is far above anything real prose produces
# (a markdown rule is ~3–80 chars), so normal output is never affected.
_MAX_CHAR_RUN = 300

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


_MIME_EXT = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}


def _ext_from_mime(header: str) -> str:
    """'data:image/jpeg;base64' -> 'jpg'. Defaults to png."""
    m = re.search(r"image/([a-z0-9.+-]+)", header.lower())
    return _MIME_EXT.get(m.group(1), "png") if m else "png"


def _ext_from_url(url: str) -> str:
    """Guess an extension from a URL path. Clamped to the same known-image
    allowlist as _ext_from_mime — the URL is provider-controlled (including a
    user's own BYOK api_base), so an unrecognized extension (.svg, .htm) must
    never pass through unchanged: the workspace file-serving route renders
    files inline with no Content-Disposition, so echoing an arbitrary
    extension would let a malicious/compromised provider land a stored-XSS
    payload. Defaults to png like _ext_from_mime."""
    m = re.search(r"\.([a-z0-9]{3,4})(?:[?#]|$)", url.lower())
    return _MIME_EXT.get(m.group(1), "png") if m else "png"


def _image_part_url(img: Any) -> str:
    """Pull the url out of an MCP-style image content part, tolerating both
    dict and pydantic-object shapes (litellm's message.images entries)."""
    if isinstance(img, dict):
        iu = img.get("image_url") or {}
    else:
        iu = getattr(img, "image_url", None) or {}
    if isinstance(iu, dict):
        return iu.get("url") or ""
    return getattr(iu, "url", "") or ""


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
        # Trailing single-char run tracker for the degeneration guard.
        run_char = ""
        run_len = 0
        truncated_repeat = False

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
                    # Track the trailing run of one character; a huge run means
                    # the model is stuck in a repetition loop — stop consuming.
                    for ch in text:
                        if ch == run_char:
                            run_len += 1
                        else:
                            run_char, run_len = ch, 1
                    if run_len >= _MAX_CHAR_RUN:
                        truncated_repeat = True
                        break
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
        if truncated_repeat and run_char:
            # Drop the whole runaway tail so it doesn't persist in history or
            # bloat the next turn's context; keep the good prefix.
            full_content = full_content.rstrip(run_char)
            finish_reason = "length"
            logger.warning(
                "Cut a runaway repetition loop from {} (char {!r})", model, run_char
            )
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

    # -- image generation (separate path, never touches the agent loop) -----

    async def generate_image(
        self,
        prompt: str,
        model: str,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        size: str | None = None,
        mode: str = "chat",
        timeout: float = 120.0,
    ) -> list[tuple[bytes, str]]:
        """Generate image(s) from a text prompt. Returns [(bytes, ext), ...].

        Two paths, chosen by the caller via ``mode`` (derived from the
        provider's model_prefix):
          - "images_endpoint": OpenAI/Azure DALL·E-style /images endpoint
            (litellm.aimage_generation), returning b64_json (or a URL we fetch).
          - "chat": OpenRouter/Gemini image models return the image as an image
            content part on the chat message
            (``message.images[*].image_url.url`` data URL) — confirmed by the
            verification spike. NEVER sends tool definitions (sending tools is
            exactly what makes these models 404 as chat models).

        Runs entirely outside the agent loop: no tools, no streaming, no
        EventBus. Raises ProviderError on any failure or if no image comes back.
        """
        import base64

        effective_key = api_key or self.api_key
        effective_base = api_base or self.api_base
        try:
            if mode == "images_endpoint":
                results = await self._image_via_images_endpoint(
                    prompt, model, effective_key, effective_base, size, timeout
                )
            else:
                results = await self._image_via_chat(
                    prompt, model, effective_key, effective_base, timeout, base64
                )
        except ProviderError:
            raise
        except Exception as exc:
            logger.warning("Image generation failed for {}: {}", model, exc)
            raise ProviderError(str(exc)) from exc
        if not results:
            raise ProviderError("the model returned no image")
        return results

    async def _image_via_images_endpoint(
        self, prompt, model, key, base, size, timeout
    ) -> list[tuple[bytes, str]]:
        import base64

        from litellm import aimage_generation

        kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
            "timeout": timeout,
        }
        if size:
            kwargs["size"] = size
        if key:
            kwargs["api_key"] = key
        if base:
            kwargs["api_base"] = base
        resp = await aimage_generation(**kwargs)
        out: list[tuple[bytes, str]] = []
        for item in getattr(resp, "data", None) or []:
            b64 = getattr(item, "b64_json", None) or (item.get("b64_json") if isinstance(item, dict) else None)
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            if b64:
                out.append((base64.b64decode(b64), "png"))
            elif url:
                out.append((await self._fetch_bytes(url, timeout), _ext_from_url(url)))
        return out

    async def _image_via_chat(self, prompt, model, key, base, timeout, base64) -> list[tuple[bytes, str]]:
        from litellm import acompletion

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": timeout,
        }
        if key:
            kwargs["api_key"] = key
        if base:
            kwargs["api_base"] = base
        # Deliberately does NOT call apply_model_overrides: the dashscope qwen3
        # override sets enable_thinking=True, which DashScope only accepts on
        # streaming calls — this path is non-streaming (no image-generation
        # model in practice matches "qwen3" anyway, but don't risk it).
        resp = await acompletion(**kwargs)  # deliberately NO tools / NO stream
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return []
        msg = choices[0].message
        images = getattr(msg, "images", None) or []
        out: list[tuple[bytes, str]] = []
        for img in images:
            url = _image_part_url(img)
            if url.startswith("data:image"):
                header, _, b64 = url.partition(",")
                out.append((base64.b64decode(b64), _ext_from_mime(header)))
            elif url.startswith("http"):
                out.append((await self._fetch_bytes(url, timeout), _ext_from_url(url)))
        return out

    @staticmethod
    async def _fetch_bytes(url: str, timeout: float) -> bytes:
        import httpx

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    def count_tokens(self, messages: list[dict[str, Any]], model: str | None = None) -> int:
        try:
            from litellm import token_counter

            return token_counter(model=model or self.default_model, messages=self._sanitize(messages))
        except Exception:
            # Fallback heuristic: ~4 chars per token.
            import json

            return len(json.dumps(messages, ensure_ascii=False, default=str)) // 4
