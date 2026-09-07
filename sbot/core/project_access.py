"""Resolve the project-container policy that guards every project tool call."""

from dataclasses import dataclass

from sbot.db.models import User, UserGroup
from sbot.db.stores import UserStore


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    allowed: bool
    max_containers: int
    source: str  # "user" | "group" | "none"


class ProjectAccessPolicy:
    """User overrides take precedence; otherwise inherit the user's group.

    This is intentionally resolved on each operation instead of cached in an
    agent, so an administrator's revoke takes effect on the next tool call.
    """

    def __init__(self, users: UserStore):
        self.users = users

    async def resolve(self, user_id: str) -> ProjectAccess:
        async with self.users.factory() as db:
            user = await db.get(User, user_id)
            if user is None:
                return ProjectAccess(False, 0, "none")
            if user.project_containers_enabled is not None:
                limit = user.project_container_limit or 0
                return ProjectAccess(bool(user.project_containers_enabled) and limit > 0, limit, "user")
            group = await db.get(UserGroup, user.group_id) if user.group_id else None
            if group is None:
                return ProjectAccess(False, 0, "none")
            limit = group.project_container_limit or 0
            return ProjectAccess(bool(group.project_containers_enabled) and limit > 0, limit, "group")
