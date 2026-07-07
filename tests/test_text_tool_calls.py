"""Text-encoded tool-call recovery for models without native function calling.

Some open models (gemma, some Qwen/Hermes builds) emit tool calls as a JSON
blob in the text content instead of the OpenAI `tool_calls` field. The provider
must recover those so the tool runs — and must NOT mistake a genuine JSON answer
for a tool call.
"""

from types import SimpleNamespace

import pytest

from claw.providers.base import ChatResult, TextDelta
from claw.providers.litellm_provider import (
    LiteLLMProvider,
    _extract_text_tool_calls,
    _hold_decision,
    _tool_names,
)

VALID = {"search_knowledge", "web_search"}

# The exact shape from the bug report (gemma via Softnix GenAI).
GEMMA_BLOB = (
    '{"tool_calls": [{"name": "search_knowledge", "arguments": '
    '{"knowledge_base": "Softnix Products", "query": "Softnix Logger log"}}]}'
)


# ---- pure helpers -----------------------------------------------------------


def test_extract_wrapped_tool_calls():
    calls = _extract_text_tool_calls(GEMMA_BLOB, VALID)
    assert len(calls) == 1
    assert calls[0].name == "search_knowledge"
    assert calls[0].arguments["knowledge_base"] == "Softnix Products"


def test_extract_flat_object():
    text = '{"name": "web_search", "arguments": {"query": "hello"}}'
    calls = _extract_text_tool_calls(text, VALID)
    assert len(calls) == 1 and calls[0].name == "web_search"


def test_extract_openai_function_shape():
    text = '{"function": {"name": "web_search", "arguments": {"query": "x"}}}'
    calls = _extract_text_tool_calls(text, VALID)
    assert len(calls) == 1 and calls[0].arguments == {"query": "x"}


def test_extract_parameters_alias_and_string_args():
    # `parameters` alias, and arguments delivered as a JSON *string*.
    text = '{"name": "web_search", "parameters": "{\\"query\\": \\"y\\"}"}'
    calls = _extract_text_tool_calls(text, VALID)
    assert len(calls) == 1 and calls[0].arguments == {"query": "y"}


def test_extract_fenced_and_tagged():
    fenced = "```json\n" + GEMMA_BLOB + "\n```"
    tagged = "<tool_call>" + GEMMA_BLOB + "</tool_call>"
    assert len(_extract_text_tool_calls(fenced, VALID)) == 1
    assert len(_extract_text_tool_calls(tagged, VALID)) == 1


def test_unknown_tool_name_is_not_recovered():
    # A JSON answer that isn't one of our tools must be left alone.
    text = '{"name": "not_a_real_tool", "arguments": {}}'
    assert _extract_text_tool_calls(text, VALID) == []


def test_plain_prose_is_never_a_tool_call():
    assert _extract_text_tool_calls("Softnix Logger stores syslog and JSON logs.", VALID) == []


def test_no_valid_names_disables_recovery():
    assert _extract_text_tool_calls(GEMMA_BLOB, set()) == []


def test_hold_decision():
    # Tool-call blob → withhold; prose → stream; short structured prefix → wait.
    assert _hold_decision(GEMMA_BLOB) is True
    assert _hold_decision("Sure, here is the answer.") is False
    assert _hold_decision("{") is None  # undecided until more arrives
    # A long JSON answer with no tool markers eventually streams.
    assert _hold_decision('{"config": {"a": 1, ' + "x" * 250) is False


def test_tool_names_extraction():
    tools = [{"type": "function", "function": {"name": "web_search", "description": "d"}}]
    assert _tool_names(tools) == {"web_search"}
    assert _tool_names(None) == set()


# ---- integration through stream_chat ---------------------------------------


def _chunk(content=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice], usage=usage)


async def _drain(provider, tools):
    events = []
    async for ev in provider.stream_chat([{"role": "user", "content": "hi"}], tools=tools):
        events.append(ev)
    return events


@pytest.fixture
def tools():
    return [{"type": "function", "function": {"name": "search_knowledge", "description": "d"}}]


@pytest.mark.asyncio
async def test_stream_recovers_text_tool_call_without_leaking(monkeypatch, tools):
    """gemma streams the JSON blob → no text deltas leak, tool call is recovered."""
    # Split the blob across chunks to exercise the streaming buffer.
    mid = len(GEMMA_BLOB) // 2
    chunks = [_chunk(content=GEMMA_BLOB[:mid]), _chunk(content=GEMMA_BLOB[mid:], finish="stop")]

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in chunks:
                yield c
        return gen()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    provider = LiteLLMProvider(default_model="gemma")
    events = await _drain(provider, tools)

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "", "raw tool-call JSON must not stream to the UI"
    result = next(e for e in events if isinstance(e, ChatResult))
    assert result.content is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "search_knowledge"


@pytest.mark.asyncio
async def test_stream_preserves_normal_answer(monkeypatch, tools):
    """A genuine prose answer still streams token-by-token and stays content."""
    parts = ["Softnix ", "Logger ", "stores logs."]
    chunks = [_chunk(content=p) for p in parts]
    chunks[-1] = _chunk(content=parts[-1], finish="stop")

    async def fake_acompletion(**kwargs):
        async def gen():
            for c in chunks:
                yield c
        return gen()

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    provider = LiteLLMProvider(default_model="gemma")
    events = await _drain(provider, tools)

    deltas = [e.text for e in events if isinstance(e, TextDelta)]
    assert "".join(deltas) == "Softnix Logger stores logs."
    assert len(deltas) >= 2, "normal prose should stream incrementally, not buffer"
    result = next(e for e in events if isinstance(e, ChatResult))
    assert result.content == "Softnix Logger stores logs."
    assert result.tool_calls == []
