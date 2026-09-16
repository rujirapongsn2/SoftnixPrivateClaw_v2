"""Streaming agent loop.

One turn = stream LLM → forward deltas → execute tool calls → iterate.
The loop owns no locks and no channel knowledge; the runtime schedules turns
per session and adapters consume events from the bus.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from sbot.core.events import (
    AgentEvent,
    DelegationDelta,
    DelegationFinished,
    DelegationStarted,
    DelegationStep,
    PlanUpdated,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)
from sbot.providers.base import ChatResult, LLMProvider, ProviderError, TextDelta, ThinkingDelta
from sbot.core.turn_context import (
    ProjectApprovalScope,
    current_project_approval_grants,
    current_turn_locale,
)
from sbot.i18n import t
from sbot.providers.registry import context_window
from sbot.tools.project import (
    PROJECT_ACTIONS_COVERED_BY_TASK_APPROVAL,
    PROJECT_ACTIONS_REQUIRING_CONFIRMATION,
)
from sbot.tools.registry import ToolRegistry

Emit = Callable[[AgentEvent], None]

# Guards a tool call before execution. Returns (possibly-masked args, block_message).
# When block_message is not None the tool is not run and the message is fed back.
ArgGuard = Callable[[str, dict[str, Any]], tuple[dict[str, Any], str | None]]

# Ask-mode confirmation gate: (turn_id, tool_name, args_preview) -> approved.
ConfirmFn = Callable[[str, str, str], Awaitable[bool]]

# Tools gated behind a user confirmation when the session's permission mode is
# "ask": ones that touch the sandbox / run arbitrary code (`exec`), and ones
# that launch autonomous multi-step agents which can themselves run those tools
# with no further prompt (`workflow`, `spawn`) — so the gate can't be bypassed
# by delegating. `workflow` especially is long-running and expensive, so
# confirming before it starts is what the user expects. `mission_gate` is here
# for a different reason: it records a decision that is the user's to make, so
# the model calling it is only ever relaying — and in ask mode the user gets to
# see the relay before it lands.
UNSAFE_TOOLS = {
    "exec", "project", "delegate", "delegate_many", "workflow", "spawn", "create_bot", "send_external", "mission_gate",
}

# A card must remain readable even when a model supplies a pathological command.
# Longer commands are rejected rather than hiding their tail behind a truncation
# marker that a user might approve without seeing.
_MAX_PROJECT_CONFIRM_COMMAND_CHARS = 4_000

_PREVIEW_CHARS = 200

# How many times one tool call may repeat within a turn before the loop stops
# executing it. A model that cannot finish a step tends to retry it verbatim
# forever, which burns the whole token budget and the wall clock without making
# progress; observed in the wild as four full rewrites of the same file.
_REPEAT_LIMIT = 3

# Tools whose repetition is judged by target path rather than by the whole
# argument set: a stuck rewrite loop produces slightly different content each
# time, so comparing full arguments would never match. edit_file is the same
# failure mode as write_file (a model whose old_text guess keeps missing
# retries with a different fragment each time), so it needs the same keying.
_PATH_KEYED_TOOLS = {"write_file", "edit_file"}


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
    if path.startswith('.deliveries/'):
        return False
    # A software project is a working tree, not a bundle of chat attachments.
    # Individual project files remain available to tools and can still be
    # deliberately published into .deliveries/ when the user requests a
    # download/export.
    if path.startswith('projects/'):
        return True
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
        if a.startswith('.deliveries/') or _TEMPLATE_TOKEN not in _name_tokens(a) or Path(a).suffix.lower() not in shadowing
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
    # Why the final call stopped. Only meaningful when final_content is empty:
    # it separates "cut off at the output cap" from "answered with nothing",
    # which need different messages. The runtime does that mapping (it owns the
    # locale); the loop just reports what the provider said.
    finish_reason: str = "stop"
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


def _project_confirmation_preview(arguments: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return an approval-card preview, or a safe block reason for a long command."""
    command = arguments.get("command")
    if isinstance(command, str) and len(command) > _MAX_PROJECT_CONFIRM_COMMAND_CHARS:
        return None, (
            "Project command is too long to approve safely. Split it into commands of at most "
            f"{_MAX_PROJECT_CONFIRM_COMMAND_CHARS} characters."
        )
    details = {
        "project": arguments.get("project"),
        "action": arguments.get("action"),
    }
    if isinstance(command, str) and command:
        details["command"] = command
    return json.dumps(details, ensure_ascii=False, indent=2), None


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
        max_context_chars: int | None = None,
        max_recovery_output_tokens: int | None = None,
    ):
        self.provider = provider
        self.tools = tools
        self.model = model
        self.max_iterations = max_iterations
        self.max_turn_seconds = max_turn_seconds
        self.max_recovery_output_tokens = max(max_tokens, max_recovery_output_tokens or max_tokens)
        self.max_context_chars = max_context_chars
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
    ) -> TurnOutcome:
        """Run one user turn. Mutates a copy of `messages`; returns appended messages.

        `model`/`api_key`/`api_base` override the loop defaults for this turn only
        (per-chat model selection), falling back to the agent's configured model.
        `context_window` is the admin's per-model input-window override, if set.
        """
        working = list(messages)
        base_len = len(working)
        usage_total: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        effective_model = model or self.model
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
        written: list[str] = []
        baseline = _snapshot_workspace(self.workspace) if self.workspace is not None else {}
        started = time.monotonic()
        first_text_at: float | None = None
        tool_call_count = 0
        iterations = 0
        # Turn-local: a cached loop may serve multiple sessions concurrently.
        active_plan: list[dict[str, Any]] = []
        continue_plan_work = False
        last_plan_answer: str | None = None
        completion_reminders = 0
        completion_instruction: str | None = None
        finalize_only = False
        finalization_rounds = 0
        output_continuations = 0
        request_output_tokens = self.max_tokens
        # Loop-breaker state: the last tool call actually executed, and how many
        # times in a row it has been executed.
        last_signature: str | None = None
        repeats = 0
        # The runtime supplies a root-turn scope shared with nested team bots. A
        # directly constructed loop (tests and background jobs) gets an isolated
        # local scope, preserving the same one-turn lifetime.
        project_approval_scope = current_project_approval_grants.get()
        if project_approval_scope is None:
            project_approval_scope = ProjectApprovalScope()
        timed_out = False
        tool_defs_chars = 0
        prompt_chars = 0
        # The exact text each of this turn's tool results is sent to the model as,
        # keyed by tool_call_id, in call order. Assigned once when the result comes
        # back and only ever shrunk, so the prompt the provider sees grows by
        # appending rather than by being rewritten.
        sent_results: dict[str, str] = {}
        sent_order: list[str] = []
        ceiling = _compaction_ceiling_chars(effective_model, self.max_recovery_output_tokens, context_window)
        if fallback_available:
            ceiling = min(
                ceiling,
                _compaction_ceiling_chars(
                    fallback_model, self.max_recovery_output_tokens, fallback_context_window
                ),
            )

        if self.max_context_chars:
            ceiling = min(ceiling, self.max_context_chars)

        for _iteration in range(self.max_iterations):
            if finalize_only:
                if finalization_rounds >= 1:
                    break
                finalization_rounds += 1
            # Checked between iterations, so an in-flight tool always finishes;
            # the first iteration always runs, however long the turn is over.
            if iterations and self.max_turn_seconds > 0:
                if time.monotonic() - started >= self.max_turn_seconds:
                    timed_out = True
                    break
            iterations += 1
            result: ChatResult | None = None
            definitions = self.tools.get_definitions()
            if finalize_only:
                definitions = [d for d in definitions if d['function']['name'] == 'finish_step']
            prompt = _prompt_messages(working, base_len, sent_results)
            size = _prompt_size(prompt)
            if size > ceiling and _compact_sent_tool_results(sent_order, sent_results):
                # Past the ceiling, fitting the context window outranks keeping the
                # cached prefix intact — but only here, not on every iteration.
                prompt = _prompt_messages(working, base_len, sent_results)
                size = _prompt_size(prompt)
                logger.info("Turn {} compacted stale tool results to {} chars", turn_id, size)
            if completion_instruction:
                # Runtime control, not a fabricated user message or durable history.
                prompt = [*prompt, {"role": "system", "content": completion_instruction}]
                completion_instruction = None
                size = _prompt_size(prompt)
            if not tool_defs_chars:
                tool_defs_chars = len(json.dumps(definitions, ensure_ascii=False, default=str))
            prompt_chars = max(prompt_chars, size)
            # Text streamed so far this iteration. The user has already seen it,
            # so if the deadline cuts the stream off mid-answer it is kept as
            # the turn's answer rather than thrown away for a bare error.
            partial: list[str] = []
            # The budget has to bind DURING a call, not only between them: a
            # single slow call (76s just to first token was observed) can push
            # a 600s turn past 700s, and the user is told "too long" only after
            # waiting all of it. `None` = no budget, which asyncio.timeout
            # treats as no deadline.
            remaining = (
                max(0.0, self.max_turn_seconds - (time.monotonic() - started))
                if self.max_turn_seconds > 0
                else None
            )
            stream_started = False
            try:
                while True:
                    try:
                        async with asyncio.timeout(remaining) as budget:
                            async for event in self.provider.stream_chat(
                                prompt,
                                tools=definitions,
                                model=effective_model,
                                max_tokens=request_output_tokens,
                                temperature=self.temperature,
                                api_key=api_key,
                                api_base=api_base,
                            ):
                                stream_started = True
                                if isinstance(event, TextDelta):
                                    if first_text_at is None:
                                        first_text_at = time.monotonic()
                                    partial.append(event.text)
                                    emit(TextDeltaEvent(turn_id=turn_id, text=event.text))
                                elif isinstance(event, ThinkingDelta):
                                    emit(ThinkingDeltaEvent(turn_id=turn_id, text=event.text))
                                elif isinstance(event, ChatResult):
                                    result = event
                        break
                    except ProviderError as exc:
                        if fallback_available and not stream_started:
                            logger.warning(
                                "Turn {} switching from model {} to fallback {} after upstream failure: {}",
                                turn_id,
                                effective_model,
                                fallback_model,
                                exc,
                            )
                            effective_model = fallback_model
                            api_key = fallback_api_key
                            api_base = fallback_api_base
                            context_window = fallback_context_window
                            fallback_available = False
                            if on_fallback is not None:
                                on_fallback(effective_model)
                            remaining = (
                                max(0.0, self.max_turn_seconds - (time.monotonic() - started))
                                if self.max_turn_seconds > 0
                                else None
                            )
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

            if result.finish_reason in ('length', 'max_tokens'):
                # Repaired JSON from a cut-off tool call is not authorization
                # to execute an incomplete command or record a partial report.
                discarded_tools = [tc.name for tc in result.tool_calls]
                result.tool_calls = []
                completion_tool = self.tools.get('finish_step')
                if (getattr(completion_tool, 'require_record', False)
                        and completion_tool.result is None and not finalize_only
                        and output_continuations < 2):
                    # Continue the existing conversation, never replay the node.
                    # Incomplete arguments are discarded, not repaired/executed.
                    output_continuations += 1
                    request_output_tokens = min(self.max_recovery_output_tokens, request_output_tokens * 2)
                    if result.content:
                        working.append({"role": "assistant", "content": result.content})
                    completion_instruction = (
                        'The last response hit its output limit. Its tool calls were NOT executed: '
                        + ', '.join(discarded_tools) + '. Continue from the successful tool results above. '
                        'Do not repeat completed actions. Generate a smaller complete call; split long '
                        'file content into sections and save progress incrementally. Do not repeat the '
                        'whole analysis in your reply. Finish the remaining work and call finish_step '
                        'with evidence and paths. The original time and iteration limits still apply.'
                    )
                    continue

            if not result.has_tool_calls:
                # An empty final message is never stored: it renders as a blank
                # bubble, and it comes back as a content-less assistant turn in
                # the next prompt's history, which some providers reject. The
                # runtime surfaces it as a visible error instead.
                recovered_plan_answer = False
                if not result.content and last_plan_answer:
                    notice = t("error.continuation_stopped", current_turn_locale.get())
                    result.content = last_plan_answer + "\n\n" + notice
                    recovered_plan_answer = True
                if result.content:
                    if recovered_plan_answer:
                        # The intermediate answer is already in history.  Keep
                        # a single authoritative assistant message rather than
                        # appending it again with the recovery notice.
                        for message in reversed(working):
                            if (
                                message.get("role") == "assistant"
                                and message.get("content") == last_plan_answer
                            ):
                                message["content"] = result.content
                                break
                        else:
                            working.append({"role": "assistant", "content": result.content})
                    else:
                        working.append({"role": "assistant", "content": result.content})
                completion_tool = self.tools.get('finish_step')
                if (getattr(completion_tool, 'require_record', False)
                        and completion_tool.result is None and not finalize_only
                        and result.finish_reason not in ('length', 'max_tokens')):
                    finalize_only = True
                    completion_instruction = (
                        'Record the outcome of the existing work with finish_step now. '
                        'Only this completion tool is available; do not repeat earlier actions. '
                        'Include the full deliverable text in summary for a text task. '
                        'If there is no usable output, record failed and say why; do not claim completion. '
                        'You have one final model call within the original time budget.'
                    )
                    continue
                unfinished = [s for s in active_plan if s.get("status") != "done"]
                paused = any(s.get("status") in {"blocked", "waiting_for_user"} for s in unfinished)
                if continue_plan_work and unfinished and not paused and result.finish_reason not in ('length', 'max_tokens'):
                    if completion_reminders < 2:
                        completion_reminders += 1
                        last_plan_answer = result.content or last_plan_answer
                        completion_instruction = (
                            "Your current task plan is unfinished. Continue the authorized work now with tools; "
                            "an intermediate artifact is not completion. Do not invent an approval gate for "
                            "reversible work (for example HTML first, then PowerPoint). Update the plan to "
                            "reflect actual results. If essential input/permission is truly missing, mark the "
                            "affected step waiting_for_user and state the specific question. If an external "
                            "failure prevents progress, mark it blocked and include the reason in the step. "
                            "Never mark unfinished work done just to end the turn. Remaining steps: "
                            + json.dumps(unfinished, ensure_ascii=False)
                        )
                        continue
                    # A non-cooperating model must not silently report success or
                    # consume the whole turn budget repeating the same final answer.
                    notice = "\n\n⚠️ " + t("error.plan_incomplete", current_turn_locale.get())
                    emit(TextDeltaEvent(turn_id=turn_id, text=notice))
                    result.content = (result.content or "") + notice
                    if working and working[-1].get("role") == "assistant":
                        working[-1]["content"] = result.content
                    else:
                        working.append({"role": "assistant", "content": result.content})
                    result.finish_reason = "plan_incomplete"
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
            terminal_results = []
            for tc in result.tool_calls:
                tool_call_count += 1
                args_preview = _args_preview(tc.arguments)
                args = tc.arguments
                block_message: str | None = None
                project_slug = (
                    str(tc.arguments.get("project") or "").strip()
                    if tc.name == "project"
                    else ""
                )
                project_scope_requested = (
                    tc.name == "project"
                    and str(tc.arguments.get("action") or "") in PROJECT_ACTIONS_REQUIRING_CONFIRMATION
                )
                project_task_grant_eligible = (
                    tc.name == "project"
                    and str(tc.arguments.get("action") or "")
                    in PROJECT_ACTIONS_COVERED_BY_TASK_APPROVAL
                )
                project_grant_active = bool(
                    project_task_grant_eligible
                    and project_slug
                    and project_approval_scope.is_approved(project_slug)
                )
                project_confirmation_required = project_scope_requested and not project_grant_active
                confirmation_preview = args_preview
                if project_confirmation_required:
                    confirmation_preview, block_message = _project_confirmation_preview(tc.arguments)
                # Keep the existing execution timeline for every other tool.
                # Only a project action that needs a user decision is delayed:
                # showing it as running before that decision is misleading.
                tool_started = not project_confirmation_required
                if tool_started:
                    emit(ToolStarted(turn_id=turn_id, tool=tc.name, args_preview=args_preview))
                    logger.info("Tool call: {}({})", tc.name, args_preview)
                if finalize_only and tc.name != 'finish_step':
                    block_message = 'Only recording the existing result is allowed during output recovery.'
                # Loop-breaker identity: only back-to-back repeats count — any
                # different call in between may have changed the state this one
                # reads (edit a script, then re-run it), which makes the repeat
                # legitimate.
                signature = _call_signature(tc.name, tc.arguments)
                if signature != last_signature:
                    last_signature = signature
                    repeats = 0
                # The first project action that can create/start a persistent
                # container is confirmed even in Auto mode. Its approval grants
                # the rest of this turn access to that exact project slug; a new
                # turn or another slug asks again. Status/stop stay immediate in
                # Auto mode because they never call _ensure().
                ask_mode_confirmation_required = (
                    permission_mode == "ask"
                    and (
                        (tc.name in UNSAFE_TOOLS and not (
                            tc.name == "project" and project_grant_active
                        ))
                        or (
                        tc.name.startswith("mcp_") and tc.name.endswith(
                            ("_publish_files", "_create_pull_request", "_dispatch_workflow")
                        )
                        )
                    )
                )
                gated_by_confirm = (project_confirmation_required or ask_mode_confirmation_required) and confirm is not None
                if block_message is None and (project_confirmation_required or ask_mode_confirmation_required) and confirm is None:
                    # Delegated/background loops do not have a client that can
                    # receive the approval card. Fail closed so they cannot
                    # create a project environment without the owner's decision.
                    block_message = "This action requires user approval, but confirmation is unavailable."
                elif block_message is None and gated_by_confirm:
                    if project_task_grant_eligible and project_slug:
                        approved = await project_approval_scope.request(
                            project_slug,
                            lambda: confirm(
                                turn_id,
                                tc.name,
                                confirmation_preview or args_preview,
                            ),
                        )
                    else:
                        approved = await confirm(
                            turn_id,
                            tc.name,
                            confirmation_preview or args_preview,
                        )
                    if not approved:
                        block_message = "The user declined to run this action."
                # Loop breaker: only counts as a runaway loop for calls nothing
                # human gated this turn (auto mode, or tools outside
                # UNSAFE_TOOLS) — a call the user just explicitly approved above
                # isn't that.
                if block_message is None and not gated_by_confirm and repeats >= _REPEAT_LIMIT:
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
                        # A tool that runs another bot reports something the
                        # Execution panel's tool-shaped substep can't express —
                        # who is working and what they were asked. Such a tool
                        # tags its payload with `kind` and gets a typed event of
                        # its own; everything else stays a substep.
                        kind = str(payload.get("kind") or "")
                        if kind == 'artifact_published':
                            path = str(payload.get('path') or '')
                            if path and path not in written:
                                written.append(path)
                            return
                        if kind == "delegation_started":
                            emit(
                                DelegationStarted(
                                    turn_id=turn_id,
                                    delegation_id=str(payload.get("delegation_id") or ""),
                                    bot_id=str(payload.get("bot_id") or ""),
                                    bot_name=str(payload.get("bot_name") or ""),
                                    role_title=str(payload.get("role_title") or ""),
                                    task=str(payload.get("task") or ""),
                                )
                            )
                            return
                        if kind == "delegation_step":
                            emit(
                                DelegationStep(
                                    turn_id=turn_id,
                                    delegation_id=str(payload.get("delegation_id") or ""),
                                    bot_id=str(payload.get("bot_id") or ""),
                                    index=int(payload.get("index") or 0),
                                    tool=str(payload.get("tool") or ""),
                                    detail=str(payload.get("detail") or ""),
                                    status=str(payload.get("status") or "running"),
                                )
                            )
                            return
                        if kind == "delegation_delta":
                            emit(
                                DelegationDelta(
                                    turn_id=turn_id,
                                    delegation_id=str(payload.get("delegation_id") or ""),
                                    bot_id=str(payload.get("bot_id") or ""),
                                    text=str(payload.get("text") or ""),
                                )
                            )
                            return
                        if kind == "delegation_finished":
                            for path in payload.get('artifacts') or []:
                                if path not in written:
                                    written.append(path)
                            emit(
                                DelegationFinished(
                                    turn_id=turn_id,
                                    delegation_id=str(payload.get("delegation_id") or ""),
                                    bot_id=str(payload.get("bot_id") or ""),
                                    text=str(payload.get("text") or ""),
                                    artifacts=[str(path) for path in payload.get("artifacts") or []],
                                    is_error=bool(payload.get("is_error")),
                                    result=payload.get("result"),
                                )
                            )
                            return
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
                    turn_deadline = started + self.max_turn_seconds if self.max_turn_seconds > 0 else None
                    # A project proposal is not running while its approval card
                    # is pending. Emit this only once execution can actually
                    # begin, so the timeline never claims an unapproved
                    # container was started.
                    emit(ToolStarted(turn_id=turn_id, tool=tc.name, args_preview=args_preview))
                    tool_started = True
                    logger.info("Tool call: {}({})", tc.name, args_preview)
                    tool_result = await self.tools.execute(
                        tc.name, args, progress=_progress, deadline=turn_deadline
                    )
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
                # Files created/modified by an ordinary shell command (e.g.
                # matplotlib savefig) aren't captured above, so diff the
                # workspace vs the turn's baseline. Persistent project files
                # intentionally do not become chat artifacts: an application
                # is delivered through its access URL, with explicit
                # publish_artifact reserved for requested downloads.
                elif tc.name == "exec" and self.workspace is not None and not tool_result.startswith("Error"):
                    for rel, mtime in _snapshot_workspace(self.workspace).items():
                        if (rel not in baseline or mtime > baseline[rel]) and rel not in written:
                            written.append(rel)
                # Surface a plan revision to the Execution panel in real time, from
                # the args the model just sent (already persisted by the tool).
                elif tc.name == "update_plan" and not tool_result.startswith("Error"):
                    raw_steps = args.get("steps") if isinstance(args, dict) else None
                    steps = [s for s in (raw_steps or []) if isinstance(s, dict)]
                    continue_plan_work = args.get("continue_work") is True
                    active_plan = [dict(s) for s in steps if str(s.get("step") or "").strip()][:40]
                    emit(
                        PlanUpdated(
                            turn_id=turn_id,
                            goal=str(args.get("goal") or "") if isinstance(args, dict) else "",
                            steps=steps,
                        )
                    )
                if tool_started:
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
                tool = self.tools.get(tc.name)
                if tool and tool.ends_turn_on_success and not tool_result.startswith('Error'):
                    terminal_results.append(tool_result)
                    # Do not execute a later, bundled tool call after a durable
                    # completion such as create_bot or team_submit.
                    break

            if terminal_results:
                final = '\n\n'.join(terminal_results)
                working.append({'role': 'assistant', 'content': final})
                shown, suppressed = _split_artifacts(written)
                return TurnOutcome(
                    final_content=final, finish_reason='stop', new_messages=working[base_len:],
                    usage=usage_total, artifacts=shown, hidden_artifacts=suppressed,
                    iterations=iterations, tool_calls=tool_call_count,
                    ttft_ms=_elapsed_ms(started, first_text_at), duration_ms=_elapsed_ms(started, time.monotonic()),
                    tool_defs_chars=tool_defs_chars, prompt_chars=prompt_chars,
                )

        if timed_out:
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
            reached_max_iterations=not timed_out,
            timed_out=timed_out,
            artifacts=shown,
            hidden_artifacts=suppressed,
            iterations=iterations,
            tool_calls=tool_call_count,
            ttft_ms=_elapsed_ms(started, first_text_at),
            duration_ms=_elapsed_ms(started, time.monotonic()),
            tool_defs_chars=tool_defs_chars,
            prompt_chars=prompt_chars,
        )
