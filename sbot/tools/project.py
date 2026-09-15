"""One persistent development environment shared by the owner's team per project."""
from typing import ClassVar

from sbot.tools.base import Tool

PROJECT_ACTIONS = (
    "create",
    "start",
    "status",
    "stop",
    "delete",
    "exec",
    "compose_up",
    "compose_ps",
    "compose_logs",
    "compose_down",
)
# Every action except an inspection or an explicit stop can start or remove a container.
PROJECT_ACTIONS_REQUIRING_CONFIRMATION = frozenset(PROJECT_ACTIONS) - {"status", "stop"}
# Destructive lifecycle operations always get their own explicit decision, even
# after the user granted ordinary access to this project for the turn.
PROJECT_ACTIONS_COVERED_BY_TASK_APPROVAL = (
    PROJECT_ACTIONS_REQUIRING_CONFIRMATION - {"delete"}
)


class ProjectTool(Tool):
    name = 'project'
    description = (
        'Manage a persistent software project environment. Packages, files and services survive tool calls '
        'and Sbot restarts. One slug identifies one application: never reuse an existing slug for an unrelated '
        'new application or to work around the container limit. Create every new application with action=create; '
        'that action rejects an existing slug. Use the SAME slug only when continuing that '
        'application across team members. File tools access projects/<slug>/; '
        'project exec runs there at /workspace. Write compose.yaml then compose_up for persistent services; '
        'status returns localhost port mappings. Use exec for git, builds, tests and curl. '
        'Use separate git worktrees for parallel edits and report commit/test evidence. '
        'stop preserves the container. delete removes the project container and its network but preserves files '
        'and named volumes, so create can rebuild it with current settings. compose_down preserves named volumes. '
        'Never claim deployment from a dispatch alone.'
    )
    parameters: ClassVar[dict] = {
        'type': 'object',
        'properties': {
            'project': {
                'type': 'string',
                'description': 'Stable lowercase slug unique to this application; do not reuse for another app',
            },
            'action': {'type': 'string', 'enum': list(PROJECT_ACTIONS)},
            'command': {'type': 'string'},
            'timeout_seconds': {'type': 'integer', 'description': '1–1800 seconds; default 90'},
        },
        'required': ['project', 'action'],
    }

    def __init__(self, sandbox, workspace, owner_id: str | None = None, project_access=None):
        self.sandbox = sandbox
        self.workspace = workspace
        self.owner_id = owner_id
        self.project_access = project_access

    async def execute(self, project, action, command='', timeout_seconds=90, **kwargs):
        max_projects = None
        if self.project_access is not None and self.owner_id is not None and action not in {"status", "stop"}:
            access = await self.project_access.resolve(self.owner_id)
            if not access.allowed:
                return "Error: project containers are not allowed for this account."
            max_projects = access.max_containers
        return await self.sandbox.projects.execute(
            self.workspace, project, action, command, timeout_seconds, max_projects=max_projects
        )
