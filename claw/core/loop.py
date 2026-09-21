"""Streaming agent loop.

One turn = stream LLM → forward deltas → execute tool calls → iterate.
The loop owns no locks and no channel knowledge; the runtime schedules turns
per session and adapters consume events from the bus.
"""

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from claw.core.events import (
    AgentEvent,
    PlanUpdated,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)
from claw.providers.base import ChatResult, LLMProvider, ProviderError, TextDelta, ThinkingDelta
from claw.providers.registry import context_window
from claw.tools.registry import ToolRegistry

Emit = Callable[[AgentEvent], None]

# Guards a tool call before execution. Returns (possibly-masked args, block_message).
# When block_message is not None the tool is not run and the message is fed back.
ArgGuard = Callable[[str, dict[str, Any]], tuple[dict[str, Any], str | None]]

# Ask-mode confirmation gate: (turn_id, tool_name, args_preview) -> approved.
ConfirmFn = Callable[[str, str, str], Awaitable[bool]]
CheckpointFn = Callable[[dict[str, Any]], Awaitable[None]]
BeforeToolFn = Callable[[dict[str, Any]], Awaitable[None]]

# Tools gated behind a user confirmation when the session's permission mode is
# "ask": ones that touch the sandbox / run arbitrary code (`exec`), and ones
# that launch autonomous multi-step agents which can themselves run those tools
# with no further prompt (`workflow`, `spawn`) — so the gate can't be bypassed
# by delegating. `workflow` especially is long-running and expensive, so
# confirming before it starts is what the user expects.
UNSAFE_TOOLS = {"exec", "workflow", "spawn"}

_PREVIEW_CHARS = 200

# How many times one tool call may repeat within a turn before the loop stops
# executing it. A model that cannot finish a step tends to retry it verbatim
# forever, which burns the whole token budget and the wall clock without making
# progress; observed in the wild as four full rewrites of the same file.
_REPEAT_LIMIT = 3

# Retry only transient provider failures, and keep the retry window deliberately
# small so a broken upstream cannot trap a turn. The configured fallback is
# attempted once after these primary attempts are exhausted.
_MAX_TRANSIENT_RETRIES = 2
_RETRY_BASE_SECONDS = 0.25

# Tools whose repetition is judged by target path rather than by the whole
# argument set: a stuck rewrite loop produces slightly different content each
# time, so comparing full arguments would never match. edit_file is the same
# failure mode as write_file (a model whose old_text guess keeps missing
# retries with a different fragment each time), so it needs the same keying.
_PATH_KEYED_TOOLS = {"write_file", "edit_file"}

# Reading the same immutable job input again only inflates context and latency.
# Cache these exact calls for the whole resumable job, not just the first call
# after recovery. Mutation-sensitive tools retain the narrower one-replay rule.
_JOB_READ_ONCE_TOOLS = {"read_skill", "read_file", "read_excel", "read_csv", "read_pdf", "read_docx"}


# Hard cap on one tool result *as fed back to the model*. A tool result isn't
# paid for once: every later iteration of the turn re-sends the whole history,
# so an uncapped web-search/extract/fetch result (tens of thousands of
# characters is normal) taxes every remaining step. Measured on a 14-iteration
# turn: 76s to first token by the end, ~12 minutes wall clock, no answer. The
# full text still reaches the UI and the stored transcript — only the model's
# copy is trimmed.
_MAX_TOOL_RESULT_CHARS = 12_000

# Aged-out results shrink much further — but ONLY as a last resort, once the
# prompt is big enough to be a context-window risk. Prompt caching keys on an
# exact byte prefix, so rewriting a message the model was already sent forfeits
# the cache for everything before it: shrinking one more result per iteration
# would re-bill (and re-process) the entire history every single step, which
# costs far more than the few thousand characters it saves.
_RECENT_TOOL_RESULTS_KEPT = 4
_STALE_TOOL_RESULT_CHARS = 800

# What one inlined image costs the prompt, in _prompt_size()'s char units.
# Providers bill a full-size image at roughly 1.5k tokens regardless of how many
# bytes of base64 carry it, and _CEILING_CHARS_PER_INPUT_TOKEN is 1.0, so this
# is that token figure expressed in the same units the ceiling uses.
_IMAGE_BLOCK_CHARS = 1500

# Characters of prompt allowed per token of the model's input window. One char
# per token is deliberately pessimistic: English averages ~4 chars per token,
# but Thai and other non-Latin scripts run closer to 1-2, and a gate that
# under-counts them would let the prompt overrun the window it is protecting.
# The remaining slack also covers what _prompt_size() cannot see — the tool
# definitions and any provider-side prompt.
_CEILING_CHARS_PER_INPUT_TOKEN = 1.0
# Used only when the model's window is unknown (an unlisted checkpoint, or a
# BYOK gateway with a private model id). Sized off a 128k window, the smallest
# any current agent-capable model ships with: too low would cripple long turns
# on every unlisted model, and a real overrun surfaces as a provider error the
# runtime already reports.
_DEFAULT_COMPACTION_CEILING_CHARS = 120_000
# Only for billing a call whose real usage never arrived — see
# _add_estimated_usage(). Independent of the ceiling ratio above, which is a
# safety gate and errs the other way on purpose.
_ESTIMATED_CHARS_PER_TOKEN = 4.0


def _truncate_tool_result(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit] + f"\n… [truncated: {len(text)} chars total. Re-run this tool with a "
        "narrower query/range, or read the saved file, if you need the rest.]"
    )


def _add_estimated_usage(totals: dict[str, int], prompt_chars: int, streamed_chars: int) -> None:
    """Charge a cut-off call for roughly what it consumed.

    The provider reports usage only in its final chunk, which a stream that was
    abandoned mid-flight never delivers — yet the prompt was sent and the tokens
    are billed. Recording nothing would make a turn that reliably times out free,
    so the size is estimated from characters instead. 4 chars/token is the usual
    English ratio and deliberately the *conservative* end for a bill: Thai packs
    closer to 2, so this under-states rather than over-charges.
    """
    totals["prompt_tokens"] += int(prompt_chars / _ESTIMATED_CHARS_PER_TOKEN)
    totals["completion_tokens"] += int(streamed_chars / _ESTIMATED_CHARS_PER_TOKEN)


def _compaction_ceiling_chars(
    model: str | None, reserved_output_tokens: int, override: int | None = None
) -> int:
    """Prompt size, in characters, past which aged-out tool results get shrunk —
    derived from the model's own input window, since that is the thing being
    protected. The answer has to fit in the same window as the prompt, so the
    reserved output budget comes off the top (floored at half the window, in
    case max_tokens is configured absurdly high for the model).

    ``override`` is the admin's per-model value from the Control Plane; it wins
    over LiteLLM's bundled table, which doesn't know private gateways or
    brand-new checkpoints."""
    window = override if override and override > 0 else context_window(model)
    if window is None:
        return _DEFAULT_COMPACTION_CEILING_CHARS
    budget = max(window - reserved_output_tokens, window // 2)
    return int(budget * _CEILING_CHARS_PER_INPUT_TOKEN)


def _compact_sent_tool_results(order: list[str], sent: dict[str, str]) -> bool:
    """Shrink every aged-out tool result in one batch. Returns whether anything
    changed. Shrinking is monotonic — a result never grows back — so the prompt
    stays stable again immediately afterwards."""
    if len(order) <= _RECENT_TOOL_RESULTS_KEPT:
        return False
    changed = False
    for call_id in order[:-_RECENT_TOOL_RESULTS_KEPT]:
        shrunk = _truncate_tool_result(sent[call_id], _STALE_TOOL_RESULT_CHARS)
        if shrunk != sent[call_id]:
            sent[call_id] = shrunk
            changed = True
    return changed


def _prompt_messages(
    working: list[dict[str, Any]], base_len: int, sent: dict[str, str]
) -> list[dict[str, Any]]:
    """The history to send this iteration: `working`, with each of THIS turn's
    tool results replaced by the size-bounded copy in `sent`.

    Returns a new list and never mutates `working`: that list is what gets
    persisted and shown to the user, who should still see everything the tool
    actually returned. Only message *contents* are swapped, never messages
    removed, so every tool result stays paired with its tool call.

    Messages before `base_len` are earlier turns, already capped when they were
    stored. They are passed through untouched: re-trimming them would both drop
    context the model was given last turn and change a prefix that is very
    likely still in the provider's prompt cache.
    """
    if not sent:
        return working
    out = list(working[:base_len])
    for message in working[base_len:]:
        replacement = sent.get(message.get("tool_call_id")) if message.get("role") == "tool" else None
        out.append({**message, "content": replacement} if replacement is not None else message)
    return out


def _prompt_size(messages: list[dict[str, Any]]) -> int:
    """Rough character count of a prompt — enough to tell a context that grew
    too big apart from a provider that is simply slow.

    Image blocks count as a flat estimate rather than the length of their
    base64 data URL. An inlined photo is a few megabytes of characters but only
    ~1.5k tokens to the model, so measuring it literally would put every
    attachment turn permanently over the compaction ceiling and, on a timed-out
    turn, bill the user a million imaginary prompt tokens.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    total += len(str(block))
                elif block.get("type") == "image_url":
                    total += _IMAGE_BLOCK_CHARS
                else:
                    total += len(str(block.get("text") or ""))
        elif content is not None:
            total += len(str(content))
        for call in message.get("tool_calls") or ():
            total += len(str(call.get("function", {}).get("arguments") or ""))
    return total


def _call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Identity of a tool call, for detecting a model stuck repeating itself."""
    if name in _PATH_KEYED_TOOLS and isinstance(arguments.get("path"), str):
        return f"{name}:{arguments['path']}"
    return f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"


def _exact_call_signature(name: str, arguments: dict[str, Any]) -> str:
    """Exact identity used only to prevent replay after provider recovery.

    This must include every argument. The regular loop-breaker intentionally
    groups writes by path, but a fallback model may legitimately write new
    content to the same path and that must still execute.
    """
    return f"{name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)}"


def _invalidate_read_cache(results: dict[str, str], path: str | None = None) -> None:
    """Drop cached workspace reads after a mutation; read_skill stays immutable."""
    for signature in list(results):
        name, separator, raw_args = signature.partition(":")
        if not separator or name not in _JOB_READ_ONCE_TOOLS or name == "read_skill":
            continue
        if path is None:
            results.pop(signature, None)
            continue
        try:
            cached_args = json.loads(raw_args)
            cached_path = str(
                cached_args.get("path")
                or cached_args.get("file_path")
                or cached_args.get("filename")
                or ""
            )
        except (json.JSONDecodeError, AttributeError):
            cached_path = ""
        if cached_path == path:
            results.pop(signature, None)


def _gateway_for_model(model: str | None) -> str | None:
    if not model or "/" not in model:
        return None
    return model.split("/", 1)[0]


def _provider_error_type(exc: ProviderError) -> str:
    if exc.error_type:
        return exc.error_type
    detail = str(exc).lower()
    if any(
        marker in detail
        for marker in (
            "provider_unavailable",
            "upstream error from",
            "h2 protocol error",
            "error reading a body from connection",
        )
    ):
        return "provider_unavailable"
    if any(marker in detail for marker in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(marker in detail for marker in ("429", "rate limit", "too many requests")):
        return "rate_limit"
    return "provider_error"


def _is_transient_provider_error(exc: ProviderError) -> bool:
    if exc.retryable is not None:
        return exc.retryable
    return _provider_error_type(exc) in {"provider_unavailable", "timeout"}


# Directories/suffixes we never surface as artifacts when scanning for files an
# `exec` command created (build/cache noise, VCS internals, hidden dotfiles).
_ARTIFACT_IGNORE_DIRS = {"__pycache__", "node_modules"}
_ARTIFACT_IGNORE_SUFFIXES = {".pyc", ".pyo"}
# Intermediate files a turn routinely produces on the way to its actual
# deliverable: a "make me a PDF" turn writes a Python script, a JSON/XML
# payload or two, then the PDF. Those helpers were all surfaced as identical
# download chips, which non-technical users read as "which of these am I
# supposed to download?". They stay in the workspace and remain reachable by
# their direct /files/ URL (and by the agent's own tools) — only the
# automatic artifact chips are suppressed.
_ARTIFACT_HIDDEN_SUFFIXES = {".py", ".json", ".xml", ".b64"}

# Base64 payloads the agent dumps on the way to inlining them into a report
# ("osm_map_base64.txt", "map_b64.txt"). Matched as a whole delimiter-separated
# token, never as a bare substring: "b64" occurs inside hex ids often enough to
# matter — "chart_9b64c1.png" and the "generated-a1b64f.png" name the image
# route mints are real deliverables, and hiding one leaves the user nothing at
# all (there is no workspace browser to go find it in).
_ARTIFACT_HIDDEN_TOKENS = {"b64", "base64"}
_TEMPLATE_TOKEN = "template"
_NAME_TOKEN_RE = re.compile(r"[._\-\s]+")


def _name_tokens(path: str) -> set[str]:
    return set(_NAME_TOKEN_RE.split(Path(path).stem.lower()))


def _is_artifact_hidden(path: str) -> bool:
    if Path(path).suffix.lower() in _ARTIFACT_HIDDEN_SUFFIXES:
        return True
    return bool(_ARTIFACT_HIDDEN_TOKENS & _name_tokens(path))


def _drop_shadowed_templates(artifacts: list[str]) -> list[str]:
    """Drop a "*_template.html" when the turn also produced a non-template file
    of the same type.

    A report template sitting next to the finished report rendered as two
    near-identical preview cards and users opened the wrong one. But a template
    is only an intermediate when something was built *from* it: "make me an
    invoice template" hands back exactly one file, and suppressing that one is
    indistinguishable from producing nothing.
    """
    shadowing = {Path(a).suffix.lower() for a in artifacts if _TEMPLATE_TOKEN not in _name_tokens(a)}
    return [
        a
        for a in artifacts
        if _TEMPLATE_TOKEN not in _name_tokens(a) or Path(a).suffix.lower() not in shadowing
    ]


def visible_artifacts(paths: list[str]) -> list[str]:
    """The files of one turn that are worth handing a human: no helper scripts,
    no base64 payloads, no template that something else was assembled from."""
    return _drop_shadowed_templates([p for p in paths if not _is_artifact_hidden(p)])


def _split_artifacts(written: list[str]) -> tuple[list[str], list[str]]:
    """Partition the files a turn wrote into (surfaced as chips, suppressed).

    Suppressed files stay in the workspace and remain reachable by their direct
    /files/ URL and by the agent's own tools; they are kept here so a turn that
    died before it could speak can still name what it produced.
    """
    visible = visible_artifacts(written)
    shown = set(visible)
    return visible, [p for p in written if p not in shown]


def _snapshot_workspace(workspace: Path) -> dict[str, float]:
    """Map workspace-relative file path -> mtime, skipping cache/VCS/hidden files.

    Used to detect files an `exec` command creates or modifies (e.g. a chart a
    Python snippet writes with matplotlib), which the file-writing tools don't
    track. Kept cheap: a single tree walk of the user's workspace.
    """
    snap: dict[str, float] = {}
    if not workspace.exists():
        return snap
    for p in workspace.rglob("*"):
        rel = p.relative_to(workspace)
        if any(part.startswith(".") or part in _ARTIFACT_IGNORE_DIRS for part in rel.parts):
            continue
        if p.suffix in _ARTIFACT_IGNORE_SUFFIXES or not p.is_file():
            continue
        try:
            snap[str(rel)] = p.stat().st_mtime
        except OSError:
            continue
    return snap


@dataclass(slots=True)
class TurnOutcome:
    final_content: str | None
    new_messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    reached_max_iterations: bool = False
    # The turn ran out of wall-clock budget rather than steps. Separate from
    # reached_max_iterations because the step count can be nowhere near its
    # limit — a few slow tool calls are enough — and the user needs to be told
    # which limit actually stopped the work.
    timed_out: bool = False
    # The caller supplied a cumulative token allowance and the next provider
    # request would exceed it. No provider call is made in this state.
    usage_limit_reached: bool = False
    # The provider stream failed after visible text had already reached the UI.
    # The partial answer is returned and persisted; the runtime appends a
    # localized marker so it cannot be mistaken for a complete response.
    interrupted: bool = False
    # Why the final call stopped. Only meaningful when final_content is empty:
    # it separates "cut off at the output cap" from "answered with nothing",
    # which need different messages. The runtime does that mapping (it owns the
    # locale); the loop just reports what the provider said.
    finish_reason: str = "stop"
    blocked_reason: str = ""
    # Workspace-relative paths of files the agent created/edited this turn, so
    # the UI can offer them as downloadable/openable artifacts.
    artifacts: list[str] = field(default_factory=list)
    # The rest of what the turn wrote: helper scripts, base64 payloads and
    # superseded templates that don't earn a chip. Recorded so a turn that ran
    # out of budget before it could speak can still say what it produced.
    hidden_artifacts: list[str] = field(default_factory=list)
    # Per-turn cost shape, recorded alongside tokens: how many LLM round-trips
    # the turn took, how many tools it ran, and how long the user waited for the
    # first visible character. Tokens alone can't tell a one-shot answer apart
    # from a five-tool detour that produced the same reply.
    iterations: int = 0
    tool_calls: int = 0
    ttft_ms: int = 0
    duration_ms: int = 0
    # Size of the largest prompt actually sent this turn, split into the tool
    # schemas (a fixed cost paid on every single call) and the conversation.
    # These are the two levers behind time-to-first-token, and the only way to
    # tell "the provider is slow" apart from "the prompt got too big".
    tool_defs_chars: int = 0
    prompt_chars: int = 0


def _elapsed_ms(started: float, mark: float | None) -> int:
    """Milliseconds from `started` to `mark`; 0 when the mark never happened
    (a turn that emitted no visible text has no time-to-first-token)."""
    if mark is None:
        return 0
    return max(0, int((mark - started) * 1000))


def _args_preview(arguments: dict[str, Any]) -> str:
    text = json.dumps(arguments, ensure_ascii=False)
    return text[:_PREVIEW_CHARS] + ("…" if len(text) > _PREVIEW_CHARS else "")


class AgentLoop:
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        model: str | None = None,
        max_iterations: int = 30,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        arg_guard: ArgGuard | None = None,
        workspace: Path | None = None,
        max_turn_seconds: float = 600,
    ):
        self.provider = provider
        self.tools = tools
        self.model = model
        self.max_iterations = max_iterations
        self.max_turn_seconds = max_turn_seconds
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.arg_guard = arg_guard
        self.workspace = workspace

    async def run_turn(
        self,
        turn_id: str,
        messages: list[dict[str, Any]],
        emit: Emit,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        context_window: int | None = None,
        fallback_model: str | None = None,
        fallback_api_key: str | None = None,
        fallback_api_base: str | None = None,
        fallback_context_window: int | None = None,
        on_fallback: Callable[[str], None] | None = None,
        permission_mode: str = "auto",
        confirm: ConfirmFn | None = None,
        tool_names: set[str] | None = None,
        resume_messages: list[dict[str, Any]] | None = None,
        resume_tool_results: dict[str, str] | None = None,
        resume_written: list[str] | None = None,
        checkpoint: CheckpointFn | None = None,
        before_tool: BeforeToolFn | None = None,
        resume_pending_call: dict[str, Any] | None = None,
        max_usage_tokens: int | None = None,
    ) -> TurnOutcome:
        """Run one user turn. Mutates a copy of `messages`; returns appended messages.

        `model`/`api_key`/`api_base` override the loop defaults for this turn only
        (per-chat model selection), falling back to the agent's configured model.
        `context_window` is the admin's per-model input-window override, if set.
        A configured fallback is adopted for the rest of the turn when an
        upstream call fails before emitting user-visible text. Transport-only
        events (usage metadata or hidden thinking) do not block recovery.
        """
        working = [*messages, *(resume_messages or [])]
        base_len = len(working)
        usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        effective_model = model or self.model
        requested_model = effective_model
        fallback_available = bool(
            fallback_model
            and (
                fallback_model != effective_model
                or fallback_api_key != api_key
                or fallback_api_base != api_base
            )
        )
        # Files the agent wrote/edited this turn (deduped, in order). Split into
        # surfaced/suppressed by _split_artifacts at each exit, since whether a
        # template counts as an intermediate depends on what else the turn wrote.
        # `baseline` lets us also detect files an `exec` command created (e.g. a
        # saved chart) by diffing the workspace.
        written: list[str] = list(dict.fromkeys(resume_written or []))
        baseline = _snapshot_workspace(self.workspace) if self.workspace is not None else {}
        started = time.monotonic()
        first_text_at: float | None = None
        tool_call_count = 0
        iterations = 0
        # Loop-breaker state: the last tool call actually executed, and how many
        # times in a row it has been executed.
        last_signature: str | None = None
        repeats = 0
        timed_out = False
        usage_limit_reached = False
        tool_defs_chars = 0
        prompt_chars = 0
        # The exact text each of this turn's tool results is sent to the model as,
        # keyed by tool_call_id, in call order. Assigned once when the result comes
        # back and only ever shrunk, so the prompt the provider sees grows by
        # appending rather than by being rewritten.
        sent_results: dict[str, str] = {}
        sent_order: list[str] = []
        # Successful tool results survive provider recovery. If a retried or
        # fallback model repeats the exact call it just received in the prompt,
        # reuse the result instead of executing the side effect a second time.
        successful_tool_results: dict[str, str] = dict(resume_tool_results or {})
        # A recovered segment may have died after a side effect committed but
        # before the next provider response. Reuse an immediately repeated exact
        # call once; normal later iterations are free to run it again after the
        # model has made progress (for example edit script → rerun generator).
        recovery_generation = bool(resume_tool_results)
        fallback_selected = False
        ceiling = _compaction_ceiling_chars(effective_model, self.max_tokens, context_window)
        if fallback_available:
            ceiling = min(
                ceiling,
                _compaction_ceiling_chars(fallback_model, self.max_tokens, fallback_context_window),
            )

        for _iteration in range(self.max_iterations):
            # Checked between iterations, so an in-flight tool always finishes;
            # the first iteration always runs, however long the turn is over.
            if iterations and self.max_turn_seconds > 0:
                if time.monotonic() - started >= self.max_turn_seconds:
                    timed_out = True
                    break
            iterations += 1
            result: ChatResult | None = None
            definitions = self.tools.get_definitions(tool_names)
            prompt = _prompt_messages(working, base_len, sent_results)
            size = _prompt_size(prompt)
            if size > ceiling and _compact_sent_tool_results(sent_order, sent_results):
                # Past the ceiling, fitting the context window outranks keeping the
                # cached prefix intact — but only here, not on every iteration.
                prompt = _prompt_messages(working, base_len, sent_results)
                size = _prompt_size(prompt)
                logger.info("Turn {} compacted stale tool results to {} chars", turn_id, size)
            if not tool_defs_chars:
                tool_defs_chars = len(json.dumps(definitions, ensure_ascii=False, default=str))
            prompt_chars = max(prompt_chars, size)
            message_tokens = self.provider.count_tokens(prompt, effective_model)
            definition_tokens = self.provider.count_tokens(
                [{"role": "system", "content": json.dumps(definitions, ensure_ascii=False)}],
                effective_model,
            )
            # Provider accounting may add wrapper tokens around messages and
            # tool schemas. Keep a small reserve so the hard cumulative cap is
            # not crossed merely because the local tokenizer omitted wrappers.
            request_tokens = max(1, int((message_tokens + definition_tokens) * 1.05) + 8)
            remaining_tokens = (
                None
                if max_usage_tokens is None
                else max_usage_tokens - sum(usage_total.values())
            )
            if remaining_tokens is not None and remaining_tokens <= request_tokens:
                usage_limit_reached = True
                iterations -= 1  # no provider request was made
                logger.warning(
                    "Turn {} stopped before provider call: estimated request {} tokens exceeds "
                    "remaining cumulative allowance {}",
                    turn_id,
                    request_tokens,
                    max(0, remaining_tokens),
                )
                break
            request_max_tokens = self.max_tokens
            if remaining_tokens is not None:
                request_max_tokens = max(1, min(self.max_tokens, remaining_tokens - request_tokens))
            # Text streamed so far this iteration. The user has already seen it,
            # so if the deadline cuts the stream off mid-answer it is kept as
            # the turn's answer rather than thrown away for a bare error.
            partial: list[str] = []
            # The budget has to bind DURING a call, not only between them: a
            # single slow call (76s just to first token was observed) can push
            # a 600s turn past 700s, and the user is told "too long" only after
            # waiting all of it. `None` = no budget, which asyncio.timeout
            # treats as no deadline.
            retry_count = 0
            try:
                while True:
                    transport_stream_started = False
                    attempt_generated_chars = 0
                    remaining = (
                        max(0.0, self.max_turn_seconds - (time.monotonic() - started))
                        if self.max_turn_seconds > 0
                        else None
                    )
                    try:
                        async with asyncio.timeout(remaining) as budget:
                            async for event in self.provider.stream_chat(
                                prompt,
                                tools=definitions,
                                model=effective_model,
                                max_tokens=request_max_tokens,
                                temperature=self.temperature,
                                api_key=api_key,
                                api_base=api_base,
                            ):
                                transport_stream_started = True
                                if isinstance(event, TextDelta):
                                    attempt_generated_chars += len(event.text)
                                    if first_text_at is None:
                                        first_text_at = time.monotonic()
                                    partial.append(event.text)
                                    emit(TextDeltaEvent(turn_id=turn_id, text=event.text))
                                elif isinstance(event, ThinkingDelta):
                                    attempt_generated_chars += len(event.text)
                                    emit(ThinkingDeltaEvent(turn_id=turn_id, text=event.text))
                                elif isinstance(event, ChatResult):
                                    result = event
                        break
                    except ProviderError as exc:
                        transport_stream_started = (
                            transport_stream_started or exc.transport_stream_started
                        )
                        visible_output_started = bool(partial)
                        committed_output_started = bool(successful_tool_results)
                        transient = _is_transient_provider_error(exc)
                        retry_delay = _RETRY_BASE_SECONDS * (2**retry_count)
                        retry_delay += random.uniform(0, retry_delay * 0.2)
                        remaining_after_failure = (
                            max(0.0, self.max_turn_seconds - (time.monotonic() - started))
                            if self.max_turn_seconds > 0
                            else None
                        )
                        has_retry_budget = bool(
                            remaining_after_failure is None
                            or remaining_after_failure > retry_delay
                        )
                        will_retry = bool(
                            transient
                            and not fallback_selected
                            and not visible_output_started
                            and retry_count < _MAX_TRANSIENT_RETRIES
                            and has_retry_budget
                        )
                        will_fallback = bool(
                            fallback_available and not visible_output_started and not will_retry
                        )
                        logger.bind(
                            turn_id=turn_id,
                            requested_model=requested_model,
                            effective_model=effective_model,
                            gateway=exc.gateway or _gateway_for_model(effective_model),
                            provider=exc.gateway or _gateway_for_model(effective_model),
                            upstream_provider=exc.upstream_provider,
                            error_type=_provider_error_type(exc),
                            retry_count=retry_count,
                            transport_stream_started=transport_stream_started,
                            visible_output_started=visible_output_started,
                            committed_output_started=committed_output_started,
                            fallback_available=fallback_available,
                            fallback_selected=fallback_selected or will_fallback,
                            tool_side_effect_state=(
                                "completed" if committed_output_started else "none"
                            ),
                        ).warning("LLM provider attempt failed")

                        # Usage normally arrives only with ChatResult. Account
                        # for an interrupted transport attempt, but do not bill
                        # a request rejected before the stream produced anything.
                        if transport_stream_started:
                            _add_estimated_usage(usage_total, size, attempt_generated_chars)

                        if visible_output_started:
                            streamed = "".join(partial)
                            working.append({"role": "assistant", "content": streamed})
                            shown, suppressed = _split_artifacts(written)
                            return TurnOutcome(
                                final_content=streamed,
                                finish_reason="provider_error",
                                new_messages=working[base_len:],
                                usage=usage_total,
                                interrupted=True,
                                artifacts=shown,
                                hidden_artifacts=suppressed,
                                iterations=iterations,
                                tool_calls=tool_call_count,
                                ttft_ms=_elapsed_ms(started, first_text_at),
                                duration_ms=_elapsed_ms(started, time.monotonic()),
                                tool_defs_chars=tool_defs_chars,
                                prompt_chars=prompt_chars,
                            )

                        if will_retry:
                            retry_count += 1
                            recovery_generation = bool(successful_tool_results)
                            await asyncio.sleep(retry_delay)
                            continue

                        if will_fallback:
                            logger.bind(
                                turn_id=turn_id,
                                requested_model=requested_model,
                                failed_model=effective_model,
                                fallback_model=fallback_model,
                                retry_count=retry_count,
                            ).warning("Switching turn to configured fallback model")
                            effective_model = fallback_model
                            api_key = fallback_api_key
                            api_base = fallback_api_base
                            context_window = fallback_context_window
                            fallback_available = False
                            fallback_selected = True
                            recovery_generation = bool(successful_tool_results)
                            if on_fallback is not None:
                                on_fallback(effective_model)
                            continue
                        raise
            except TimeoutError:
                # A provider that raises TimeoutError of its own (its per-request
                # HTTP budget) lands here too, and that is NOT the turn running
                # out of time — it's a provider failure, which the runtime
                # reports differently. budget.expired() tells the two apart.
                if not budget.expired():
                    raise
                timed_out = True
                streamed = "".join(partial)
                # The interrupted call never yielded a ChatResult, so its usage
                # would otherwise be dropped from the ledger entirely.
                _add_estimated_usage(usage_total, size, len(streamed))
                logger.warning(
                    "Turn {} timed out mid-stream; billing an estimate for the cut-off call "
                    "(prompt {} chars, streamed {} chars)",
                    turn_id, size, len(streamed),
                )
                if streamed:
                    working.append({"role": "assistant", "content": streamed})
                    shown, suppressed = _split_artifacts(written)
                    return TurnOutcome(
                        final_content=streamed,
                        finish_reason="timeout",
                        new_messages=working[base_len:],
                        usage=usage_total,
                        timed_out=True,
                        artifacts=shown,
                        hidden_artifacts=suppressed,
                        iterations=iterations,
                        tool_calls=tool_call_count,
                        ttft_ms=_elapsed_ms(started, first_text_at),
                        duration_ms=_elapsed_ms(started, time.monotonic()),
                        tool_defs_chars=tool_defs_chars,
                        prompt_chars=prompt_chars,
                    )
                break

            if result is None:
                raise RuntimeError("provider stream ended without a result")
            for key in usage_total:
                usage_total[key] += result.usage.get(key, 0)

            if not result.has_tool_calls:
                # An empty final message is never stored: it renders as a blank
                # bubble, and it comes back as a content-less assistant turn in
                # the next prompt's history, which some providers reject. The
                # runtime surfaces it as a visible error instead.
                if result.content:
                    working.append({"role": "assistant", "content": result.content})
                shown, suppressed = _split_artifacts(written)
                return TurnOutcome(
                    final_content=result.content,
                    finish_reason=result.finish_reason,
                    new_messages=working[base_len:],
                    usage=usage_total,
                    artifacts=shown,
                    hidden_artifacts=suppressed,
                    iterations=iterations,
                    tool_calls=tool_call_count,
                    ttft_ms=_elapsed_ms(started, first_text_at),
                    duration_ms=_elapsed_ms(started, time.monotonic()),
                    tool_defs_chars=tool_defs_chars,
                    prompt_chars=prompt_chars,
                )

            working.append(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                            },
                        }
                        for tc in result.tool_calls
                    ],
                }
            )
            for tc in result.tool_calls:
                tool_call_count += 1
                tool_executed = False
                exec_snapshot: dict[str, float] | None = None
                args_preview = _args_preview(tc.arguments)
                emit(ToolStarted(turn_id=turn_id, tool=tc.name, args_preview=args_preview))
                logger.info("Tool call: {}({})", tc.name, args_preview)
                args = tc.arguments
                block_message: str | None = None
                # Loop-breaker identity: only back-to-back repeats count — any
                # different call in between may have changed the state this one
                # reads (edit a script, then re-run it), which makes the repeat
                # legitimate.
                signature = _call_signature(tc.name, tc.arguments)
                replay_signature = _exact_call_signature(tc.name, tc.arguments)
                uncertain_replay = bool(
                    resume_pending_call
                    and resume_pending_call.get("signature") == replay_signature
                )
                replayed_result = (
                    successful_tool_results.get(replay_signature)
                    if recovery_generation or tc.name in _JOB_READ_ONCE_TOOLS
                    else None
                )
                if uncertain_replay:
                    tool_result = (
                        "Recovery notice: this exact tool call may already have completed before the "
                        "previous process stopped. It was not run again. Inspect the workspace/output "
                        "and continue from the observed state."
                    )
                    resume_pending_call = None
                elif replayed_result is not None:
                    tool_result = replayed_result
                    logger.bind(
                        turn_id=turn_id,
                        tool=tc.name,
                        tool_call_id=tc.id,
                    ).warning("Reused successful tool result during provider recovery")
                else:
                    if signature != last_signature:
                        last_signature = signature
                        repeats = 0
                    # Ask-mode gate: pause for user approval before an unsafe tool.
                    # Checked before the loop breaker below — a human re-approving
                    # the same call each time is this call's safety gate, not the
                    # loop, so it must keep being asked rather than getting silently
                    # auto-blocked once the repeat count crosses the limit.
                    gated_by_confirm = (
                        permission_mode == "ask"
                        and tc.name in UNSAFE_TOOLS
                        and confirm is not None
                    )
                    if gated_by_confirm:
                        from claw.jobs.provider import current_execution
                        if current_execution.get() is not None:
                            from claw.jobs.approval import confirm as durable_confirm
                            approved = await durable_confirm(tc.name, tc.arguments)
                        else:
                            approved = await confirm(turn_id, tc.name, args_preview)
                        if not approved:
                            block_message = "The user declined to run this action."
                    # Loop breaker: only counts as a runaway loop for calls nothing
                    # human gated this turn (auto mode, or tools outside
                    # UNSAFE_TOOLS) — a call the user just explicitly approved above
                    # isn't that.
                    if (
                        block_message is None
                        and not gated_by_confirm
                        and repeats >= _REPEAT_LIMIT
                    ):
                        logger.warning(
                            "Turn {} blocked repeated tool call: {} (x{})",
                            turn_id,
                            signature[:_PREVIEW_CHARS],
                            repeats + 1,
                        )
                        block_message = (
                            f"Repeated call blocked: `{tc.name}` just ran {_REPEAT_LIMIT} times in a row "
                            "with the same arguments and nothing happened in between, so running it "
                            "again produces the same result. Do something different: take the next "
                            "step, or answer the user with what you have."
                        )
                    if block_message is None and self.arg_guard is not None:
                        args, block_message = self.arg_guard(tc.name, tc.arguments)
                    if block_message is not None:
                        tool_result = f"Error: {block_message}"
                    else:
                        # Counted here, not at the gates above: a call the user
                        # declined or the guardrail masked never ran, so it must not
                        # push the tool toward a limit that claims it already has.
                        repeats += 1

                        # Sub-step progress for long tools (workflow) → Execution panel.
                        def _progress(payload: dict, _name: str = tc.name) -> None:
                            emit(
                                ToolProgress(
                                    turn_id=turn_id,
                                    tool=_name,
                                    label=str(payload.get("label") or ""),
                                    stage=str(payload.get("stage") or ""),
                                    index=int(payload.get("index") or 0),
                                    total=int(payload.get("total") or 0),
                                    status=str(payload.get("status") or "running"),
                                )
                            )

                        # Absolute deadline for the whole turn, so a tool that opts in
                        # (e.g. workflow) can bound its own multi-step budget by what's
                        # actually left here instead of granting itself a fresh full
                        # max_turn_seconds regardless of how much of the turn is spent.
                        turn_deadline = (
                            started + self.max_turn_seconds
                            if self.max_turn_seconds > 0
                            else None
                        )
                        if before_tool is not None:
                            await before_tool(
                                {
                                    "signature": replay_signature,
                                    "name": tc.name,
                                    "arguments": args,
                                }
                            )
                        if tc.name == "exec" and self.workspace is not None:
                            exec_snapshot = _snapshot_workspace(self.workspace)
                        tool_executed = True
                        tool_result = await self.tools.execute(
                            tc.name, args, progress=_progress, deadline=turn_deadline
                        )
                        if not tool_result.startswith("Error"):
                            successful_tool_results[replay_signature] = tool_result
                if tool_executed and not tool_result.startswith("Error"):
                    if tc.name in {"write_file", "edit_file"}:
                        _invalidate_read_cache(
                            successful_tool_results,
                            str(args.get("path") or "") if isinstance(args, dict) else "",
                        )
                    elif tc.name == "exec":
                        after_exec = _snapshot_workspace(self.workspace) if self.workspace else {}
                        changed = {
                            path
                            for path in set(exec_snapshot or {}) | set(after_exec)
                            if (exec_snapshot or {}).get(path) != after_exec.get(path)
                        }
                        for path in changed:
                            _invalidate_read_cache(successful_tool_results, path)
                from claw.jobs.tool_results import observe
                await observe(tool_result)
                if tc.name == 'generate_workbook' and not tool_result.startswith('Error') and args.get('output'):
                    written.append(str(args['output']))
                # Track files the agent created/edited (successful write_file /
                # edit_file) so the UI can offer them as artifacts.
                if (
                    tc.name in ("write_file", "edit_file")
                    and not tool_result.startswith("Error")
                    and isinstance(args, dict)
                    and args.get("path")
                ):
                    p = str(args["path"])
                    if p not in written:
                        written.append(p)
                # Files created/modified by a shell command (e.g. matplotlib
                # savefig) aren't captured above, so diff the workspace vs the
                # turn's baseline and surface anything new or freshly changed.
                elif tc.name == "exec" and self.workspace is not None and not tool_result.startswith("Error"):
                    for rel, mtime in _snapshot_workspace(self.workspace).items():
                        if (rel not in baseline or mtime > baseline[rel]) and rel not in written:
                            written.append(rel)
                # Surface a plan revision to the Execution panel in real time, from
                # the args the model just sent (already persisted by the tool).
                elif tc.name == "update_plan" and not tool_result.startswith("Error"):
                    raw_steps = args.get("steps") if isinstance(args, dict) else None
                    steps = [s for s in (raw_steps or []) if isinstance(s, dict)]
                    emit(
                        PlanUpdated(
                            turn_id=turn_id,
                            goal=str(args.get("goal") or "") if isinstance(args, dict) else "",
                            steps=steps,
                        )
                    )
                emit(
                    ToolFinished(
                        turn_id=turn_id,
                        tool=tc.name,
                        result_preview=tool_result[:_PREVIEW_CHARS],
                        is_error=tool_result.startswith("Error"),
                    )
                )
                if tc.id not in sent_results:
                    sent_order.append(tc.id)
                sent_results[tc.id] = _truncate_tool_result(tool_result, _MAX_TOOL_RESULT_CHARS)
                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": tool_result,
                    }
                )
                if checkpoint is not None:
                    await checkpoint(
                        {
                            "messages": working[base_len:],
                            "tool_results": successful_tool_results,
                            "written": written,
                            "iterations": iterations,
                            "tool_calls": tool_call_count,
                        }
                    )
                if tool_result.startswith("Error: [sandbox_unavailable]"):
                    # Infrastructure recovery belongs to the runtime, not to a
                    # model repeatedly inventing equivalent shell commands.
                    answered = {m.get("tool_call_id") for m in working if m.get("role") == "tool"}
                    for pending in result.tool_calls:
                        if pending.id not in answered:
                            working.append({"role": "tool", "tool_call_id": pending.id,
                                            "name": pending.name,
                                            "content": "Error: deferred until sandbox recovery"})
                    if checkpoint is not None:
                        await checkpoint({"messages": working[base_len:],
                                          "tool_results": successful_tool_results,
                                          "written": written, "iterations": iterations,
                                          "tool_calls": tool_call_count})
                    shown, suppressed = _split_artifacts(written)
                    return TurnOutcome(
                        final_content=None, new_messages=working[base_len:], usage=usage_total,
                        blocked_reason="sandbox_unavailable", artifacts=shown,
                        hidden_artifacts=suppressed, iterations=iterations,
                        tool_calls=tool_call_count, duration_ms=_elapsed_ms(started, time.monotonic()),
                        tool_defs_chars=tool_defs_chars, prompt_chars=prompt_chars,
                    )
            recovery_generation = False

        if usage_limit_reached:
            logger.warning("Turn {} reached its cumulative token allowance", turn_id)
        elif timed_out:
            logger.warning(
                "Turn {} exceeded its time budget ({}s) after {} iterations",
                turn_id,
                self.max_turn_seconds,
                iterations,
            )
        else:
            logger.warning("Turn {} reached max iterations ({})", turn_id, self.max_iterations)
        shown, suppressed = _split_artifacts(written)
        return TurnOutcome(
            final_content=None,
            new_messages=working[base_len:],
            usage=usage_total,
            reached_max_iterations=not timed_out and not usage_limit_reached,
            timed_out=timed_out,
            usage_limit_reached=usage_limit_reached,
            artifacts=shown,
            hidden_artifacts=suppressed,
            iterations=iterations,
            tool_calls=tool_call_count,
            ttft_ms=_elapsed_ms(started, first_text_at),
            duration_ms=_elapsed_ms(started, time.monotonic()),
            tool_defs_chars=tool_defs_chars,
            prompt_chars=prompt_chars,
        )
