from tests.conftest_app import build_api_app, client
from tests.test_admin import _register, _bearer


async def test_team_policy_is_admin_only_validated_and_persisted(db_factory):
    app = build_api_app(db_factory)
    async with client(app) as c:
        admin, _ = await _register(c, "admin@policy.io")
        user, _ = await _register(c, "user@policy.io")
        assert (await c.get("/api/admin/team-policy")).status_code == 401
        assert (await c.get("/api/admin/team-policy", headers=_bearer(user))).status_code == 403
        response = await c.get("/api/admin/team-policy", headers=_bearer(admin))
        assert response.status_code == 200
        policy = response.json()
        assert (await c.put("/api/admin/team-policy", json=policy, headers=_bearer(user))).status_code == 403
        invalid = {**policy, "max_step_recoveries": 999}
        assert (
            await c.put("/api/admin/team-policy", json=invalid, headers=_bearer(admin))
        ).status_code == 422
        policy["model_output_limits"] = {"private/model": 2048}
        policy["max_step_recoveries"] = 3
        assert (await c.put("/api/admin/team-policy", json=policy, headers=_bearer(admin))).status_code == 200
    restarted = build_api_app(db_factory)
    async with client(restarted) as c:
        response = await c.get("/api/admin/team-policy", headers=_bearer(admin))
        assert response.status_code == 200
        assert response.json() == policy
