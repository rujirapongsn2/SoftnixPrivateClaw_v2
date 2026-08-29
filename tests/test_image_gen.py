"""Model-kind classification (chat / image / vision are separate lists and
separate resolvers), provider generate_image extraction (both paths), and the
one-shot /images endpoint that runs outside the agent loop."""

import base64
from types import SimpleNamespace

from claw.core.limits import RateLimiter
from claw.db.stores import LLMConfigStore
from claw.providers.litellm_provider import LiteLLMProvider
from tests.conftest_app import build_api_app, client

# 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def _add_model(store: LLMConfigStore, model_id: str, kind: str, prefix: str = "openrouter"):
    p = await store.create_provider("prov-" + model_id[-6:], "sk-test", "", True, prefix, owner_id=None)
    return await store.create_model(p.id, model_id, model_id, True, "medium", "", kind=kind, owner_id=None)


# ---- store: chat vs image are separate + resolve_image gates on kind ----


async def test_enabled_models_split_chat_vs_image(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_model(store, "vendor/chatty-1", "chat")
    await _add_model(store, "vendor/pixel-1", "image")

    chat = await store.enabled_models(kind="chat")
    image = await store.enabled_models(kind="image")
    assert [m["model_id"] for m in chat] == ["vendor/chatty-1"]
    assert [m["model_id"] for m in image] == ["vendor/pixel-1"]


async def test_distinct_chat_models_dedupes_and_skips_image_models(db_factory):
    # Startup reads this to report which models it can resolve a context window
    # for; the same model id offered by two providers must be reported once.
    store = LLMConfigStore(db_factory)
    await _add_model(store, "vendor/chatty-1", "chat")
    second = await store.create_provider("other-prov", "sk-test", "", True, "openai", owner_id=None)
    await store.create_model(second.id, "vendor/chatty-1", "", True, "medium", "", kind="chat", owner_id=None)
    await _add_model(store, "vendor/pixel-1", "image")

    assert await store.distinct_chat_models() == [("vendor/chatty-1", None)]


async def test_resolve_vision_ignores_chat_and_image_models(db_factory):
    # A chat or image model must never be pressed into service as the vision
    # reader just because one wasn't configured — the operator opts in by
    # classifying a model, and "none configured" has to stay distinguishable.
    store = LLMConfigStore(db_factory)
    await _add_model(store, "vendor/chatty-1", "chat")
    await _add_model(store, "vendor/pixel-1", "image")

    assert await store.resolve_vision("u1") is None


async def test_resolve_vision_prefers_the_callers_own_key_then_the_cheaper_tier(db_factory):
    store = LLMConfigStore(db_factory)
    pricey = await store.create_provider("g1", "sk-global", "", True, "", owner_id=None)
    await store.create_model(pricey.id, "global/eyes-hi", "", True, "high", "", kind="vision", owner_id=None)
    cheap = await store.create_provider("g2", "sk-global", "", True, "", owner_id=None)
    await store.create_model(cheap.id, "global/eyes-lo", "", True, "low", "", kind="vision", owner_id=None)

    # Only global models exist → the cheap one wins; describing an image is not
    # work worth the top tier by default.
    assert (await store.resolve_vision("u1"))["model_id"] == "global/eyes-lo"

    own = await store.create_provider("mine", "sk-mine", "", True, "", owner_id="u1")
    await store.create_model(own.id, "byok/eyes", "", True, "very_high", "", kind="vision", owner_id="u1")

    # The caller's own key wins outright, cost tier or not — they pay for it.
    picked = await store.resolve_vision("u1")
    assert picked["model_id"] == "byok/eyes" and picked["scope"] == "private"
    # ...and it is still their own key, never another user's.
    assert (await store.resolve_vision("u2"))["model_id"] == "global/eyes-lo"


async def test_resolve_vision_ignores_the_plan_chat_cost_ceiling(db_factory):
    store = LLMConfigStore(db_factory)
    prov = await store.create_provider("g1", "sk-global", "", True, "", owner_id=None)
    await store.create_model(prov.id, "global/eyes-hi", "", True, "high", "", kind="vision", owner_id=None)

    # max_chat_cost gates which model a user may *converse* with. The reader is
    # the operator's own, so the ceiling must not reach it: the seeded default
    # plan is "low", and applying it here silently disabled delegation for
    # exactly the users most likely to be stuck on a text-only chat model.
    assert (await store.resolve_vision("u1"))["model_id"] == "global/eyes-hi"


async def test_distinct_chat_models_reports_context_window_override(db_factory):
    store = LLMConfigStore(db_factory)
    prov = await store.create_provider("ctx-prov", "sk-test", "", True, "openai", owner_id=None)
    await store.create_model(
        prov.id,
        "vendor/private-1",
        "",
        True,
        "medium",
        "",
        kind="chat",
        owner_id=None,
        context_window=250_000,
    )

    assert await store.distinct_chat_models() == [("vendor/private-1", 250_000)]


async def test_context_window_override_round_trips_and_clears(db_factory):
    store = LLMConfigStore(db_factory)
    prov = await store.create_provider("ctx-prov", "sk-test", "", True, "openai", owner_id=None)
    row = await store.create_model(
        prov.id,
        "vendor/private-1",
        "",
        True,
        "medium",
        "",
        kind="chat",
        owner_id=None,
        context_window=250_000,
    )
    assert row is not None
    resolved = await store.resolve("vendor/private-1")
    assert resolved is not None and resolved["context_window"] == 250_000

    # 0 from the form means "go back to automatic lookup", stored as NULL.
    await store.update_model(row.id, None, context_window=0)
    resolved = await store.resolve("vendor/private-1")
    assert resolved is not None and resolved["context_window"] is None


async def test_resolve_image_only_matches_image_kind(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_model(store, "vendor/chatty-1", "chat", prefix="openrouter")
    await _add_model(store, "vendor/pixel-1", "image", prefix="openai")

    assert await store.resolve_image("vendor/chatty-1") is None  # chat model: not resolvable here
    got = await store.resolve_image("vendor/pixel-1")
    assert got is not None
    assert got["model_id"] == "vendor/pixel-1"
    assert got["model_prefix"] == "openai"  # drives images-endpoint vs chat strategy


# ---- provider: both extraction paths ----


async def test_generate_image_chat_path(monkeypatch):
    import litellm

    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode()

    async def fake_acompletion(**kwargs):
        assert "tools" not in kwargs  # never send tools to an image model
        msg = SimpleNamespace(images=[{"type": "image_url", "image_url": {"url": data_url}}])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider()
    out = await provider.generate_image("a red circle", "openrouter/vendor/pixel-1", mode="chat")
    assert len(out) == 1
    data, ext = out[0]
    assert data == _PNG and ext == "png"


async def test_generate_image_images_endpoint_path(monkeypatch):
    import litellm

    async def fake_aimage_generation(**kwargs):
        assert kwargs["response_format"] == "b64_json"
        item = SimpleNamespace(b64_json=base64.b64encode(_PNG).decode(), url=None)
        return SimpleNamespace(data=[item])

    monkeypatch.setattr(litellm, "aimage_generation", fake_aimage_generation)
    provider = LiteLLMProvider()
    out = await provider.generate_image("a red circle", "dall-e-3", mode="images_endpoint", size="1024x1024")
    assert out[0][0] == _PNG


async def test_generate_image_raises_when_no_image(monkeypatch):
    import litellm

    from claw.providers.base import ProviderError

    async def fake_acompletion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(images=[]))])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    provider = LiteLLMProvider()
    try:
        await provider.generate_image("x", "openrouter/vendor/pixel-1", mode="chat")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass


# ---- endpoint: /images (outside the agent loop) ----


async def test_images_endpoint_generates_and_persists(db_factory, monkeypatch):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "img@x.io", "password": "password123"})
        token = reg.json()["access_token"]
        uid = reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}

        await _add_model(app.state.claw.llm_config, "vendor/pixel-1", "image", prefix="openrouter")
        session = await app.state.claw.sessions.create(uid, "img chat")

        captured: dict = {}

        async def fake_generate_image(prompt, model, **kwargs):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["mode"] = kwargs.get("mode")
            return [(_PNG, "png")]

        app.state.claw.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=fake_generate_image))

        r = await c.post(
            f"/api/sessions/{session.id}/images",
            json={"model": "vendor/pixel-1", "prompt": "a red circle"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        path = r.json()["path"]
        assert path.startswith("uploads/generated-") and path.endswith(".png")
        assert captured["model"] == "vendor/pixel-1"
        assert captured["mode"] == "chat"  # openrouter prefix -> chat extraction

        # The generated image persists as an artifact message (survives reload).
        msgs = await c.get(f"/api/sessions/{session.id}/messages", headers=headers)
        assistant = [m for m in msgs.json() if m["role"] == "assistant"]
        assert any(path in (m.get("meta") or {}).get("artifacts", []) for m in assistant)


async def test_images_endpoint_rejects_chat_model(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "img2@x.io", "password": "password123"})
        token = reg.json()["access_token"]
        uid = reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        await _add_model(app.state.claw.llm_config, "vendor/chatty-1", "chat")
        session = await app.state.claw.sessions.create(uid, "c")
        r = await c.post(
            f"/api/sessions/{session.id}/images",
            json={"model": "vendor/chatty-1", "prompt": "hi"},
            headers=headers,
        )
        assert r.status_code == 404  # a chat model is not usable as an image model


async def test_images_endpoint_blocks_prompt_via_policy(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "pol@x.io", "password": "password123"})
        token, uid = reg.json()["access_token"], reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        await _add_model(app.state.claw.llm_config, "vendor/pixel-1", "image")
        session = await app.state.claw.sessions.create(uid, "c")

        called = {"gen": False}

        async def fake_generate_image(prompt, model, **kwargs):
            called["gen"] = True
            return [(_PNG, "png")]

        app.state.claw.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=fake_generate_image))
        # Policy that blocks everything.
        blocked = SimpleNamespace(matched_rules=["r"], action="block", blocked=True, message="nope", text="")
        app.state.claw.policy = SimpleNamespace(enforce=lambda text, scope: blocked)

        r = await c.post(
            f"/api/sessions/{session.id}/images",
            json={"model": "vendor/pixel-1", "prompt": "bad prompt"},
            headers=headers,
        )
        assert r.status_code == 400
        assert called["gen"] is False  # blocked BEFORE spending a provider call


async def test_images_endpoint_stores_masked_prompt(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "mask@x.io", "password": "password123"})
        token, uid = reg.json()["access_token"], reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        await _add_model(app.state.claw.llm_config, "vendor/pixel-1", "image")
        session = await app.state.claw.sessions.create(uid, "c")

        seen = {}

        async def fake_generate_image(prompt, model, **kwargs):
            seen["prompt"] = prompt
            return [(_PNG, "png")]

        app.state.claw.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=fake_generate_image))
        masked = SimpleNamespace(
            matched_rules=["r"], action="mask", blocked=False, message=None, text="a [REDACTED]"
        )
        app.state.claw.policy = SimpleNamespace(enforce=lambda text, scope: masked)

        r = await c.post(
            f"/api/sessions/{session.id}/images",
            json={"model": "vendor/pixel-1", "prompt": "a secret@example.com"},
            headers=headers,
        )
        assert r.status_code == 200
        assert seen["prompt"] == "a [REDACTED]"  # masked text sent to provider
        msgs = await c.get(f"/api/sessions/{session.id}/messages", headers=headers)
        users = [m for m in msgs.json() if m["role"] == "user"]
        assert users[-1]["content"] == "a [REDACTED]"  # raw prompt never persisted


async def test_images_endpoint_rate_limited(db_factory):
    app = build_api_app(db_factory)
    app.state.claw.image_rate_limiter = RateLimiter(1)  # 1/min
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "rl@x.io", "password": "password123"})
        token, uid = reg.json()["access_token"], reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        await _add_model(app.state.claw.llm_config, "vendor/pixel-1", "image")
        session = await app.state.claw.sessions.create(uid, "c")

        async def fake_generate_image(prompt, model, **kwargs):
            return [(_PNG, "png")]

        app.state.claw.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=fake_generate_image))
        body = {"model": "vendor/pixel-1", "prompt": "x"}
        r1 = await c.post(f"/api/sessions/{session.id}/images", json=body, headers=headers)
        r2 = await c.post(f"/api/sessions/{session.id}/images", json=body, headers=headers)
        assert r1.status_code == 200
        assert r2.status_code == 429  # second within the same minute is throttled


async def test_images_endpoint_rejects_oversized(db_factory):
    app = build_api_app(db_factory)
    app.state.claw.settings.image.max_bytes = 4  # tiny cap
    async with client(app) as c:
        reg = await c.post("/api/auth/register", json={"email": "big@x.io", "password": "password123"})
        token, uid = reg.json()["access_token"], reg.json()["user"]["id"]
        headers = {"Authorization": f"Bearer {token}"}
        await _add_model(app.state.claw.llm_config, "vendor/pixel-1", "image")
        session = await app.state.claw.sessions.create(uid, "c")

        async def fake_generate_image(prompt, model, **kwargs):
            return [(b"way-too-many-bytes", "png")]

        app.state.claw.runtime = SimpleNamespace(provider=SimpleNamespace(generate_image=fake_generate_image))
        r = await c.post(
            f"/api/sessions/{session.id}/images",
            json={"model": "vendor/pixel-1", "prompt": "x"},
            headers=headers,
        )
        assert r.status_code == 502  # oversized payload refused before write


async def test_reclassifying_default_chat_model_to_image_clears_default(db_factory):
    store = LLMConfigStore(db_factory)
    m = await _add_model(store, "vendor/chatty-1", "chat")
    await store.update_model(m.id, owner_id=None, is_default=True)
    assert await store.default_model() == "vendor/chatty-1"

    # Reclassify the current default to image — must drop is_default so the
    # deployment doesn't end up with a null chat default.
    await store.update_model(m.id, owner_id=None, kind="image")
    assert await store.default_model() is None
    refreshed = (await store.enabled_models(kind="image"))[0]
    assert refreshed["is_default"] is False
