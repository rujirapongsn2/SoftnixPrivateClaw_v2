"""Knowledge base visibility: private/group/public resolution.

Group visibility is resolved LIVE every query (owner's *current* group via a
join, not a snapshot) — same "never go stale" pattern as
ConnectorManager.resolve_tool_names — plus an explicit
KnowledgeBaseSharedGroup table for additional groups beyond the owner's own.
"""

from claw.db.stores import GroupStore, KnowledgeStore, UserStore
from tests.conftest_app import build_api_app, client


async def _register(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return r.json()["access_token"], r.json()["user"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_private_base_visible_only_to_owner(db_factory, stores):
    knowledge = KnowledgeStore(db_factory, is_postgres=False)
    owner = await stores["users"].get_or_create_by_email("owner@x.y")
    other = await stores["users"].get_or_create_by_email("other@x.y")

    kb = await knowledge.create_base(owner.id, "Private KB", visibility="private")

    assert kb.id in await knowledge.accessible_ids(owner.id)
    assert kb.id not in await knowledge.accessible_ids(other.id)


async def test_public_base_visible_to_everyone(db_factory, stores):
    knowledge = KnowledgeStore(db_factory, is_postgres=False)
    owner = await stores["users"].get_or_create_by_email("owner2@x.y")
    other = await stores["users"].get_or_create_by_email("other2@x.y")

    kb = await knowledge.create_base(owner.id, "Public KB", visibility="public")

    assert kb.id in await knowledge.accessible_ids(owner.id)
    assert kb.id in await knowledge.accessible_ids(other.id)


async def test_group_base_visible_to_owners_own_group_by_default(db_factory, stores):
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales")
    marketing = await groups.create("marketing")
    owner = await users.create(email="sales-owner@x.y", password_hash="h", group_id=sales.id)
    same_group = await users.create(email="sales-mate@x.y", password_hash="h", group_id=sales.id)
    other_group = await users.create(email="marketing-person@x.y", password_hash="h", group_id=marketing.id)
    ungrouped = await users.create(email="ungrouped@x.y", password_hash="h")

    kb = await knowledge.create_base(owner.id, "Sales KB", visibility="group")

    assert kb.id in await knowledge.accessible_ids(owner.id)
    assert kb.id in await knowledge.accessible_ids(same_group.id)
    assert kb.id not in await knowledge.accessible_ids(other_group.id)
    assert kb.id not in await knowledge.accessible_ids(ungrouped.id)


async def test_group_base_follows_owner_when_owners_group_changes(db_factory, stores):
    """Resolution is live — moving the owner to a different group changes who
    can see the base without touching the KnowledgeBase row at all."""
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales2")
    marketing = await groups.create("marketing2")
    owner = await users.create(email="movable-owner@x.y", password_hash="h", group_id=sales.id)
    sales_person = await users.create(email="sales-person2@x.y", password_hash="h", group_id=sales.id)
    marketing_person = await users.create(
        email="marketing-person2@x.y", password_hash="h", group_id=marketing.id
    )

    kb = await knowledge.create_base(owner.id, "Movable KB", visibility="group")
    assert kb.id in await knowledge.accessible_ids(sales_person.id)
    assert kb.id not in await knowledge.accessible_ids(marketing_person.id)

    await users.assign_group(owner.id, marketing.id)

    assert kb.id not in await knowledge.accessible_ids(sales_person.id)
    assert kb.id in await knowledge.accessible_ids(marketing_person.id)


async def test_group_base_explicit_additional_share(db_factory, stores):
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales3")
    marketing = await groups.create("marketing3")
    engineering = await groups.create("engineering3")
    owner = await users.create(email="shared-owner@x.y", password_hash="h", group_id=sales.id)
    marketing_person = await users.create(
        email="marketing-person3@x.y", password_hash="h", group_id=marketing.id
    )
    engineer = await users.create(email="engineer3@x.y", password_hash="h", group_id=engineering.id)

    kb = await knowledge.create_base(owner.id, "Shared KB", visibility="group")
    assert kb.id not in await knowledge.accessible_ids(marketing_person.id)

    await knowledge.set_shared_groups(kb.id, [marketing.id])

    assert kb.id in await knowledge.accessible_ids(marketing_person.id)
    assert kb.id not in await knowledge.accessible_ids(engineer.id)
    assert await knowledge.shared_group_ids(kb.id) == [marketing.id]


async def test_update_base_away_from_group_clears_shared_groups(db_factory, stores):
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales4")
    marketing = await groups.create("marketing4")
    owner = await users.create(email="clearer-owner@x.y", password_hash="h", group_id=sales.id)

    kb = await knowledge.create_base(owner.id, "Clearer KB", visibility="group")
    await knowledge.set_shared_groups(kb.id, [marketing.id])
    assert await knowledge.shared_group_ids(kb.id) == [marketing.id]

    await knowledge.update_base(kb.id, visibility="private")
    assert await knowledge.shared_group_ids(kb.id) == []


async def test_list_accessible_includes_owner_group_name_and_shared_ids(db_factory, stores):
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales5")
    marketing = await groups.create("marketing5")
    owner = await users.create(email="listing-owner@x.y", password_hash="h", group_id=sales.id)

    kb = await knowledge.create_base(owner.id, "Listing KB", visibility="group")
    await knowledge.set_shared_groups(kb.id, [marketing.id])

    rows = await knowledge.list_accessible(owner.id)
    row = next(r for r in rows if r["id"] == kb.id)
    assert row["owner_group_name"] == "sales5"
    assert row["shared_group_ids"] == [marketing.id]


async def test_delete_base_cascades_shared_groups(db_factory, stores):
    """Deleting a base doesn't leave orphaned KnowledgeBaseSharedGroup rows —
    ON DELETE CASCADE, and set_shared_groups on a since-deleted kb_id is a
    silent no-op rather than an error."""
    users = UserStore(db_factory)
    groups = GroupStore(db_factory)
    knowledge = KnowledgeStore(db_factory, is_postgres=False)

    sales = await groups.create("sales6")
    marketing = await groups.create("marketing6")
    owner = await users.create(email="deleted-owner@x.y", password_hash="h", group_id=sales.id)

    kb = await knowledge.create_base(owner.id, "Deleted KB", visibility="group")
    await knowledge.set_shared_groups(kb.id, [marketing.id])

    await knowledge.delete_base(kb.id)
    assert await knowledge.shared_group_ids(kb.id) == []


async def test_create_base_rejects_invalid_visibility(db_factory, stores):
    knowledge = KnowledgeStore(db_factory, is_postgres=False)
    owner = await stores["users"].get_or_create_by_email("invalid-vis@x.y")

    kb = await knowledge.create_base(owner.id, "Bad Vis KB", visibility="not-a-real-value")
    assert kb.visibility == "private"


# ---------------------------------------------------------------- API layer

async def test_create_knowledge_api_rejects_invalid_visibility(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        token, _ = await _register(c, "api-owner@x.io")
        r = await c.post(
            "/api/knowledge",
            json={"name": "Bad", "visibility": "invalid"},
            headers=_bearer(token),
        )
        assert r.status_code == 422


async def test_group_visibility_api_end_to_end(db_factory):
    app = build_api_app(db_factory)
    state = app.state.claw
    async with client(app) as c:
        owner_token, owner = await _register(c, "group-owner@x.io")
        same_token, same = await _register(c, "group-mate@x.io")
        other_token, other = await _register(c, "group-outsider@x.io")

        sales = await state.groups.create("api-sales")
        marketing = await state.groups.create("api-marketing")
        await state.users.assign_group(owner["id"], sales.id)
        await state.users.assign_group(same["id"], sales.id)
        await state.users.assign_group(other["id"], marketing.id)

        created = (
            await c.post(
                "/api/knowledge",
                json={"name": "Sales Handbook", "visibility": "group"},
                headers=_bearer(owner_token),
            )
        ).json()
        kb_id = created["id"]

        # Owner's own group sees it by default.
        r_same = await c.get(f"/api/knowledge/{kb_id}/documents", headers=_bearer(same_token))
        assert r_same.status_code == 200

        # A different group does not.
        r_other = await c.get(f"/api/knowledge/{kb_id}/documents", headers=_bearer(other_token))
        assert r_other.status_code == 403

        # Explicitly sharing with the outsider's group grants access.
        patched = (
            await c.patch(
                f"/api/knowledge/{kb_id}",
                json={"shared_group_ids": [marketing.id]},
                headers=_bearer(owner_token),
            )
        ).json()
        assert patched["shared_group_ids"] == [marketing.id]

        r_other_after = await c.get(f"/api/knowledge/{kb_id}/documents", headers=_bearer(other_token))
        assert r_other_after.status_code == 200


async def test_list_groups_endpoint_accessible_to_non_admin(db_factory):
    app = build_api_app(db_factory)
    state = app.state.claw
    async with client(app) as c:
        token, _ = await _register(c, "regular-user@x.io")
        await state.groups.create("visible-group")

        r = await c.get("/api/groups", headers=_bearer(token))
        assert r.status_code == 200
        names = {g["name"] for g in r.json()}
        assert "visible-group" in names
