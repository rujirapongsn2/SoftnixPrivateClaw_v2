import importlib

import pytest

PACKAGES = ("claw", "sbot")


def _modules(pkg):
    return (
        importlib.import_module(f"{pkg}.core.runtime"),
        importlib.import_module(f"{pkg}.core.events"),
        importlib.import_module(f"{pkg}.security.policy"),
    )


def _rule(policy, action, scopes=("output",), enabled=True):
    return policy.PolicyRule(name="r", pattern="x", action=action, scopes=scopes, enabled=enabled)


@pytest.mark.parametrize("pkg", PACKAGES)
def test_guards_output_only_when_output_can_change(pkg):
    _, _, policy = _modules(pkg)
    Action = policy.Action
    assert policy.PolicyEngine().guards_output()
    assert not policy.PolicyEngine([]).guards_output()
    assert not policy.PolicyEngine([_rule(policy, Action.MONITOR)]).guards_output()
    assert not policy.PolicyEngine([_rule(policy, Action.MASK, scopes=("input",))]).guards_output()
    assert not policy.PolicyEngine([_rule(policy, Action.MASK, enabled=False)]).guards_output()
    assert not policy.PolicyEngine([_rule(policy, Action.BLOCK)], monitor_only=True).guards_output()
    assert policy.PolicyEngine([_rule(policy, Action.BLOCK)]).guards_output()
    semantic_only = policy.PolicyEngine([])
    semantic_only.semantic = object()
    assert semantic_only.guards_output()


@pytest.mark.parametrize("pkg", PACKAGES)
def test_reasoning_passes_through_without_output_guardrails(pkg):
    runtime, events, policy = _modules(pkg)
    ev = events.ThinkingDeltaEvent(turn_id="t", text="secret 4111 1111 1111 1111")
    assert runtime._reasoning_gate(None)(ev) is ev
    assert runtime._reasoning_gate(policy.PolicyEngine([]))(ev) is ev


@pytest.mark.parametrize("pkg", PACKAGES)
def test_guarded_reasoning_keeps_only_a_throttled_signal(pkg, monkeypatch):
    runtime, events, policy = _modules(pkg)
    clock = iter([10.0, 10.5, 11.2])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    gate = runtime._reasoning_gate(policy.PolicyEngine())
    chunk = events.ThinkingDeltaEvent(turn_id="t", text="card 4111 1111 1111 1111")

    first, throttled, later = gate(chunk), gate(chunk), gate(chunk)

    assert (first.turn_id, first.text) == ("t", "")
    assert throttled is None
    assert later.text == ""
    text = events.TextDeltaEvent(turn_id="t", text="answer")
    assert gate(text) is text
