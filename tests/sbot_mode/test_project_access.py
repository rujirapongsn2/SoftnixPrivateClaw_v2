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


@pytest.mark.asyncio
async def test_project_tool_passes_the_fresh_user_limit_to_every_mutating_call(stores, tmp_path):
    user = await stores["users"].create("limited@sbot.ai")
    await stores["users"].set_project_policy(user.id, True, 1)
    policy = ProjectAccessPolicy(stores["users"])
    received: list[int | None] = []

    class Projects:
        async def execute(self, *_args, max_projects=None, **_kwargs):
            received.append(max_projects)
            return "ok"

    tool = ProjectTool(SimpleNamespace(projects=Projects()), tmp_path, user.id, policy)
    assert await tool.execute("first-app", "start") == "ok"
    assert await tool.execute("second-app", "exec", "true") == "ok"
    assert received == [1, 1]


@pytest.mark.asyncio
async def test_project_policy_with_several_groups_is_the_union(stores):
    groups = GroupStore(stores["users"].factory)
    locked = await groups.create("Locked")
    small = await groups.create("Small")
    big = await groups.create("Big")
    await groups.set_project_policy(small.id, True, 2)
    await groups.set_project_policy(big.id, True, 5)
    policy = ProjectAccessPolicy(stores["users"])
    user = await stores["users"].create("multi@sbot.ai", group_ids=[locked.id, small.id, big.id])

    access = await policy.resolve(user.id)  # any group that allows it opens it, with the largest limit
    assert (access.allowed, access.max_containers, access.source) == (True, 5, "group")

    only_locked = await stores["users"].create("locked@sbot.ai", group_ids=[locked.id])
    denied = await policy.resolve(only_locked.id)
    assert (denied.allowed, denied.max_containers, denied.source) == (False, 0, "group")

    await stores["users"].set_project_policy(user.id, False, None)  # a personal override still wins
    assert (await policy.resolve(user.id)).source == "user" and not (await policy.resolve(user.id)).allowed
