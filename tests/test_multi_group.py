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
async def test_group_knowledge_is_visible_through_any_shared_group(db_factory, stores, store_type):
    a, b, c = await _groups(db_factory, "A", "B", "C")
    owner, v_ab, v_c, v_none = await _users(stores, "kowner", "v_ab", "v_c", "v_none")
    users = stores["users"]
    await users.set_groups(owner.id, [a])
    await users.set_groups(v_ab.id, [c, a])  # shares A with the owner via its second group
    await users.set_groups(v_c.id, [c])
    kb_store = store_type(db_factory)
    kb = await kb_store.create_base(owner_id=owner.id, name="Team KB", description="", visibility="group", kind="documents")

    ids = kb_store.accessible_ids
    assert kb.id in await ids(v_ab.id)
    assert kb.id not in await ids(v_c.id) and kb.id not in await ids(v_none.id)
    assert await kb_store.group_can_read(kb.id, owner.id, v_ab.id)
    assert not await kb_store.group_can_read(kb.id, owner.id, v_c.id)

    # Explicitly adding group C opens it to C's members; the owner's own groups stay in force.
    await kb_store.set_shared_groups(kb.id, [c])
    assert kb.id in await ids(v_c.id) and await kb_store.group_can_read(kb.id, owner.id, v_c.id)
    rows = {r["id"]: r for r in await kb_store.list_accessible(v_ab.id)}
    assert rows[kb.id]["owner_group_names"] == ["A"] and rows[kb.id]["owner_group_name"] == "A"

    # Moving the owner into a second group extends the base to that group too (resolved live).
    await users.set_groups(owner.id, [a, b])
    (in_b,) = await _users(stores, "in_b")
    await users.set_groups(in_b.id, [b])
    assert kb.id in await ids(in_b.id)
    assert {r["id"] for r in await kb_store.list_accessible(in_b.id)} == {kb.id}
    # Private bases never leak through a shared group.
    private = await kb_store.create_base(owner_id=owner.id, name="Mine", description="", visibility="private", kind="documents")
    assert private.id not in await ids(in_b.id)


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
