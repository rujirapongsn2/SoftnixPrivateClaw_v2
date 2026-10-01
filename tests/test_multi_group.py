"""A user can belong to several groups; access is the union of what those groups allow."""

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from claw.db.models import UserGroup
from claw.db.stores import GroupStore, KnowledgeStore, PolicyPlanStore, SettingsAclStore, SkillStore
from sbot.db.stores import KnowledgeStore as SbotKnowledgeStore
from sbot.db.stores import SkillStore as SbotSkillStore
from tests.conftest_app import build_api_app, client
from tests.test_admin import _bearer, _register


async def _groups(db_factory, *names):
    async with db_factory() as db:
        rows = [UserGroup(name=n) for n in names]
        db.add_all(rows)
        await db.commit()
        return [r.id for r in rows]


async def _users(stores, *names):
    return [await stores["users"].get_or_create_by_email(f"{n}@test.local") for n in names]


async def test_membership_roundtrip_primary_mirror_and_counts(db_factory, stores):
    a, b, c = await _groups(db_factory, "A", "B", "C")
    (u,) = await _users(stores, "u")
    users, groups = stores["users"], GroupStore(db_factory)

    user = await users.set_groups(u.id, [b, a, b, ""])  # duplicates/blanks dropped, order kept
    assert await users.group_ids_for(u.id) == [b, a]  # the chosen order is kept, primary first
    assert user.group_id == b  # the first is the primary, mirrored on users.group_id
    assert await groups.counts_by_group() == {a: 1, b: 1}

    with pytest.raises(ValueError):
        await users.set_groups(u.id, [a, "nope"])
    assert set(await users.group_ids_for(u.id)) == {a, b}  # the failed change left things as they were

    # Deleting the primary group keeps the other membership and re-points the mirror at it.
    assert await groups.delete(b)
    assert await users.group_ids_for(u.id) == [a]
    assert (await users.get(u.id)).group_id == a
    assert await groups.delete(a)
    assert await users.group_ids_for(u.id) == [] and (await users.get(u.id)).group_id is None

    # The legacy single-group call still works and replaces everything.
    await users.assign_group(u.id, c)
    assert await users.group_ids_for(u.id) == [c]
    await users.assign_group(u.id, None)
    assert await users.group_ids_for(u.id) == []


@pytest.mark.parametrize("store_type", [SkillStore, SbotSkillStore])
async def test_skill_sharing_with_several_groups(db_factory, stores, store_type):
    a, b, c = await _groups(db_factory, "A", "B", "C")
    owner, in_a, in_b, in_c, in_ab = await _users(stores, "owner", "in_a", "in_b", "in_c", "in_ab")
    users = stores["users"]
    await users.set_groups(owner.id, [a, b])
    await users.set_groups(in_a.id, [a])
    await users.set_groups(in_b.id, [b])
    await users.set_groups(in_c.id, [c])
    skills = store_type(db_factory)

    # In two groups the owner must say which one; the error is explicit rather than a guess.
    with pytest.raises(ValueError, match="Choose which"):
        await skills.upsert(owner.id, "playbook", content="X", visibility="group")
    with pytest.raises(ValueError, match="belong to"):
        await skills.upsert(owner.id, "playbook", content="X", visibility="group", shared_group_id=c)

    shared = await skills.upsert(owner.id, "playbook", content="X", visibility="group", shared_group_id=b)
    assert shared.shared_group_id == b
    for uid, expected in ((in_a.id, False), (in_b.id, True), (in_c.id, False)):
        assert (shared.id in {s.id for s in await skills.available_for_user(uid)}) is expected

    # A later content edit keeps the target; moving to another of the owner's groups is explicit.
    kept = await skills.upsert(owner.id, "playbook", content="Y", visibility="group")
    assert kept.shared_group_id == b
    moved = await skills.upsert(owner.id, "playbook", visibility="group", shared_group_id=a)
    assert moved.shared_group_id == a and shared.id in {s.id for s in await skills.available_for_user(in_a.id)}

    # A recipient in several groups reaches it through any of them.
    await users.set_groups(in_ab.id, [c, a])
    assert shared.id in {s.id for s in await skills.available_for_user(in_ab.id)}
    # Leaving the owner's group closes the door: membership is checked live.
    await users.set_groups(in_ab.id, [c])
    assert shared.id not in {s.id for s in await skills.available_for_user(in_ab.id)}
    # Owner leaving the target group does not silently re-aim the share at another group.
    await users.set_groups(owner.id, [b])
    again = await skills.upsert(owner.id, "playbook", content="Z", visibility="group")
    assert again.shared_group_id == b  # single remaining group -> re-aimed only because the old one is gone


@pytest.mark.parametrize("store_type", [KnowledgeStore, SbotKnowledgeStore])
async def test_group_knowledge_is_visible_only_through_the_groups_it_was_shared_with(db_factory, stores, store_type):
    a, b, c = await _groups(db_factory, "A", "B", "C")
    owner, v_a, v_ac, v_c, v_none = await _users(stores, "kowner", "v_a", "v_ac", "v_c", "v_none")
    users = stores["users"]
    await users.set_groups(owner.id, [a, b])
    await users.set_groups(v_a.id, [a])
    await users.set_groups(v_ac.id, [c, a])  # reaches A through its second group
    await users.set_groups(v_c.id, [c])
    kb_store = store_type(db_factory)
    kb = await kb_store.create_base(owner_id=owner.id, name="Team KB", description="", visibility="group", kind="documents")
    await kb_store.set_shared_groups(kb.id, [a])

    ids = kb_store.accessible_ids
    assert kb.id in await ids(v_a.id) and kb.id in await ids(v_ac.id)
    assert kb.id not in await ids(v_c.id) and kb.id not in await ids(v_none.id)
    assert await kb_store.group_can_read(kb.id, owner.id, v_ac.id)
    assert not await kb_store.group_can_read(kb.id, owner.id, v_c.id)

    # The owner is ALSO in B, but that exposes nothing to B's members: only the chosen list counts.
    (in_b,) = await _users(stores, "in_b")
    await users.set_groups(in_b.id, [b])
    assert kb.id not in await ids(in_b.id)
    # Joining a further group later widens nothing either.
    await users.set_groups(owner.id, [a, b, c])
    assert kb.id not in await ids(in_b.id) and kb.id not in await ids(v_c.id)

    # The owner can share with a group they are not in, and the list is replaced as a whole.
    await kb_store.set_shared_groups(kb.id, [c])
    assert kb.id in await ids(v_c.id) and kb.id not in await ids(v_a.id)
    rows = {r["id"]: r for r in await kb_store.list_accessible(v_ac.id)}
    assert rows[kb.id]["owner_group_names"] == ["C"] and rows[kb.id]["shared_group_ids"] == []  # ids are the owner's
    mine = {r["id"]: r for r in await kb_store.list_accessible(owner.id)}
    assert mine[kb.id]["shared_group_ids"] == [c]
    # Private bases never leak through a shared group.
    private = await kb_store.create_base(owner_id=owner.id, name="Mine", description="", visibility="private", kind="documents")
    assert private.id not in await ids(v_c.id)


async def test_knowledge_and_blueprint_api_pin_the_groups(db_factory, tmp_path):
    from sbot.api.blueprints import router as bp_router
    from sbot.api.deps import current_user as sbot_user
    from sbot.api.deps import get_state as sbot_state
    from claw.api.deps import current_user, get_state
    from sbot.db.stores import BlueprintStore

    app = build_api_app(db_factory)
    app.state.claw.blueprints = BlueprintStore(db_factory)
    app.state.claw.settings.blueprints_root = tmp_path / "bp"
    app.include_router(bp_router)
    app.dependency_overrides[sbot_state] = get_state
    app.dependency_overrides[sbot_user] = current_user
    async with client(app) as c:
        admin, _ = await _register(c, "pin-admin@x.io")
        h = _bearer(admin)
        ga = (await c.post("/api/admin/groups", json={"name": "A"}, headers=h)).json()
        gb = (await c.post("/api/admin/groups", json={"name": "B"}, headers=h)).json()
        owner = (await c.post("/api/admin/users", headers=h, json={"email": "o@x.io", "password": "password123", "group_ids": [ga["id"], gb["id"]]})).json()
        single = (await c.post("/api/admin/users", headers=h, json={"email": "s@x.io", "password": "password123", "group_ids": [ga["id"]]})).json()
        member_b = (await c.post("/api/admin/users", headers=h, json={"email": "mb@x.io", "password": "password123", "group_ids": [gb["id"]]})).json()
        tok = {}
        for email in ("o@x.io", "s@x.io", "mb@x.io"):
            r = await c.post("/api/auth/login", json={"email": email, "password": "password123"})
            tok[email] = _bearer(r.json()["access_token"])

        # Knowledge: an owner in two groups must choose; a one-group owner needs no choice.
        r = await c.post("/api/knowledge", headers=tok["o@x.io"], json={"name": "K", "visibility": "group"})
        assert r.status_code == 422, r.text
        r = await c.post("/api/knowledge", headers=tok["o@x.io"], json={"name": "K", "visibility": "group", "shared_group_ids": ["nope"]})
        assert r.status_code == 404
        r = await c.post("/api/knowledge", headers=tok["o@x.io"], json={"name": "K", "visibility": "group", "shared_group_ids": [ga["id"]]})
        assert r.status_code == 200 and r.json()["shared_group_ids"] == [ga["id"]]
        kb_id = r.json()["id"]
        r = await c.post("/api/knowledge", headers=tok["s@x.io"], json={"name": "S", "visibility": "group"})
        assert r.status_code == 200 and r.json()["shared_group_ids"] == [ga["id"]]  # defaulted to their only group
        assert (await c.get(f"/api/knowledge/{kb_id}/documents", headers=tok["s@x.io"])).status_code == 200
        assert (await c.get(f"/api/knowledge/{kb_id}/documents", headers=tok["mb@x.io"])).status_code == 403  # owner is in B too: still no
        # Switching an existing private base to group needs the same choice.
        priv = (await c.post("/api/knowledge", headers=tok["o@x.io"], json={"name": "P"})).json()
        assert (await c.patch(f"/api/knowledge/{priv['id']}", headers=tok["o@x.io"], json={"visibility": "group"})).status_code == 422
        r = await c.patch(f"/api/knowledge/{priv['id']}", headers=tok["o@x.io"], json={"visibility": "group", "shared_group_ids": [gb["id"]]})
        assert r.status_code == 200 and r.json()["shared_group_ids"] == [gb["id"]]
        assert (await c.get(f"/api/knowledge/{priv['id']}/documents", headers=tok["mb@x.io"])).status_code == 200
        # Changing other fields keeps the audience.
        r = await c.patch(f"/api/knowledge/{priv['id']}", headers=tok["o@x.io"], json={"description": "d"})
        assert r.json()["shared_group_ids"] == [gb["id"]]

        # Blueprints behave the same way.
        data = {"name": "T", "visibility": "group"}
        files = {"file": ("t.docx", b"x" * 20, "application/octet-stream")}
        r = await c.post("/api/blueprints", headers=tok["o@x.io"], data=data, files=files)
        assert r.status_code == 422, r.text
        r = await c.post("/api/blueprints", headers=tok["o@x.io"], data=data | {"shared_group_ids": [gb["id"]]}, files=files)
        assert r.status_code == 200, r.text
        bp = r.json()
        assert bp["shared_group_ids"] == [gb["id"]] and bp["shared_group_names"] == ["B"]
        assert bp["id"] in [x["id"] for x in (await c.get("/api/blueprints", headers=tok["mb@x.io"])).json()]
        assert bp["id"] not in [x["id"] for x in (await c.get("/api/blueprints", headers=tok["s@x.io"])).json()]
        r = await c.patch(f"/api/blueprints/{bp['id']}", headers=tok["o@x.io"], json={"shared_group_ids": [ga["id"]]})
        assert r.status_code == 200 and r.json()["shared_group_ids"] == [ga["id"]]
        assert bp["id"] in [x["id"] for x in (await c.get("/api/blueprints", headers=tok["s@x.io"])).json()]
        assert bp["id"] not in [x["id"] for x in (await c.get("/api/blueprints", headers=tok["mb@x.io"])).json()]
    assert owner["group_ids"] and single["group_ids"] and member_b["group_ids"]


def _pin_migration():
    import pathlib

    return importlib.import_module(
        "migrations.versions." + next(p.stem for p in pathlib.Path("migrations/versions").glob("*_pin_group_shares.py"))
    )


def test_pin_migration_keeps_todays_exposure_for_existing_group_items():
    migration = _pin_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        for ddl in (
            "CREATE TABLE user_groups (id VARCHAR(32) PRIMARY KEY)",
            "CREATE TABLE users (id VARCHAR(32) PRIMARY KEY)",
            "CREATE TABLE user_group_members (user_id VARCHAR(32), group_id VARCHAR(32), position INTEGER DEFAULT 0)",
            "CREATE TABLE knowledge_bases (id VARCHAR(32) PRIMARY KEY, owner_id VARCHAR(32), visibility VARCHAR(16))",
            "CREATE TABLE knowledge_base_shared_groups (kb_id VARCHAR(32), group_id VARCHAR(32), PRIMARY KEY (kb_id, group_id))",
            "CREATE TABLE sbot_blueprints (id VARCHAR(32) PRIMARY KEY, owner_id VARCHAR(32), visibility VARCHAR(16))",
        ):
            conn.execute(sa.text(ddl))
        conn.execute(sa.text("INSERT INTO user_groups VALUES ('g1'), ('g2'), ('g3')"))
        conn.execute(sa.text("INSERT INTO users VALUES ('owner'), ('solo')"))
        conn.execute(sa.text("INSERT INTO user_group_members (user_id, group_id) VALUES ('owner','g1'), ('owner','g2'), ('solo','g3')"))
        conn.execute(sa.text("INSERT INTO knowledge_bases VALUES ('kb-g','owner','group'), ('kb-p','owner','private'), ('kb-solo','solo','group')"))
        conn.execute(sa.text("INSERT INTO knowledge_base_shared_groups VALUES ('kb-g','g3'), ('kb-g','g1')"))  # g1 already explicit
        conn.execute(sa.text("INSERT INTO sbot_blueprints VALUES ('bp-g','owner','group'), ('bp-pub','owner','public')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        kb = sorted(conn.execute(sa.text("SELECT kb_id, group_id FROM knowledge_base_shared_groups")).all())
        # kb-g keeps its extra g3 and gains the owner's current groups (g1 not duplicated); private/public get nothing.
        assert kb == [("kb-g", "g1"), ("kb-g", "g2"), ("kb-g", "g3"), ("kb-solo", "g3")]
        bp = sorted(conn.execute(sa.text("SELECT blueprint_id, group_id FROM sbot_blueprint_shared_groups")).all())
        assert bp == [("bp-g", "g1"), ("bp-g", "g2")]
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "sbot_blueprint_shared_groups" not in sa.inspect(conn).get_table_names()
        assert conn.execute(sa.text("SELECT count(*) FROM knowledge_base_shared_groups")).scalar() == 4  # superset: nothing lost
    engine.dispose()


async def test_plan_with_several_groups_takes_the_highest_ranked_and_own_plan_wins(db_factory, stores):
    plans = PolicyPlanStore(db_factory)
    await plans.seed(
        [
            {"name": "Free", "rank": 0, "max_chat_cost": "low", "is_default": True},
            {"name": "Plus", "rank": 1, "max_chat_cost": "medium"},
            {"name": "Pro", "rank": 2, "max_chat_cost": "high"},
        ]
    )
    by_name = {p["name"]: p for p in await plans.list()}
    a, b, c = await _groups(db_factory, "A", "B", "C")
    groups = GroupStore(db_factory)
    await groups.set_plan(a, by_name["Plus"]["id"])
    await groups.set_plan(b, by_name["Pro"]["id"])
    (u, v, w) = await _users(stores, "pu", "pv", "pw")
    users = stores["users"]
    await users.set_groups(u.id, [a, b])
    await users.set_groups(v.id, [c])  # a group without a plan
    await users.set_groups(w.id, [a, b])
    await users.assign_plan(w.id, by_name["Free"]["id"])  # explicit personal plan

    assert (await plans.resolve_for_user(u.id))["name"] == "Pro"
    assert (await plans.resolve_for_user(v.id))["name"] == "Free"  # falls through to the system default
    assert (await plans.resolve_for_user(w.id))["name"] == "Free"  # personal plan beats any group plan
    batch = await plans.resolve_for_users([u.id, v.id, w.id, "missing"])
    assert [batch[k]["name"] for k in (u.id, v.id, w.id, "missing")] == ["Pro", "Free", "Free", "Free"]


async def test_menu_acl_with_several_groups(db_factory):
    acl = SettingsAclStore(db_factory)
    await acl.set_scope("group", "g1", {"memory": False, "skills": False})
    await acl.set_scope("group", "g2", {"memory": True})
    await acl.set_scope("global", None, {"telegram": False})
    # g2 explicitly shows memory, so the union keeps it; skills is hidden by g1; global still applies.
    assert await acl.hidden_for("u", ["g1", "g2"]) == ["skills", "telegram"]
    assert await acl.hidden_for("u", ["g1"]) == ["memory", "skills", "telegram"]
    assert await acl.hidden_for("u", "g1") == ["memory", "skills", "telegram"]  # legacy single id
    await acl.set_scope("user", "u", {"skills": True})
    assert await acl.hidden_for("u", ["g1", "g2"]) == ["telegram"]  # the user's own rule is last


async def test_admin_api_assigns_and_lists_several_groups(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "admin@x.io")
        h = _bearer(admin)
        ga = (await c.post("/api/admin/groups", json={"name": "Eng"}, headers=h)).json()
        gb = (await c.post("/api/admin/groups", json={"name": "Ops"}, headers=h)).json()

        created = (await c.post("/api/admin/users", headers=h, json={
            "email": "multi@x.io", "password": "password123", "group_ids": [gb["id"], ga["id"]]})).json()
        assert created["group_ids"] == [gb["id"], ga["id"]] and created["group_id"] == gb["id"]
        assert created["group_names"] == ["Ops", "Eng"]

        listed = {u["email"]: u for u in (await c.get("/api/admin/users", headers=h)).json()}
        assert set(listed["multi@x.io"]["group_ids"]) == {ga["id"], gb["id"]}
        counts = {g["id"]: g["user_count"] for g in (await c.get("/api/admin/groups", headers=h)).json()}
        assert counts == {ga["id"]: 1, gb["id"]: 1}

        # PATCH replaces the set; [] clears; the legacy single group_id still works.
        r = await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"group_ids": [ga["id"]]})
        assert r.json()["group_ids"] == [ga["id"]]
        r = await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"group_ids": []})
        assert r.json()["group_ids"] == [] and r.json()["group_id"] is None
        r = await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"group_id": gb["id"]})
        assert r.json()["group_ids"] == [gb["id"]]
        r = await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"display_name": "Renamed"})
        assert r.json()["group_ids"] == [gb["id"]]  # an unrelated edit leaves groups alone

        bad = await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"group_ids": ["nope"]})
        assert bad.status_code == 404
        assert (await c.post("/api/admin/users", headers=h, json={
            "email": "bad@x.io", "password": "password123", "group_ids": ["nope"]})).status_code == 404

        # The user sees every one of their groups on /me.
        await c.patch(f"/api/admin/users/{created['id']}", headers=h, json={"group_ids": [ga["id"], gb["id"]]})
        token = (await c.post("/api/auth/login", json={"email": "multi@x.io", "password": "password123"})).json()
        assert token["user"]["group_ids"] == [ga["id"], gb["id"]]
        me = (await c.get("/api/auth/me", headers=_bearer(token["access_token"]))).json()
        assert me["group_ids"] == [ga["id"], gb["id"]] and me["group_id"] == ga["id"]

        # Deleting one group leaves the user in the other.
        assert (await c.delete(f"/api/admin/groups/{ga['id']}", headers=h)).status_code == 200
        me = (await c.get("/api/auth/me", headers=_bearer(token["access_token"]))).json()
        assert me["group_ids"] == [gb["id"]] and me["group_id"] == gb["id"]


async def test_self_registration_joins_the_default_group(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "first@x.io")
        h = _bearer(admin)
        g = (await c.post("/api/admin/groups", json={"name": "Everyone"}, headers=h)).json()
        await c.put("/api/admin/groups/default", json={"group_id": g["id"]}, headers=h)
        reg = await c.post("/api/auth/register", json={"email": "later@x.io", "password": "password123"})
        if reg.status_code == 200:  # open registration may be off in the test app
            assert reg.json()["user"]["group_ids"] == [g["id"]]


def _migration():
    return importlib.import_module(
        "migrations.versions." + next(
            p.stem for p in __import__("pathlib").Path("migrations/versions").glob("*_user_group_members.py")
        )
    )


def test_migration_backfills_memberships_and_downgrade_restores_the_primary():
    migration = _migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE user_groups (id VARCHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE users (id VARCHAR(32) PRIMARY KEY, group_id VARCHAR(32))"))
        conn.execute(sa.text("INSERT INTO user_groups VALUES ('g1'), ('g2')"))
        conn.execute(sa.text("INSERT INTO users VALUES ('grouped', 'g1'), ('loner', NULL)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = conn.execute(sa.text("SELECT user_id, group_id FROM user_group_members")).all()
        assert rows == [("grouped", "g1")]  # existing single assignments become memberships; ungrouped stay out
        # A second group added after the upgrade, then roll back: nothing is lost for the primary.
        conn.execute(sa.text("INSERT INTO user_group_members (user_id, group_id) VALUES ('loner', 'g2')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "user_group_members" not in sa.inspect(conn).get_table_names()
        assert dict(conn.execute(sa.text("SELECT id, group_id FROM users")).all()) == {"grouped": "g1", "loner": "g2"}
    engine.dispose()


async def test_deleting_a_user_removes_their_memberships(db_factory, stores):
    a, b = await _groups(db_factory, "A", "B")
    keep, gone = await _users(stores, "keep", "gone")
    await stores["users"].set_groups(keep.id, [a])
    await stores["users"].set_groups(gone.id, [a, b])
    groups = GroupStore(db_factory)
    assert await groups.counts_by_group() == {a: 2, b: 1}
    assert await stores["users"].delete(gone.id)
    assert await groups.counts_by_group() == {a: 1}  # no orphan rows, even where FKs are not enforced


async def test_an_unresolvable_personal_plan_means_the_default_not_a_group_plan(db_factory, stores):
    plans = PolicyPlanStore(db_factory)
    await plans.seed(
        [
            {"name": "Free", "rank": 0, "max_chat_cost": "low", "is_default": True},
            {"name": "Pro", "rank": 2, "max_chat_cost": "high"},
        ]
    )
    pro = next(p for p in await plans.list() if p["name"] == "Pro")
    (g,) = await _groups(db_factory, "G")
    await GroupStore(db_factory).set_plan(g, pro["id"])
    (u,) = await _users(stores, "stale")
    await stores["users"].set_groups(u.id, [g])
    await stores["users"].assign_plan(u.id, "deleted-plan-id")  # e.g. a plan removed where FKs are not enforced
    assert (await plans.resolve_for_user(u.id))["name"] == "Free"
    assert (await plans.resolve_for_users([u.id]))[u.id]["name"] == "Free"


async def test_a_group_removed_during_the_request_is_a_404_not_a_500(db_factory, monkeypatch):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "race-admin@x.io")
        h = _bearer(admin)
        g = (await c.post("/api/admin/groups", json={"name": "Ghost"}, headers=h)).json()
        user = (await c.post("/api/admin/users", headers=h, json={"email": "r@x.io", "password": "password123"})).json()
        from claw.api import admin as admin_api

        async def passes_validation(state, ids):  # the group exists when checked ...
            return list(ids or [])

        monkeypatch.setattr(admin_api, "_valid_group_ids", passes_validation)
        await c.delete(f"/api/admin/groups/{g['id']}", headers=h)  # ... and is gone by the time of the write
        r = await c.patch(f"/api/admin/users/{user['id']}", headers=h, json={"group_ids": [g["id"]]})
        assert r.status_code == 404
        r = await c.post("/api/admin/users", headers=h, json={"email": "r2@x.io", "password": "password123", "group_ids": [g["id"]]})
        assert r.status_code == 404


@pytest.mark.parametrize("store_type", [KnowledgeStore, SbotKnowledgeStore])
async def test_knowledge_visibility_and_audience_change_together(db_factory, stores, store_type):
    a, b = await _groups(db_factory, "A", "B")
    (owner,) = await _users(stores, "atomic")
    kb_store = store_type(db_factory)
    # Created as a group base WITH its audience in one step: there is no moment it is group-visible to nobody.
    kb = await kb_store.create_base(owner.id, "K", visibility="group", shared_group_ids=[a])
    assert await kb_store.shared_group_ids(kb.id) == [a]
    await kb_store.update_base(kb.id, shared_group_ids=[b])  # audience replaced, visibility untouched
    assert await kb_store.shared_group_ids(kb.id) == [b]
    await kb_store.update_base(kb.id, visibility="private", shared_group_ids=[a])  # leaving group clears it, ids ignored
    assert await kb_store.shared_group_ids(kb.id) == []
    await kb_store.update_base(kb.id, visibility="group", shared_group_ids=[a, b])  # back to group with a new list
    assert sorted(await kb_store.shared_group_ids(kb.id)) == sorted([a, b])


async def test_blueprint_visibility_and_audience_change_together(db_factory, stores):
    from sbot.db.stores import BlueprintStore

    a, b = await _groups(db_factory, "A", "B")
    (owner,) = await _users(stores, "bpatomic")
    store = BlueprintStore(db_factory)
    await store.create(blueprint_id="bp1", owner_id=owner.id, name="B", description="", visibility="group",
                       filename="a.docx", mime="x", size=1, storage_path="bp1/v1/a.docx", shared_group_ids=[a])
    await store.update("bp1", shared_group_ids=[b])
    assert await store.shared_group_ids("bp1") == [b]
    await store.update("bp1", visibility="public")  # no longer a group item: the audience goes with it
    assert await store.shared_group_ids("bp1") == []
    await store.update("bp1", visibility="group", shared_group_ids=[a, b])
    assert sorted(await store.shared_group_ids("bp1")) == sorted([a, b])
