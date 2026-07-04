from claw.core.context import ContextAssembler


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_assemble_keeps_recent_history_within_budget():
    assembler = ContextAssembler(max_context_tokens=200)
    history = []
    for i in range(20):
        history.append(_msg("user", f"question {i} " + "x" * 100))
        history.append(_msg("assistant", f"answer {i} " + "y" * 100))
    result = assembler.assemble("system", history, _msg("user", "current"))

    assert result[0]["role"] == "system"
    assert result[-1]["content"] == "current"
    # Oldest history must be trimmed away.
    kept_history = result[1:-1]
    assert len(kept_history) < len(history)
    # History must start at a user turn.
    assert kept_history[0]["role"] == "user"


def test_assemble_never_splits_tool_pairs():
    assembler = ContextAssembler(max_context_tokens=10_000)
    history = [
        _msg("user", "do it"),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "result"},
        _msg("assistant", "done"),
    ]
    result = assembler.assemble("system", history, _msg("user", "next"))
    roles = [m["role"] for m in result[1:-1]]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_zero_budget_drops_history():
    assembler = ContextAssembler(max_context_tokens=1)
    history = [_msg("user", "old"), _msg("assistant", "old answer")]
    result = assembler.assemble("system", history, _msg("user", "current"))
    assert [m["role"] for m in result] == ["system", "user"]
