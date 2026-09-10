"""Mission tools: how Chief of Staff runs work that outlives a chat turn.

`delegate` blocks the turn, so it can only ever do what fits inside one reply.
These tools instead persist a graph and hand it to the scheduler, which is what
makes long-horizon work possible (PRD §4.3): the user can close the tab, and
`mission_status` picks the thread back up in a later conversation.
"""

import json
from typing import Any

from sbot.core.mission_engine import InvalidGraphError
from sbot.core.missions import MissionService
from sbot.core.turn_context import current_session_id
from sbot.tools.cos import DelegateTool
from sbot.tools.base import Tool

_OUTPUT_PREVIEW_CHARS = 400


class GroupMissionTool(Tool):
    """Restrict group mission management to its own persisted conversation."""

    def __init__(self, tool: Tool, missions: MissionService, owner_id: str):
        self.tool = tool
        self.missions = missions
        self.owner_id = owner_id
        self.name, self.description, self.parameters = tool.name, tool.description, tool.parameters
        self.ends_turn_on_success = tool.ends_turn_on_success

    async def execute(self, **kwargs: Any) -> str:
        session_id = current_session_id.get()
        if not session_id:
            return 'Error: a group conversation is required.'
        if self.name == 'mission_status' and not kwargs.get('mission_id'):
            rows = await self.missions.missions.list_missions(self.owner_id, session_id=session_id)
            return json.dumps([{'id': m.id, 'goal': m.goal, 'status': m.status} for m in rows], ensure_ascii=False)
        if self.name != 'mission_plan':
            mission = await self.missions.missions.get_mission(str(kwargs.get('mission_id') or '').strip(), self.owner_id)
            if mission is None or mission.session_id != session_id:
                return 'Error: this mission is not in the current group conversation.'
        return await self.tool.execute(**kwargs)


class MissionPlanTool(Tool):
    name = "mission_plan"
    description = (
        "Break a large goal into a dependency graph of steps, each assigned to one of your "
        "specialist bots, and save it as a mission. Steps with no dependency on each other run "
        "in parallel. Use this instead of `delegate` when the work needs several specialists, "
        "has ordering between steps, or will take longer than one reply. Planning does not start "
        "the work — call mission_start afterwards."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "What the finished mission delivers."},
            "nodes": {
                "type": "array",
                "description": "The steps. Give each a short id you reuse in other steps' depends_on.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Short unique id, e.g. 'research'"},
                        "title": {"type": "string", "description": "Short label for the step"},
                        "bot_id": {
                            "type": "string",
                            "description": "Id of the specialist that runs this step (from list_bots). "
                            "Required for every step except a 'gate', which nobody runs.",
                        },
                        "instruction": {
                            "type": "string",
                            "description": "Self-contained instructions. The specialist sees only this, "
                            "its charter, the mission goal, and earlier steps' results.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Ids of steps that must finish first. Omit for steps that can start immediately.",
                        },
                        "required_files": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Exact workspace paths this step must deliver. Required for file tasks; use [] for text-only work. Completion is rejected if these files are not published.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["task", "gate"],
                            "description": "'gate' pauses the mission for the user to review before dependents run.",
                        },
                    },
                    "required": ["id", "title", "instruction", "required_files"],
                },
            },
        },
        "required": ["goal", "nodes"],
    }

    def __init__(self, missions: MissionService, owner_id: str, member_ids: frozenset[str] | None = None):
        self.missions = missions
        self.owner_id = owner_id
        self.member_ids = member_ids

    async def execute(self, goal: str, nodes: list[dict], **_: Any) -> str:
        try:
            mission = await self.missions.plan(
                self.owner_id, goal, nodes, session_id=current_session_id.get(), member_ids=self.member_ids
            )
        except InvalidGraphError as exc:
            return f"Error: {exc}. Fix the plan and call mission_plan again."
        return (
            f"Mission created (id: {mission.id}) with {len(nodes)} steps. "
            "Call mission_start with this id to begin."
        )


class MissionStartTool(Tool):
    name = "mission_start"
    ends_turn_on_success = True
    description = (
        "Start or resume a mission. It checks remaining budget before scheduling. "
        "If paused, report the blocker; never say it started or repeatedly retry. "
        "Optional budget sets cumulative ceilings (historical spend is retained). "
        "Supply increased ceilings only after the user explicitly approves the new budget; "
        "a generic request to continue does not authorize a budget increase."
    )
    parameters = {
        "type": "object",
        "properties": {"mission_id": {"type": "string"}, "budget": {
            "type": "object", "additionalProperties": False, "properties": {
                "max_tokens": {"type": "number", "exclusiveMinimum": 0},
                "max_wall_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_node_attempts": {"type": "integer", "minimum": 1},
            }}},
        "required": ["mission_id"],
    }

    def __init__(self, missions: MissionService, owner_id: str):
        self.missions = missions
        self.owner_id = owner_id

    async def execute(self, mission_id: str, budget: dict | None = None, **_: Any) -> str:
        try:
            if budget is None:
                status = await self.missions.start(str(mission_id).strip(), self.owner_id)
            else:
                status = await self.missions.start(str(mission_id).strip(), self.owner_id, budget=budget)
        except (InvalidGraphError, TypeError) as exc:
            return f"Error: {exc}"
        if status == "not_found":
            return f"Error: no mission {mission_id!r} belongs to this user."
        if status in ('completed', 'cancelled'):
            return f"Mission {mission_id} is already {status}; no work was restarted."
        if status == 'paused':
            detail = await self.missions.status(str(mission_id).strip(), self.owner_id)
            return json.dumps({
                'mission_id': mission_id, 'status': 'paused', 'started': False,
                'reason': detail.get('resume_blocker'), 'budget': detail['budget'], 'spent': detail['spent'],
                'message': 'งานยังไม่เริ่มต่อ เพราะงบที่ใช้ไปถึงขีดจำกัดแล้ว ต้องอนุมัติงบใหม่ก่อน การสั่งเริ่มซ้ำไม่เพิ่มงบ',
            }, ensure_ascii=False)
        return (
            f"Mission {mission_id} is {status} in the background. "
            "Check mission_status for progress; do not block waiting for it."
        )


class MissionStatusTool(Tool):
    name = "mission_status"
    description = (
        "Report a mission's progress: each step's state, what finished steps produced, and what "
        "the mission has spent. Call this when the user asks how the work is going, or to pick up "
        "a mission started in an earlier conversation. Omit mission_id to list recent missions. "
        "Provide node_id and offset to read the full result in pages of up to 8000 characters."
    )
    parameters = {
        "type": "object",
        "properties": {"mission_id": {"type": "string"}, "node_id": {"type": "string"},
                       "offset": {"type": "integer"}, "limit": {"type": "integer"}},
    }

    def __init__(self, missions: MissionService, owner_id: str):
        self.missions = missions
        self.owner_id = owner_id

    async def execute(self, mission_id: str | None = None, node_id: str | None = None,
                      offset: int = 0, limit: int = 8000, **_: Any) -> str:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            recent = await self.missions.missions.list_missions(self.owner_id, limit=10)
            if not recent:
                return "No missions yet."
            return json.dumps(
                [{"id": m.id, "goal": m.goal, "status": m.status} for m in recent],
                ensure_ascii=False,
                indent=2,
            )

        status = await self.missions.status(mission_id, self.owner_id)
        if status is None:
            return f"Error: no mission {mission_id!r} belongs to this user."
        if node_id:
            node = next((n for n in status['nodes'] if n['id'] == node_id), None)
            if node is None:
                return 'Error: node not found.'
            text = node['output']
            offset, limit = max(0, offset), min(8000, max(1, limit))
            return json.dumps({**node, 'output': text[offset:offset + limit], 'total_chars': len(text),
                               'next_offset': offset + limit if offset + limit < len(text) else None}, ensure_ascii=False)
        # Outputs are truncated: a status check on a finished mission would
        # otherwise pull every step's full result into the prompt at once.
        for node in status["nodes"]:
            text = node["output"]
            if len(text) > _OUTPUT_PREVIEW_CHARS:
                node["output"] = text[:_OUTPUT_PREVIEW_CHARS] + f"… [{len(text)} chars total]"
        return json.dumps(status, ensure_ascii=False, indent=2, default=str)


class MissionGateTool(Tool):
    name = "mission_gate"
    description = (
        "Record the user's decision on a mission step that is waiting for their review, and let "
        "the mission carry on. Approving releases the step so the work depending on it runs; "
        "declining stops the mission and leaves the step waiting, so nothing downstream happens. "
        "Only call this once the user has actually told you what they decided — the review is "
        "theirs to make, not yours."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mission_id": {"type": "string", "description": "The mission being reviewed."},
            "node_id": {
                "type": "string",
                "description": "Id of the step waiting for review (status 'awaiting_human' in mission_status).",
            },
            "approved": {
                "type": "boolean",
                "description": "true to release the step, false to stop the mission here.",
            },
            "note": {
                "type": "string",
                "description": "Optional: what the user said, recorded on the step as the reason.",
            },
        },
        "required": ["mission_id", "node_id", "approved"],
    }

    def __init__(self, missions: MissionService, owner_id: str):
        self.missions = missions
        self.owner_id = owner_id

    async def execute(
        self,
        mission_id: str,
        node_id: str,
        approved: bool,
        note: str = "",
        **_: Any,
    ) -> str:
        try:
            status = await self.missions.resolve_gate(
                str(mission_id).strip(),
                self.owner_id,
                str(node_id).strip(),
                bool(approved),
                note=note,
            )
        except InvalidGraphError as exc:
            return f"Error: {exc}."
        if not approved:
            return (
                f"Step {node_id} was declined and the mission is now {status}. Nothing depending "
                "on that step ran."
            )
        return (
            f"Step {node_id} approved; the mission is {status} again. It runs in the background — "
            "use mission_status to check on it, and do not block waiting for it."
        )


class MissionReplanTool(Tool):
    name = "mission_replan"
    description = (
        "Patch a stuck mission's graph: retry a failed step with a corrected instruction or a "
        "different specialist, skip a step the mission can do without, and add steps the plan "
        "was missing. Use this when steps have failed every attempt — it repairs the existing "
        "graph, so finished steps keep their results, which planning the mission again would "
        "throw away. Prefer `retry` over adding a replacement step: retry keeps the step's id, "
        "so everything already depending on it still receives its result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "mission_id": {"type": "string", "description": "The mission to patch."},
            "retry": {
                "type": "array",
                "description": "Failed steps to re-arm. Each needs a new instruction or a "
                "different bot — re-running the same step unchanged is what already failed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Id of the failed step."},
                        "instruction": {
                            "type": "string",
                            "description": "Replacement instruction, corrected for why it failed.",
                        },
                        "bot_id": {
                            "type": "string",
                            "description": "Reassign the step to this specialist (from list_bots).",
                        },
                    },
                    "required": ["id"],
                },
            },
            "skip": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids of steps to abandon. Their dependents then run without "
                "their output, so only skip steps the rest of the graph can cope without.",
            },
            "add": {
                "type": "array",
                "description": "New steps to append, same shape as mission_plan's nodes. They "
                "may depend on existing steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "bot_id": {"type": "string"},
                        "instruction": {"type": "string"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "required_files": {"type": "array", "items": {"type": "string"}},
                        "kind": {"type": "string", "enum": ["task", "gate"]},
                    },
                    "required": ["id", "title", "instruction", "required_files"],
                },
            },
        },
    }

    def __init__(self, missions: MissionService, owner_id: str, mission_id: str | None = None):
        self.missions = missions
        self.owner_id = owner_id
        # Pinned when the scheduler grants this tool to a replanning Chief of
        # Staff: that call is about one stuck mission, so the mission it patches
        # is not the model's to choose.
        self.mission_id = mission_id

    async def execute(
        self,
        mission_id: str | None = None,
        retry: list[dict] | None = None,
        skip: list[str] | None = None,
        add: list[dict] | None = None,
        **_: Any,
    ) -> str:
        target = self.mission_id or str(mission_id or "").strip()
        if not target:
            return "Error: mission_id is required."
        try:
            return await self.missions.apply_replan(
                target, self.owner_id, skip=skip, retry=retry, add=add
            )
        except InvalidGraphError as exc:
            return f"Error: {exc}. Fix the patch and call mission_replan again."

# Additive assignment contract shared with direct delegation.
MissionPlanTool.parameters['properties']['nodes']['items']['properties'].update({
    key: DelegateTool.parameters['properties'][key]
    for key in ('input_files', 'delivery_target', 'acceptance_criteria', 'verification')
})
