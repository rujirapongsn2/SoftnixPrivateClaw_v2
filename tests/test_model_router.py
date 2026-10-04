from claw.core.model_router import PURPOSES, Candidate, choose, probabilities_by_purpose


def cand(model_id, purposes, *, local=False, cost=1, default=False, fallback=False):
    return Candidate(model_id, tuple(purposes), "local" if local else "external", cost, default, fallback)


def judged(**kw):
    return {p: kw.get(p, 0.0) for p in PURPOSES}


POOL = [
    cand("general-1", ["general"], default=True, cost=2),
    cand("coder-1", ["coding"], cost=3),
    cand("coder-cheap", ["coding", "fast"], cost=1),
    cand("thinker", ["reasoning"], cost=4, fallback=True),
]


def test_picks_the_model_whose_purposes_match_and_breaks_ties_on_cost():
    choice = choose(POOL, judged(coding=0.9, general=0.1), require_local=False)
    assert (choice.model_id, choice.reason) == ("coder-cheap", "purpose")


def test_a_model_with_several_purposes_scores_their_sum():
    pool = [cand("a", ["coding"]), cand("b", ["coding", "reasoning"])]
    assert choose(pool, judged(coding=0.5, reasoning=0.4), require_local=False).model_id == "b"


def test_low_confidence_uses_the_default_not_a_guess():
    choice = choose(POOL, judged(coding=0.3, reasoning=0.3, general=0.2), require_local=False)
    assert (choice.model_id, choice.reason) == ("general-1", "low_confidence")


def test_no_judgment_uses_default_then_fallback_then_cheapest():
    assert choose(POOL, None, require_local=False).model_id == "general-1"
    no_default = [c for c in POOL if not c.is_default]
    assert choose(no_default, None, require_local=False).model_id == "thinker"
    plain = [cand("x", ["general"], cost=5), cand("y", ["general"], cost=2)]
    assert choose(plain, None, require_local=False).model_id == "y"


def test_local_requirement_narrows_the_pool_before_choosing():
    pool = [*POOL, cand("local-coder", ["coding"], local=True, cost=9)]
    choice = choose(pool, judged(coding=0.95), require_local=True)
    assert (choice.model_id, choice.reason) == ("local-coder", "purpose")


def test_local_requirement_never_falls_back_to_an_external_model():
    choice = choose(POOL, judged(coding=0.95), require_local=True)
    assert (choice.model_id, choice.reason) == (None, "no_local_model")


def test_local_default_is_used_when_the_judgment_is_weak():
    pool = [cand("ext", ["general"], default=True), cand("loc", ["fast"], local=True)]
    assert choose(pool, None, require_local=True).model_id == "loc"


def test_empty_pool_is_reported_not_raised():
    assert choose([], judged(coding=1.0), require_local=False).reason == "no_model"


def test_probabilities_map_by_position_and_reject_malformed_payloads():
    got = probabilities_by_purpose({str(i): 0.0 for i in range(6)} | {"3": 1.0})
    assert got["coding"] == 1.0 and set(got) == set(PURPOSES)
    assert probabilities_by_purpose({"0": 1.0}) is None
    assert probabilities_by_purpose({str(i): 2.0 for i in range(6)}) is None
    assert probabilities_by_purpose("nope") is None
