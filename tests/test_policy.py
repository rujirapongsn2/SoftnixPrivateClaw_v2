from claw.security.policy import Action, PolicyEngine, PolicyRule


def test_masks_email_on_input():
    engine = PolicyEngine()
    d = engine.enforce("contact me at alice@example.com please", scope="input")
    assert d.masked
    assert "alice@example.com" not in d.text
    assert "[REDACTED_EMAIL]" in d.text
    assert "email" in d.matched_rules


def test_masks_credit_card_high_severity():
    engine = PolicyEngine()
    d = engine.enforce("card 4111 1111 1111 1111", scope="output")
    assert d.masked
    assert "[REDACTED_CARD]" in d.text
    assert d.severity == "high"


def test_block_rule_stops_action():
    engine = PolicyEngine(
        rules=[
            PolicyRule(
                name="forbidden",
                pattern=r"launch the missiles",
                action=Action.BLOCK,
                block_message="No.",
            )
        ]
    )
    d = engine.enforce("please launch the missiles now", scope="input")
    assert d.blocked
    assert d.message == "No."


def test_monitor_only_downgrades_enforcement():
    engine = PolicyEngine(monitor_only=True)
    d = engine.enforce("email me at bob@test.com", scope="input")
    assert not d.masked
    assert not d.blocked
    assert "bob@test.com" in d.text  # unchanged
    assert "email" in d.matched_rules  # but still recorded


def test_scope_filtering():
    engine = PolicyEngine(
        rules=[
            PolicyRule(
                name="output_only",
                pattern=r"secret",
                action=Action.MASK,
                scopes=("output",),
                placeholder="X",
            )
        ]
    )
    assert engine.enforce("secret", scope="input").action is None
    assert engine.enforce("secret", scope="output").masked


def test_clean_text_passes_through():
    engine = PolicyEngine()
    d = engine.enforce("what's the weather today?", scope="input")
    assert d.action is None
    assert d.matched_rules == []
