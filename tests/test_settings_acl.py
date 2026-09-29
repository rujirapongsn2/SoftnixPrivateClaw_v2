"""Settings-menu ACL: user → group → global resolution and admin gating."""

from tests.conftest_app import build_api_app, client


async def _register(c, email):
    r = await c.post("/api/auth/register", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()["user"]


async def _put(c, h, **body):
    return await c.put("/api/admin/settings-acl", json=body, headers=h)


async def test_acl_resolution_and_validation(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_h, _ = await _register(c, "admin@x.io")
        user_h, user = await _register(c, "u@x.io")

        assert (await c.get("/api/auth/settings-acl", headers=user_h)).json() == {"hidden": []}

        # Global hides two menus; group re-shows one; user hides another.
        assert (await _put(c, admin_h, scope="global", rules={"memory": False, "skills": False})).status_code == 200
        g = (await c.post("/api/admin/groups", json={"name": "Ops"}, headers=admin_h)).json()
        await c.patch(f"/api/admin/users/{user['id']}", json={"group_id": g["id"]}, headers=admin_h)
        await _put(c, admin_h, scope="group", target_id=g["id"], rules={"skills": True})
        await _put(c, admin_h, scope="user", target_id=user["id"], rules={"telegram": False})
        hidden = (await c.get("/api/auth/settings-acl", headers=user_h)).json()["hidden"]
        assert hidden == ["memory", "telegram"]

        # Empty rules remove the override; overview lists remaining overrides.
        await _put(c, admin_h, scope="user", target_id=user["id"], rules={})
        ov = (await c.get("/api/admin/settings-acl", headers=admin_h)).json()
        assert ov["users"] == [] and [x["id"] for x in ov["groups"]] == [g["id"]]

        # Profile is locked; unknown sections and targets are rejected.
        assert (await _put(c, admin_h, scope="global", rules={"profile": False})).status_code == 422
        assert (await _put(c, admin_h, scope="global", rules={"nope": False})).status_code == 422
        assert (await _put(c, admin_h, scope="user", target_id="x", rules={"memory": False})).status_code == 404

        # Non-admins cannot read or write the policy.
        assert (await c.get("/api/admin/settings-acl", headers=user_h)).status_code == 403
        assert (await _put(c, user_h, scope="global", rules={})).status_code == 403


async def test_user_search_is_bounded_and_admin_only(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin_h, _ = await _register(c, "admin@x.io")
        user_h, _ = await _register(c, "bob@x.io")
        r = await c.get("/api/admin/settings-acl/users?q=bob", headers=admin_h)
        assert [u["email"] for u in r.json()] == ["bob@x.io"]
        assert (await c.get("/api/admin/settings-acl/users?q=b", headers=admin_h)).json() == []
        assert (await c.get("/api/admin/settings-acl/users?q=bob", headers=user_h)).status_code == 403
        assert (await c.get("/api/admin/settings-acl/users?q=__", headers=admin_h)).json() == []
