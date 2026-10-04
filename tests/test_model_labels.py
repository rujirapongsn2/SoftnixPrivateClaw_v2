"""Purpose and data-locality labels on a model: conservative defaults,
round trip through update, and server-side vocabulary validation."""

import pytest
from pydantic import ValidationError

from claw.api import llm_shared as claw_shared
from claw.db.stores import LLMConfigStore
from sbot.api import llm_shared as sbot_shared


async def _model(store: LLMConfigStore, **kw):
    p = await store.create_provider("prov", "sk-test", "", True, "openai", owner_id=None)
    return await store.create_model(p.id, "gpt-x", "gpt-x", True, "medium", "", owner_id=None, **kw)


async def test_new_model_defaults_to_general_external(db_factory):
    m = await _model(LLMConfigStore(db_factory))
    row = claw_shared.model_row(m)
    assert (row["purposes"], row["data_locality"]) == (["general"], "external")


async def test_labels_round_trip_and_survive_unrelated_patch(db_factory):
    store = LLMConfigStore(db_factory)
    m = await _model(store, purposes=["coding", "fast"], data_locality="local")
    await store.update_model(m.id, None, label="renamed")
    row = claw_shared.model_row((await store.list_models(None))[0])
    assert (row["purposes"], row["data_locality"], row["label"]) == (["coding", "fast"], "local", "renamed")
    await store.update_model(m.id, None, purposes=["reasoning"], data_locality="external")
    row = claw_shared.model_row((await store.list_models(None))[0])
    assert (row["purposes"], row["data_locality"]) == (["reasoning"], "external")


@pytest.mark.parametrize("shared", [claw_shared, sbot_shared])
def test_unknown_labels_are_rejected(shared):
    with pytest.raises(ValidationError):
        shared.ModelBody(model_id="m", purposes=["cheapest"])
    with pytest.raises(ValidationError):
        shared.ModelPatch(data_locality="onprem")
    assert shared.ModelBody(model_id="m", purposes=["long_context", "coding"], data_locality="local")
    with pytest.raises(ValidationError):
        shared.ModelBody(model_id="m", purposes=[])


@pytest.mark.parametrize("shared", [claw_shared, sbot_shared])
def test_duplicate_purposes_collapse_in_order(shared):
    assert shared.ModelBody(model_id="m", purposes=["coding", "fast", "coding"]).purposes == ["coding", "fast"]
    assert shared.ModelPatch(purposes=["fast", "fast"]).purposes == ["fast"]
