from types import SimpleNamespace

import pytest

from sbot.core.project_access import ProjectAccessPolicy
from sbot.db.stores import GroupStore
from sbot.tools.project import ProjectTool


@pytest.mark.asyncio
async def test_project_policy_inherits_group_and_user_override(stores):
    groups = GroupStore(stores["users"].factory)
    group = await groups.create("Engineering")
    user = await stores["users"].create("developer@sbot.ai", group_id=group.id)
    policy = ProjectAccessPolicy(stores["users"])

    denied = await policy.resolve(user.id)
    assert (denied.allowed, denied.max_containers, denied.source) == (False, 0, "group")

    await groups.set_project_policy(group.id, True, 2)
    inherited = await policy.resolve(user.id)
    assert (inherited.allowed, inherited.max_containers, inherited.source) == (True, 2, "group")

    await stores["users"].set_project_policy(user.id, False, None)
    direct_deny = await policy.resolve(user.id)
    assert (direct_deny.allowed, direct_deny.max_containers, direct_deny.source) == (False, 0, "user")

    await stores["users"].set_project_policy(user.id, True, 1)
    direct_allow = await policy.resolve(user.id)
    assert (direct_allow.allowed, direct_allow.max_containers, direct_allow.source) == (True, 1, "user")


@pytest.mark.asyncio
async def test_project_tool_denies_creation_before_touching_docker(stores, tmp_path):
    user = await stores["users"].create("denied@sbot.ai")
    policy = ProjectAccessPolicy(stores["users"])

    class Projects:
        async def execute(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("denied user reached Docker")

    tool = ProjectTool(SimpleNamespace(projects=Projects()), tmp_path, user.id, policy)
    assert (await tool.execute("demo", "start")).startswith("Error: project containers are not allowed")
