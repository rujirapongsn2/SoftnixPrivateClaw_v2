from claw.api.llm_shared import ModelPatch, ProviderPatch, update_model, update_provider
from claw.providers.image_capability import resolve_capability
from tests.conftest_app import build_api_app, client
from claw.providers.litellm_provider import LiteLLMProvider
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from claw.api.llm_shared import ModelBody, create_model
from claw.db.stores import LLMConfigStore


@pytest.mark.parametrize("owner", [None, "u1"])
async def test_vision_only_cannot_be_saved_as_image(db_factory, owner):
    store = LLMConfigStore(db_factory)
    provider = await store.create_provider("openai", "", "", model_prefix="openai", owner_id=owner)
    with pytest.raises(HTTPException) as error:
        await create_model(
            SimpleNamespace(llm_config=store),
            provider.id,
            ModelBody(model_id="openai/gpt-4o", kind="image"),
            owner,
        )
    assert error.value.status_code == 422
    assert await store.list_models(owner) == []


@pytest.mark.parametrize("owner", [None, "u1"])
async def test_drafts_enable_partial_patch_and_provider_rollback(db_factory, owner):
    store = LLMConfigStore(db_factory)
    state = SimpleNamespace(llm_config=store)
    provider = await store.create_provider("openai", "", "", model_prefix="openai", owner_id=owner)
    draft = await create_model(
        state, provider.id, ModelBody(model_id="future-model", kind="image", enabled=False), owner
    )
    with pytest.raises(HTTPException, match="422"):
        await update_model(state, draft["id"], ModelPatch(enabled=True), owner)
    assert not (await store.list_models(owner))[0].enabled
    good = await create_model(state, provider.id, ModelBody(model_id="openai/dall-e-3", kind="image"), owner)
    with pytest.raises(HTTPException):
        await update_model(state, good["id"], ModelPatch(model_id="openai/gpt-4o"), owner)
    with pytest.raises(HTTPException):
        await update_provider(
            state, provider.id, ProviderPatch(api_base="https://gateway.invalid/v1", name="changed"), owner
        )
    unchanged = await store.get_by_name("openai", owner)
    assert unchanged.api_base == "" and unchanged.model_prefix == "openai"
    assert await store.get_by_name("changed", owner) is None
    with pytest.raises(HTTPException):
        await update_model(
            state, good["id"], ModelPatch(kind="image", model_id="gemini/gemini-2.5-flash-image"), owner
        )
    assert next(m for m in await store.list_models(owner) if m.id == good["id"]).model_id == "openai/dall-e-3"
    with pytest.raises(HTTPException) as error:
        await update_model(state, good["id"], ModelPatch(label="foreign"), "another-user")
    assert error.value.status_code == 404


def test_metadata_exact_route_and_transport():
    table = {
        "gpt-4o": {"litellm_provider": "openai", "mode": "chat", "supports_vision": True},
        "generator": {"litellm_provider": "openai", "mode": "image_generation"},
        "gemini/generator": {"litellm_provider": "gemini", "mode": "image_generation"},
    }
    assert resolve_capability("openai", "", "openai/generator", table).status == "verified"
    assert resolve_capability("openai", "", "gpt-4o", table).status == "unsupported"
    assert resolve_capability("openai", "", "vendor/generator", table).status == "unknown"
    assert resolve_capability("openai", "https://custom.invalid", "generator", table).status == "unknown"
    assert resolve_capability("gemini", "", "gemini/generator", table).status == "unknown"
    assert resolve_capability("azure", "", "azure/generator", table).status == "unknown"
    assert resolve_capability("cloudflare", "", "cloudflare/generator", table).status == "unsupported"
    catalog = {
        "vendor/image": {
            "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
            "supported_parameters": ["modalities"],
        }
    }
    assert (
        resolve_capability("openrouter", "", "openrouter/vendor/image", table, catalog).status == "verified"
    )
    catalog["vendor/image"]["supported_parameters"] = []
    assert resolve_capability("openrouter", "", "openrouter/vendor/image", table, catalog).status == "unknown"


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"architecture": None},
        {"architecture": []},
        {"architecture": {}},
        {"architecture": {"output_modalities": None}},
        {"architecture": {"output_modalities": "text"}},
        {"architecture": {"output_modalities": ("text",)}},
        {"architecture": {"output_modalities": [None]}},
        {"architecture": {"output_modalities": ["image"], "input_modalities": None}},
        {"architecture": {"output_modalities": ["image"], "input_modalities": "text"}},
        {
            "architecture": {"output_modalities": ["image"], "input_modalities": ["text"]},
            "supported_parameters": "modalities",
        },
    ],
)
def test_openrouter_incomplete_metadata_is_unknown(entry):
    entry = {"supported_parameters": ["modalities"], **entry}
    if isinstance(entry.get("architecture"), dict):
        entry["architecture"] = {"input_modalities": ["text"], **entry["architecture"]}
    capability = resolve_capability("openrouter", "", "vendor/image", {}, {"vendor/image": entry})
    assert capability.status == "unknown"
    assert capability.reason == "metadata_unavailable"
    assert capability.request_modalities == ()


@pytest.mark.parametrize("mode", [None, "", [], {}, 1])
def test_bundled_missing_or_malformed_mode_is_unknown(mode):
    entry = {"litellm_provider": "openai"}
    if mode is not None:
        entry["mode"] = mode
    capability = resolve_capability("openai", "", "generator", {"generator": entry})
    assert capability.status == "unknown"
    assert capability.reason == "metadata_unavailable"
    entry["mode"] = None
    assert resolve_capability("openai", "", "generator", {"generator": entry}).status == "unknown"


@pytest.mark.parametrize(
    "field", ["supported_endpoints", "supported_input_modalities", "supported_output_modalities"]
)
@pytest.mark.parametrize("value", [None, "image", ("image",), [None]])
def test_bundled_chat_transport_rejects_malformed_arrays(field, value):
    entry = {
        "litellm_provider": "gemini",
        "mode": "image_generation",
        "supported_endpoints": ["/v1/chat/completions"],
        "supported_input_modalities": ["text"],
        "supported_output_modalities": ["image", "text"],
    }
    entry[field] = value
    capability = resolve_capability("gemini", "", "generator", {"generator": entry})
    assert capability.status == "unknown"
    assert capability.reason == "metadata_unavailable"


def test_bundled_chat_transport_preserves_verified_output_modalities():
    entry = {
        "litellm_provider": "gemini",
        "mode": "image_generation",
        "supported_endpoints": ["/v1/chat/completions"],
        "supported_input_modalities": ["text", "image"],
        "supported_output_modalities": ["text", "image"],
    }
    capability = resolve_capability("gemini", "", "generator", {"generator": entry})
    assert capability.status == "verified"
    assert capability.generation_mode == "chat"
    assert capability.request_modalities == ("image", "text")


@pytest.mark.parametrize("inputs,reason", [(["text"], "no_image_output"), (["text", "image"], "vision_only")])
def test_explicit_non_image_metadata_remains_unsupported(inputs, reason):
    entry = {
        "architecture": {"input_modalities": inputs, "output_modalities": ["text"]},
        "supported_parameters": [],
    }
    capability = resolve_capability("openrouter", "", "vendor/text", {}, {"vendor/text": entry})
    assert capability.status == "unsupported"
    assert capability.reason == reason
    capability = resolve_capability(
        "openai", "", "generator", {"generator": {"litellm_provider": "openai", "mode": "chat"}}
    )
    assert capability.status == "unsupported"
    assert capability.reason == "no_image_output"


@pytest.mark.parametrize("owner", [None, "u1"])
@pytest.mark.parametrize("prefix", ["openai", "openrouter"])
async def test_incomplete_metadata_can_save_disabled_draft_but_not_enable(
    db_factory, monkeypatch, owner, prefix
):
    import claw.providers.image_capability as capabilities

    monkeypatch.setattr(
        capabilities, "bundled_metadata", lambda: {"generator": {"litellm_provider": "openai"}}
    )

    async def catalog():
        return {"generator": {}}

    monkeypatch.setattr(capabilities, "openrouter_catalog", catalog)
    store = LLMConfigStore(db_factory)
    state = SimpleNamespace(llm_config=store)
    provider = await store.create_provider("draft", "", "", model_prefix=prefix, owner_id=owner)
    capability = await capabilities.image_capability(prefix, "", "generator")
    assert capability.status == "unknown"
    assert capability.reason == "metadata_unavailable"
    draft = await create_model(
        state, provider.id, ModelBody(model_id="generator", kind="image", enabled=False), owner
    )
    assert draft["enabled"] is False
    with pytest.raises(HTTPException) as error:
        await update_model(state, draft["id"], ModelPatch(enabled=True), owner)
    assert error.value.status_code == 422
    assert "ยังยืนยันไม่ได้ว่าโมเดลนี้สร้างภาพได้" in error.value.detail
    assert (await store.list_models(owner))[0].enabled is False


async def test_runtime_blocks_legacy_vision_model_before_paid_call(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = (
            await c.post("/api/auth/register", json={"email": "legacy@x.io", "password": "password123"})
        ).json()
        uid = reg["user"]["id"]
        state = app.state.claw
        provider = await state.llm_config.create_provider("openai", "", "", model_prefix="openai")
        await state.llm_config.create_model(provider.id, "openai/gpt-4o", "", kind="image")
        session = await state.sessions.create(uid, "legacy")

        async def paid(*args, **kwargs):
            pytest.fail("invalid legacy model reached paid generation")

        state.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=paid))
        response = await c.post(
            f"/api/sessions/{session.id}/images",
            headers={"Authorization": f"Bearer {reg['access_token']}"},
            json={"model": "openai/gpt-4o", "prompt": "draw a tree"},
        )
        assert response.status_code == 422
        assert (await state.llm_config.list_models())[0].enabled
        if state.usage is not None:
            assert (await state.usage.usage_today(uid))["images"] == 0


async def test_gpt_image_request_mapping_and_explicit_credentials(monkeypatch):
    import litellm

    captured = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(data=[{"b64_json": "aGVsbG8="}])

    monkeypatch.setattr(litellm, "aimage_generation", generate)
    provider = LiteLLMProvider(api_key="operator-key", api_base="https://custom.invalid")
    await provider.generate_image("tree", "openai/gpt-image-1", mode="images_endpoint")
    assert captured["api_key"] == "operator-key"
    assert captured["api_base"] == "https://custom.invalid"
    captured.clear()
    images = await provider.generate_image(
        "tree",
        "openai/gpt-image-1",
        api_key="saved-key",
        mode="images_endpoint",
        explicit_credentials=True,
        size="1024x1024",
    )
    assert images == [(b"hello", "png")]
    assert captured["api_key"] == "saved-key"
    assert "api_base" not in captured and "response_format" not in captured
    assert captured["size"] == "1024x1024" and captured["n"] == 1
    from litellm.utils import ProviderConfigManager
    from litellm.types.utils import LlmProviders

    adapter = ProviderConfigManager.get_provider_image_generation_config("gpt-image-1", LlmProviders.OPENAI)
    assert adapter.map_openai_params({"n": 1, "size": "1024x1024"}, {}, "gpt-image-1", False) == {
        "n": 1,
        "size": "1024x1024",
    }


@pytest.mark.parametrize("base", ["/api/admin", "/api/my"])
async def test_http_create_patch_and_provider_route_validation(db_factory, base):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = (
            await c.post("/api/auth/register", json={"email": "http@x.io", "password": "password123"})
        ).json()
        headers = {"Authorization": f"Bearer {reg['access_token']}"}
        provider = (
            await c.post(
                f"{base}/providers", headers=headers, json={"name": "native", "model_prefix": "openai"}
            )
        ).json()
        endpoint = f"{base}/providers/{provider['id']}/models"
        bad = await c.post(endpoint, headers=headers, json={"model_id": "openai/gpt-4o", "kind": "image"})
        assert bad.status_code == 422
        bad_disabled = await c.post(
            endpoint, headers=headers, json={"model_id": "openai/gpt-4o", "kind": "image", "enabled": False}
        )
        assert bad_disabled.status_code == 422
        draft = await c.post(
            endpoint, headers=headers, json={"model_id": "new-image", "kind": "image", "enabled": False}
        )
        assert draft.status_code == 200
        failed_enable = await c.patch(
            f"{base}/models/{draft.json()['id']}", headers=headers, json={"enabled": True}
        )
        assert failed_enable.status_code == 422
        assert "ยังยืนยันไม่ได้ว่าโมเดลนี้สร้างภาพได้" in failed_enable.json()["detail"]
        good = await c.post(endpoint, headers=headers, json={"model_id": "openai/dall-e-3", "kind": "image"})
        assert good.status_code == 200
        changed = await c.patch(
            f"{base}/providers/{provider['id']}",
            headers=headers,
            json={"model_prefix": "openrouter", "api_base": "https://custom.invalid"},
        )
        assert changed.status_code == 422
        reclassified = await c.post(
            endpoint, headers=headers, json={"model_id": "openai/gpt-4o", "kind": "chat"}
        )
        changed_kind = await c.patch(
            f"{base}/models/{reclassified.json()['id']}", headers=headers, json={"kind": "image"}
        )
        assert changed_kind.status_code == 422


@pytest.mark.parametrize("owner", [None, "u1"])
@pytest.mark.parametrize(
    "prefix,native,model",
    [
        ("openai", "https://api.openai.com/v1", "dall-e-3"),
        ("openrouter", "https://openrouter.ai/api/v1", "vendor/image"),
    ],
)
@pytest.mark.parametrize("suffix", [None, "", "/"])
async def test_native_routes_and_custom_drafts(db_factory, monkeypatch, owner, prefix, native, model, suffix):
    import claw.providers.image_capability as capabilities

    async def catalog():
        return {
            "vendor/image": {
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                "supported_parameters": ["modalities"],
            },
            "vendor/vision": {
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                "supported_parameters": [],
            },
        }

    monkeypatch.setattr(capabilities, "openrouter_catalog", catalog)
    store = LLMConfigStore(db_factory)
    state = SimpleNamespace(llm_config=store)
    api_base = "" if suffix is None else native + suffix
    provider = await store.create_provider(
        "native", api_key="", api_base=api_base, model_prefix=prefix, owner_id=owner
    )
    good = await create_model(state, provider.id, ModelBody(model_id=model, kind="image"), owner)
    assert good["enabled"]
    capability = await capabilities.image_capability(prefix, api_base, model)
    assert capability.status == "verified" and capability.api_base == api_base
    vision = "gpt-4o" if prefix == "openai" else "vendor/vision"
    with pytest.raises(HTTPException) as error:
        await create_model(state, provider.id, ModelBody(model_id=vision, kind="image", enabled=False), owner)
    assert error.value.detail == "โมเดลนี้รองรับการวิเคราะห์ภาพ แต่ไม่รองรับการสร้างภาพ"
    custom = await store.create_provider(
        "custom", api_key="", api_base=native + "/proxy", model_prefix=prefix, owner_id=owner
    )
    with pytest.raises(HTTPException):
        await create_model(state, custom.id, ModelBody(model_id=model, kind="image"), owner)
    assert not (
        await create_model(state, custom.id, ModelBody(model_id=model, kind="image", enabled=False), owner)
    )["enabled"]


@pytest.mark.parametrize(
    "base",
    [
        "http://api.openai.com/v1",
        "https://api.openai.com:443/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v1?x=1",
        "https://api.openai.com/v1#x",
        "https://api.openai.com/v1/proxy",
        "https://api.openai.com.evil/v1",
    ],
)
def test_only_exact_native_endpoint_is_trusted(base):
    table = {"generator": {"litellm_provider": "openai", "mode": "image_generation"}}
    assert resolve_capability("openai", base, "generator", table).reason == "custom_endpoint"


@pytest.mark.parametrize("owner", [None, "u1"])
async def test_legacy_disable_does_not_allow_route_changes_or_activation(db_factory, owner):
    store = LLMConfigStore(db_factory)
    state = SimpleNamespace(llm_config=store)
    provider = await store.create_provider("legacy", "", "", model_prefix="openai", owner_id=owner)
    legacy = await store.create_model(provider.id, "gpt-4o", "legacy", kind="image", owner_id=owner)
    assert not (await update_model(state, legacy.id, ModelPatch(enabled=False), owner))["enabled"]
    assert not (
        await update_model(
            state,
            legacy.id,
            ModelPatch(model_id="gpt-4o", kind="image", label="edited", enabled=False),
            owner,
        )
    )["enabled"]
    with pytest.raises(HTTPException):
        await update_model(state, legacy.id, ModelPatch(enabled=True), owner)
    with pytest.raises(HTTPException):
        await update_model(state, legacy.id, ModelPatch(model_id="gpt-4o-mini", enabled=False), owner)
    chat = await store.create_model(provider.id, "gpt-4o", "chat", owner_id=owner)
    with pytest.raises(HTTPException):
        await update_model(state, chat.id, ModelPatch(kind="image", enabled=False), owner)
    await store.create_model(provider.id, "gpt-4o", "bad enabled child", kind="image", owner_id=owner)
    assert not (await update_provider(state, provider.id, ProviderPatch(enabled=False), owner))["enabled"]
    with pytest.raises(HTTPException):
        await update_provider(state, provider.id, ProviderPatch(enabled=True), owner)
    with pytest.raises(HTTPException):
        await update_provider(
            state, provider.id, ProviderPatch(enabled=False, api_base="https://custom.invalid"), owner
        )
    assert (await store.get_by_name("legacy", owner)).api_base == ""


@pytest.mark.parametrize("failure", [None, "oversize", "too_many", "http", "malformed"])
async def test_openrouter_metadata_transport_is_bounded_cached_and_read_only(monkeypatch, failure):
    import json
    import httpx
    import claw.providers.image_capability as capabilities

    requests = []
    payload = {
        "data": [
            {
                "id": "vendor/image",
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                "supported_parameters": ["modalities"],
            }
        ]
    }
    if failure == "too_many":
        payload["data"] = [{"id": str(i)} for i in range(20001)]
    body = json.dumps(payload).encode()
    if failure == "oversize":
        body = b"x" * 4_000_001
    elif failure == "malformed":
        body = b"invalid json"

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and str(request.url) == "https://openrouter.ai/api/v1/models"
        assert "authorization" not in request.headers
        return httpx.Response(503 if failure == "http" else 200, content=body)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        capabilities.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(capabilities, "_catalog", (0, None))
    first = await capabilities.openrouter_catalog()
    assert (first is not None) == (failure is None)
    assert await capabilities.openrouter_catalog() == first
    assert len(requests) == 1
    assert capabilities._catalog[0] > capabilities.time.monotonic()


async def test_openrouter_unconfirmed_chat_output_preserves_existing_quota(db_factory, monkeypatch):
    async def catalog():
        return {
            "vendor/unconfirmed": {
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                "supported_parameters": [],
            }
        }

    monkeypatch.setattr("claw.providers.image_capability.openrouter_catalog", catalog)
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = (
            await c.post("/api/auth/register", json={"email": "unconfirmed@x.io", "password": "password123"})
        ).json()
        uid = reg["user"]["id"]
        state = app.state.claw
        provider = await state.llm_config.create_provider(
            "router", api_key="", api_base="https://openrouter.ai/api/v1", model_prefix="openrouter"
        )
        await state.llm_config.create_model(provider.id, "vendor/unconfirmed", "", kind="image")
        session = await state.sessions.create(uid, "unconfirmed")

        async def paid(*args, **kwargs):
            pytest.fail("unconfirmed chat transport reached paid generation")

        state.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=paid))
        before = await state.usage.usage_today(uid) if state.usage else None
        response = await c.post(
            f"/api/sessions/{session.id}/images",
            headers={"Authorization": f"Bearer {reg['access_token']}"},
            json={"model": "vendor/unconfirmed", "prompt": "tree"},
        )
        assert response.status_code == 422
        if state.usage:
            assert await state.usage.usage_today(uid) == before


@pytest.mark.parametrize(
    "mode,model", [("images_endpoint", "openai/dall-e-3"), ("chat", "openrouter/vendor/image")]
)
@pytest.mark.parametrize("saved_key", [None, ""])
async def test_explicit_image_credentials_never_fall_back(monkeypatch, mode, model, saved_key):
    import litellm
    from claw.providers.base import ProviderError

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.setattr(litellm, "api_key", "global-key")

    async def dispatch(**kwargs):
        pytest.fail("missing saved credentials reached LiteLLM")

    monkeypatch.setattr(litellm, "acompletion", dispatch)
    monkeypatch.setattr(litellm, "aimage_generation", dispatch)
    provider = LiteLLMProvider(api_key="operator-key")
    with pytest.raises(ProviderError, match="saved provider API key"):
        await provider.generate_image("tree", model, api_key=saved_key, mode=mode, explicit_credentials=True)


@pytest.mark.parametrize("prefix,model", [("openai", "dall-e-3"), ("openrouter", "vendor/image")])
async def test_missing_saved_image_key_releases_runtime_quota(db_factory, monkeypatch, prefix, model):
    import litellm

    async def catalog():
        return {
            "vendor/image": {
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
                "supported_parameters": ["modalities"],
            }
        }

    monkeypatch.setattr("claw.providers.image_capability.openrouter_catalog", catalog)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.setattr(litellm, "api_key", "global-key")

    async def dispatch(**kwargs):
        pytest.fail("missing saved credentials reached LiteLLM")

    monkeypatch.setattr(litellm, "acompletion", dispatch)
    monkeypatch.setattr(litellm, "aimage_generation", dispatch)
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = (
            await c.post("/api/auth/register", json={"email": "missing-key@x.io", "password": "password123"})
        ).json()
        uid = reg["user"]["id"]
        state = app.state.claw
        provider = await state.llm_config.create_provider("image", "", "", model_prefix=prefix)
        await state.llm_config.create_model(provider.id, model, "", kind="image")
        session = await state.sessions.create(uid, "missing-key")
        state.runtime = SimpleNamespace(provider=LiteLLMProvider(api_key="operator-key"))
        before = await state.usage.usage_today(uid)
        response = await c.post(
            f"/api/sessions/{session.id}/images",
            headers={"Authorization": f"Bearer {reg['access_token']}"},
            json={"model": model, "prompt": "tree"},
        )
        assert response.status_code == 502
        assert await state.usage.usage_today(uid) == before
        assert await state.messages.recent(session.id) == []


@pytest.mark.parametrize(
    "outputs,expected", [(["image"], ["image"]), (["text", "image"], ["image", "text"]), (["text"], None)]
)
async def test_runtime_uses_only_verified_output_modalities(db_factory, monkeypatch, outputs, expected):
    import litellm

    async def catalog():
        return {
            "vendor/image": {
                "architecture": {"input_modalities": ["text"], "output_modalities": outputs},
                "supported_parameters": ["modalities"],
            }
        }

    monkeypatch.setattr("claw.providers.image_capability.openrouter_catalog", catalog)
    captured = []

    async def dispatch(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(images=[{"image_url": {"url": "data:image/png;base64,aGVsbG8="}}])
                )
            ]
        )

    monkeypatch.setattr(litellm, "acompletion", dispatch)
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = (
            await c.post("/api/auth/register", json={"email": "modalities@x.io", "password": "password123"})
        ).json()
        uid = reg["user"]["id"]
        state = app.state.claw
        provider = await state.llm_config.create_provider("image", "saved-key", "", model_prefix="openrouter")
        await state.llm_config.create_model(provider.id, "vendor/image", "", kind="image")
        session = await state.sessions.create(uid, "modalities")
        state.runtime = SimpleNamespace(
            provider=LiteLLMProvider(api_key="operator-key", api_base="https://custom.invalid")
        )
        response = await c.post(
            f"/api/sessions/{session.id}/images",
            headers={"Authorization": f"Bearer {reg['access_token']}"},
            json={"model": "vendor/image", "prompt": "tree"},
        )
        if expected is None:
            assert response.status_code == 422
            assert captured == []
            assert (await state.usage.usage_today(uid))["images"] == 0
        else:
            assert response.status_code == 200, response.text
            assert len(captured) == 1
            assert captured[0]["modalities"] == expected
            assert captured[0]["model"] == "openrouter/vendor/image"
            assert captured[0]["api_key"] == "saved-key"
            assert "api_base" not in captured[0]


def test_native_chat_requires_explicit_text_input_and_output_metadata():
    entry = {
        "litellm_provider": "gemini",
        "mode": "image_generation",
        "supported_endpoints": ["/v1/chat/completions"],
        "supported_output_modalities": ["image"],
    }
    assert resolve_capability("gemini", "", "generator", {"generator": entry}).status == "unknown"
    entry["supported_input_modalities"] = ["image"]
    assert resolve_capability("gemini", "", "generator", {"generator": entry}).status == "unknown"
    entry["supported_input_modalities"] = ["text"]
    capability = resolve_capability("gemini", "", "generator", {"generator": entry})
    assert capability.status == "verified"
    assert capability.request_modalities == ("image",)
