"""Policy plans: cost-ceiling model gating, plan resolution order, daily/
per-minute quotas, admin CRUD, and the runtime messages/day gate."""

from types import SimpleNamespace

from claw.core.plans import builtin_plan_seeds, cost_allowed
from claw.db.models import UserGroup
from claw.db.stores import LLMConfigStore, PolicyPlanStore, UsageStore, UserStore, GroupStore
from tests.conftest import FakeProvider, text_turn
from tests.conftest_app import build_api_app, client
from tests.test_runtime import make_runtime


# ---- pure predicate ----

def test_cost_allowed_ordering():
    assert cost_allowed("low", "low")
    assert not cost_allowed("low", "medium")
    assert cost_allowed("very_high", "high")
    assert cost_allowed(None, "very_high")  # no ceiling = unlimited


# ---- plan resolution order (user -> group -> default -> none) ----

async def _seed_plans(factory) -> PolicyPlanStore:
    plans = PolicyPlanStore(factory)
    await plans.seed(builtin_plan_seeds())
    return plans


async def test_resolution_order(db_factory):
    plans = await _seed_plans(db_factory)
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    lst = await plans.list()
    by_name = {p["name"]: p for p in lst}

    u = await users.create(email="a@x.io")
    assert (await plans.resolve_for_user(u.id))["name"] == "Free"  # default

    await users.assign_plan(u.id, by_name["Pro"]["id"])
    assert (await plans.resolve_for_user(u.id))["name"] == "Pro"  # per-user wins

    g = await groups.create("team")
    async with db_factory() as db:
        gg = await db.get(UserGroup, g.id)
        gg.plan_id = by_name["Plus"]["id"]
        await db.commit()
    u2 = await users.create(email="b@x.io", group_id=g.id)
    assert (await plans.resolve_for_user(u2.id))["name"] == "Plus"  # group plan

    await plans.delete(by_name["Pro"]["id"])
    assert (await plans.resolve_for_user(u.id))["name"] == "Free"  # falls back after delete


async def test_single_default_invariant(db_factory):
    plans = await _seed_plans(db_factory)
    lst = await plans.list()
    plus = next(p for p in lst if p["name"] == "Plus")
    await plans.set_default(plus["id"])
    defaults = [p for p in await plans.list() if p["is_default"]]
    assert len(defaults) == 1 and defaults[0]["name"] == "Plus"


# ---- cost gating in the model store ----

async def _add_chat_model(store, model_id, cost, owner_id=None):
    pname = f"prov-{owner_id or 'global'}-{model_id.replace('/', '-')}"
    p = await store.create_provider(
        pname, "sk-test", "", True, "openrouter", owner_id=owner_id
    )
    return await store.create_model(
        p.id, model_id, model_id, True, cost, "", kind="chat", owner_id=owner_id
    )


async def test_enabled_models_cost_ceiling(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_chat_model(store, "vendor/cheap", "low")
    await _add_chat_model(store, "vendor/pricey", "very_high")
    ids = {m["model_id"] for m in await store.enabled_models(max_cost="low")}
    assert ids == {"vendor/cheap"}  # pricey filtered out
    ids_all = {m["model_id"] for m in await store.enabled_models(max_cost=None)}
    assert ids_all == {"vendor/cheap", "vendor/pricey"}


async def test_enabled_models_byok_exempt_from_ceiling(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_chat_model(store, "vendor/pricey", "very_high")  # global
    await _add_chat_model(store, "my/pricey", "very_high", owner_id="user-1")  # BYOK
    ids = {m["model_id"] for m in await store.enabled_models(user_id="user-1", max_cost="low")}
    assert ids == {"my/pricey"}  # own BYOK kept, global pricey dropped


async def test_resolve_denies_over_ceiling_global(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_chat_model(store, "vendor/pricey", "very_high")
    assert await store.resolve("vendor/pricey", max_cost="low") is None  # denied
    assert await store.resolve("vendor/pricey", max_cost="very_high") is not None


async def test_default_model_for_picks_allowed(db_factory):
    store = LLMConfigStore(db_factory)
    await _add_chat_model(store, "vendor/cheap", "low")
    m = await _add_chat_model(store, "vendor/pricey", "very_high")
    await store.update_model(m.id, owner_id=None, is_default=True)  # pricey is default
    # A low-tier plan can't use the pricey default → best allowed (cheap) instead.
    assert await store.default_model_for("low") == "vendor/cheap"
    assert await store.default_model_for(None) == "vendor/pricey"


# ---- usage counters ----

async def test_record_image_and_usage_today(db_factory):
    usage = UsageStore(db_factory, is_postgres=False)
    await usage.record_image("u1", "vendor/pixel")
    await usage.record_image("u1", "vendor/pixel")
    await usage.record("u1", None, "vendor/chat", {"prompt_tokens": 5, "completion_tokens": 3})
    today = await usage.usage_today("u1")
    assert today["images"] == 2 and today["turns"] == 1


# ---- admin CRUD API ----

async def _register(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_admin_plan_crud(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")  # first user = admin
        user_token, _ = await _register(c, "u@x.io")
        # non-admin is rejected
        assert (await c.get("/api/admin/plans", headers=_bearer(user_token))).status_code == 403
        # create
        r = await c.post(
            "/api/admin/plans",
            json={"name": "Basic", "max_chat_cost": "low", "messages_per_day": 10},
            headers=_bearer(admin_token),
        )
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # invalid cost -> 422
        bad = await c.post(
            "/api/admin/plans",
            json={"name": "Bad", "max_chat_cost": "ultra"},
            headers=_bearer(admin_token),
        )
        assert bad.status_code == 422
        # patch + default
        await c.patch(f"/api/admin/plans/{pid}", json={"messages_per_day": 25}, headers=_bearer(admin_token))
        await c.put("/api/admin/plans/default", json={"plan_id": pid}, headers=_bearer(admin_token))
        listed = (await c.get("/api/admin/plans", headers=_bearer(admin_token))).json()
        basic = next(p for p in listed if p["id"] == pid)
        assert basic["messages_per_day"] == 25 and basic["is_default"] is True
        # delete
        assert (await c.delete(f"/api/admin/plans/{pid}", headers=_bearer(admin_token))).status_code == 200


async def test_cannot_unset_sole_default_plan(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        first = (
            await c.post(
                "/api/admin/plans", json={"name": "First", "is_default": True}, headers=_bearer(admin_token)
            )
        ).json()
        default_id = first["id"]
        # Unsetting the only default plan is rejected...
        r = await c.patch(
            f"/api/admin/plans/{default_id}", json={"is_default": False}, headers=_bearer(admin_token)
        )
        assert r.status_code == 400
        # ...but making a different plan default first, then unsetting the old
        # one, works fine (there's always a replacement).
        other = (
            await c.post("/api/admin/plans", json={"name": "Other"}, headers=_bearer(admin_token))
        ).json()
        await c.put("/api/admin/plans/default", json={"plan_id": other["id"]}, headers=_bearer(admin_token))
        r2 = await c.patch(
            f"/api/admin/plans/{default_id}", json={"is_default": False}, headers=_bearer(admin_token)
        )
        assert r2.status_code == 200


async def test_resolve_for_users_batched(db_factory):
    plans = await _seed_plans(db_factory)
    users = UserStore(db_factory)
    lst = await plans.list()
    pro = next(p for p in lst if p["name"] == "Pro")

    u1 = await users.create(email="a@x.io")
    await users.assign_plan(u1.id, pro["id"])
    u2 = await users.create(email="b@x.io")  # falls to default (Free)

    resolved = await plans.resolve_for_users([u1.id, u2.id, "nonexistent"])
    assert resolved[u1.id]["name"] == "Pro"
    assert resolved[u2.id]["name"] == "Free"
    assert resolved["nonexistent"]["name"] == "Free"  # unknown id falls to default, same as resolve_for_user
    assert resolved[u1.id] == await plans.resolve_for_user(u1.id)


async def test_assign_plan_and_models_filter(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        # global models at two tiers
        store = app.state.claw.llm_config
        await _add_chat_model(store, "vendor/cheap", "low")
        await _add_chat_model(store, "vendor/pricey", "very_high")
        # a low-ceiling plan, assigned to the user
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "Lite", "max_chat_cost": "low", "allow_image": False},
            headers=_bearer(admin_token),
        )
        pid = pr.json()["id"]
        await c.patch(f"/api/admin/users/{user['id']}", json={"plan_id": pid}, headers=_bearer(admin_token))
        # chat picker hides the pricey model
        models = (await c.get("/api/models", headers=_bearer(user_token))).json()["models"]
        assert {m["model_id"] for m in models} == {"vendor/cheap"}
        # my/plan reflects the assignment
        mine = (await c.get("/api/my/plan", headers=_bearer(user_token))).json()
        assert mine["plan"]["name"] == "Lite"


async def test_models_endpoint_hides_env_default_without_credentials(db_factory):
    # No Control Plane provider configured, and the env-configured model has
    # no usable credentials (api_key/api_base both empty) — the picker must
    # NOT advertise it, since the runtime (claw/core/runtime.py) would reject
    # any turn sent against it with error.no_model_configured.
    from claw.config import LLMSettings

    app = build_api_app(db_factory, llm=LLMSettings(model="anthropic/claude-sonnet-4-5"))
    async with client(app) as c:
        _admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "u@x.io")
        r = (await c.get("/api/models", headers=_bearer(user_token))).json()
        assert r["models"] == []
        assert r["default"] is None


async def test_models_endpoint_shows_env_default_with_credentials(db_factory):
    # Same as above but the env default DOES have a usable api_key — the
    # picker must still fall back to it out of the box.
    from claw.config import LLMSettings

    app = build_api_app(
        db_factory, llm=LLMSettings(model="anthropic/claude-sonnet-4-5", api_key="sk-env")
    )
    async with client(app) as c:
        _admin_token, _ = await _register(c, "admin@x.io")
        user_token, _ = await _register(c, "u@x.io")
        r = (await c.get("/api/models", headers=_bearer(user_token))).json()
        assert [m["model_id"] for m in r["models"]] == ["anthropic/claude-sonnet-4-5"]
        assert r["default"] == "anthropic/claude-sonnet-4-5"


async def test_image_plan_gates(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        # image model + a plan that forbids images
        store = app.state.claw.llm_config
        p = await store.create_provider("imgp", "sk", "", True, "openrouter", owner_id=None)
        await store.create_model(p.id, "vendor/pixel", "pixel", True, "low", "", kind="image", owner_id=None)
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "NoImg", "allow_image": False},
            headers=_bearer(admin_token),
        )
        await c.patch(
            f"/api/admin/users/{user['id']}", json={"plan_id": pr.json()["id"]}, headers=_bearer(admin_token)
        )
        # image picker is empty
        assert (await c.get("/api/image-models", headers=_bearer(user_token))).json()["models"] == []
        # generation is 403
        sess = await app.state.claw.sessions.create(user["id"], "s")
        r = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/pixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 403


async def test_image_plan_gate_exempts_byok(db_factory):
    # A plan that forbids image generation must not block the user's own
    # (BYOK) image model — only admin-global models are gated.
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        store = app.state.claw.llm_config
        # Global image model — should stay hidden/blocked.
        gp = await store.create_provider("imgp", "sk", "", True, "openrouter", owner_id=None)
        await store.create_model(gp.id, "vendor/pixel", "pixel", True, "low", "", kind="image", owner_id=None)
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "NoImg", "allow_image": False},
            headers=_bearer(admin_token),
        )
        await c.patch(
            f"/api/admin/users/{user['id']}", json={"plan_id": pr.json()["id"]}, headers=_bearer(admin_token)
        )
        # User's own BYOK image model (owner_id=user.id).
        up = await store.create_provider("myimg", "sk", "", True, "openrouter", owner_id=user["id"])
        await store.create_model(up.id, "vendor/mypixel", "my pixel", True, "very_high", "", kind="image", owner_id=user["id"])

        models = (await c.get("/api/image-models", headers=_bearer(user_token))).json()["models"]
        assert [m["model_id"] for m in models] == ["vendor/mypixel"]

        async def fake_generate_image(prompt, model, **kwargs):
            return [(b"\x89PNG", "png")]

        app.state.claw.runtime = SimpleNamespace(
            provider=SimpleNamespace(generate_image=fake_generate_image)
        )
        sess = await app.state.claw.sessions.create(user["id"], "s")
        r = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/mypixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 200
        # Global model is still blocked by the plan.
        r2 = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/pixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r2.status_code == 403


async def test_images_per_day_quota_exempts_byok(db_factory):
    # A low images_per_day cap on the plan must not throttle the user's own
    # BYOK image model — only admin-global usage counts against it.
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        store = app.state.claw.llm_config
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "TinyImg", "allow_image": True, "images_per_day": 1},
            headers=_bearer(admin_token),
        )
        await c.patch(
            f"/api/admin/users/{user['id']}", json={"plan_id": pr.json()["id"]}, headers=_bearer(admin_token)
        )
        up = await store.create_provider("myimg", "sk", "", True, "openrouter", owner_id=user["id"])
        await store.create_model(up.id, "vendor/mypixel", "my pixel", True, "very_high", "", kind="image", owner_id=user["id"])
        # Pre-consume the plan's single daily slot via a global-scoped record.
        await app.state.claw.usage.record_image(user["id"], "vendor/other")

        async def fake_generate_image(prompt, model, **kwargs):
            return [(b"\x89PNG", "png")]

        app.state.claw.runtime = SimpleNamespace(
            provider=SimpleNamespace(generate_image=fake_generate_image)
        )
        sess = await app.state.claw.sessions.create(user["id"], "s")
        r = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/mypixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 200  # BYOK generation goes through despite the plan cap already used


async def test_images_per_day_quota_atomic_no_overshoot(db_factory):
    # Reserve-then-verify must not overshoot even if the counter is already AT
    # the limit when the request arrives (simulates a concurrent winner).
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        store = app.state.claw.llm_config
        p = await store.create_provider("imgp", "sk", "", True, "openrouter", owner_id=None)
        await store.create_model(p.id, "vendor/pixel", "pixel", True, "low", "", kind="image", owner_id=None)
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "OneImg", "allow_image": True, "images_per_day": 1},
            headers=_bearer(admin_token),
        )
        await c.patch(
            f"/api/admin/users/{user['id']}", json={"plan_id": pr.json()["id"]}, headers=_bearer(admin_token)
        )
        await app.state.claw.usage.record_image(user["id"], "vendor/pixel")  # at limit

        async def fake_generate_image(prompt, model, **kwargs):
            return [(b"\x89PNG", "png")]

        app.state.claw.runtime = SimpleNamespace(
            provider=SimpleNamespace(generate_image=fake_generate_image)
        )
        sess = await app.state.claw.sessions.create(user["id"], "s")
        r = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/pixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 429
        # The rejected attempt must have released its reservation → count stays 1.
        assert (await app.state.claw.usage.usage_today(user["id"]))["images"] == 1


async def test_images_per_day_quota(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        user_token, user = await _register(c, "u@x.io")
        store = app.state.claw.llm_config
        p = await store.create_provider("imgp", "sk", "", True, "openrouter", owner_id=None)
        await store.create_model(p.id, "vendor/pixel", "pixel", True, "low", "", kind="image", owner_id=None)
        pr = await c.post(
            "/api/admin/plans",
            json={"name": "OneImg", "allow_image": True, "images_per_day": 1},
            headers=_bearer(admin_token),
        )
        await c.patch(
            f"/api/admin/users/{user['id']}", json={"plan_id": pr.json()["id"]}, headers=_bearer(admin_token)
        )
        # pre-consume today's single image
        await app.state.claw.usage.record_image(user["id"], "vendor/pixel")

        async def fake_generate_image(prompt, model, **kwargs):
            return [(b"\x89PNG", "png")]

        app.state.claw.runtime = SimpleNamespace(
            provider=SimpleNamespace(generate_image=fake_generate_image)
        )
        sess = await app.state.claw.sessions.create(user["id"], "s")
        r = await c.post(
            f"/api/sessions/{sess.id}/images",
            json={"model": "vendor/pixel", "prompt": "hi"},
            headers=_bearer(user_token),
        )
        assert r.status_code == 429  # daily image limit reached


async def test_group_patch_noop_does_not_404(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_token, _ = await _register(c, "admin@x.io")
        g = (await c.post("/api/admin/groups", json={"name": "team"}, headers=_bearer(admin_token))).json()
        # PATCH with no plan_id (empty body) is a no-op, not a 404.
        r = await c.patch(f"/api/admin/groups/{g['id']}", json={}, headers=_bearer(admin_token))
        assert r.status_code == 200 and r.json()["id"] == g["id"]
        # A genuinely missing group still 404s.
        assert (await c.patch("/api/admin/groups/nope", json={}, headers=_bearer(admin_token))).status_code == 404


async def test_plan_turns_per_minute_cannot_exceed_global(stores, tmp_path, db_factory):
    # Plan rpm=1000 must be clamped to the global backstop (make global tiny).
    from claw.config import SandboxSettings, Settings
    from claw.core.bus import EventBus
    from claw.core.memory import MemoryService
    from claw.core.runtime import AgentRuntime

    plans = await _seed_plans(db_factory)
    default = await plans.default_plan()
    await plans.update(default["id"], turns_per_minute=1000)  # very high plan cap
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "ws",
        sandbox=SandboxSettings(enabled=False),
        turns_per_minute=1,  # tiny global backstop
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], FakeProvider([]))
    runtime = AgentRuntime(
        settings=settings, provider=FakeProvider([text_turn("a"), text_turn("b")]),
        bus=EventBus(), users=stores["users"], sessions=stores["sessions"],
        messages=stores["messages"], memory=memory, audit=stores["audit"], plans=plans,
    )
    user = await stores["users"].get_or_create_by_email("u@x.io")
    session = await stores["sessions"].create(user.id)
    first = await runtime.handle_message(user.id, session.id, "one")
    assert first == "a"  # first within the global cap of 1/min
    second = await runtime.handle_message(user.id, session.id, "two")
    # Clamped to global=1 despite plan=1000, so the 2nd is rate-limited.
    assert "too fast" in second.lower() or "ถี่" in second


# ---- runtime messages/day gate (agent loop) ----

async def test_no_allowed_model_rejects_turn_instead_of_bypassing_ceiling(db_factory, stores, tmp_path):
    # Plan restricts to "low" cost, but the only enabled global chat model is
    # "high" — resolve()/default_model_for() both return None. The turn must
    # be rejected, NOT silently fall back to the provider's raw default model.
    plans = await _seed_plans(db_factory)
    default = await plans.default_plan()
    await plans.update(default["id"], max_chat_cost="low")
    llm_config = LLMConfigStore(db_factory)
    p = await llm_config.create_provider("prov", "sk", "", True, "openrouter", owner_id=None)
    await llm_config.create_model(p.id, "vendor/pricey", "pricey", True, "high", "", kind="chat", owner_id=None)

    provider = FakeProvider([text_turn("should not be reached")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.plans = plans
    runtime.llm_config = llm_config

    user = await stores["users"].get_or_create_by_email("u@x.io")
    session = await stores["sessions"].create(user.id)
    out = await runtime.handle_message(user.id, session.id, "hi")
    assert "plan" in out.lower() or "แพ็กเกจ" in out
    assert provider.calls == []  # provider never invoked — turn rejected before any model call


async def test_env_default_model_usable_on_default_plan_when_no_control_plane_model(
    db_factory, stores, tmp_path
):
    # A fresh install with NO admin-global model configured (chat runs entirely
    # off the operator's CLAW_LLM__MODEL env default). The default plan caps at
    # "low", but there is no lineup to gate — the env default is the operator's
    # deliberate baseline and MUST work, not be rejected as "plan disallows".
    plans = await _seed_plans(db_factory)
    default = await plans.default_plan()
    await plans.update(default["id"], max_chat_cost="low")
    llm_config = LLMConfigStore(db_factory)  # deliberately empty: no models at all

    provider = FakeProvider([text_turn("hi from env default")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.plans = plans
    runtime.llm_config = llm_config
    # Operator's env default is actually configured (has credentials), so the
    # fallback is genuinely usable.
    runtime.settings.llm.api_key = "sk-env"

    user = await stores["users"].get_or_create_by_email("u@x.io")
    session = await stores["sessions"].create(user.id)
    out = await runtime.handle_message(user.id, session.id, "hi")
    assert out == "hi from env default"  # turn ran on the env default, not rejected
    assert provider.calls  # provider WAS invoked


async def test_no_control_plane_model_and_no_env_config_gives_clear_setup_message(
    db_factory, stores, tmp_path
):
    # No admin-global model AND the env default has no usable credentials
    # (api_key and api_base both empty). Instead of letting the loop hit a raw
    # provider auth error, surface an admin-facing "configure a model" message.
    plans = await _seed_plans(db_factory)
    default = await plans.default_plan()
    await plans.update(default["id"], max_chat_cost="low")
    llm_config = LLMConfigStore(db_factory)  # empty

    provider = FakeProvider([text_turn("should not be reached")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.plans = plans
    runtime.llm_config = llm_config
    # make_runtime's default Settings leaves llm.api_key and llm.api_base empty.
    assert not runtime.settings.llm.api_key and not runtime.settings.llm.api_base

    user = await stores["users"].get_or_create_by_email("u@x.io")
    session = await stores["sessions"].create(user.id)
    out = await runtime.handle_message(user.id, session.id, "hi")
    assert "configured" in out.lower() or "ตั้งค่า" in out
    assert provider.calls == []  # no model to call — provider never invoked


async def test_messages_per_day_gate(db_factory, stores, tmp_path):
    plans = await _seed_plans(db_factory)
    # Retune the default plan to a 2/day cap for the test.
    default = await plans.default_plan()
    await plans.update(default["id"], messages_per_day=2)
    usage = UsageStore(db_factory, is_postgres=False)

    provider = FakeProvider([text_turn("hi"), text_turn("hi"), text_turn("hi")])
    runtime = make_runtime(stores, provider, tmp_path)
    runtime.plans = plans
    runtime.usage = usage

    user = await stores["users"].get_or_create_by_email("u@x.io")
    session = await stores["sessions"].create(user.id)
    # Pre-seed today's usage at the cap (2 turns) so the next turn is blocked.
    await usage.record(user.id, None, "m", {"prompt_tokens": 1, "completion_tokens": 1})
    await usage.record(user.id, None, "m", {"prompt_tokens": 1, "completion_tokens": 1})

    out = await runtime.handle_message(user.id, session.id, "third")
    assert "daily" in out.lower() or "โควตา" in out  # localized daily-limit message
