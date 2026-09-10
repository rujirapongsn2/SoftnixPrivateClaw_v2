"""Conversation-facing background work. Workers never receive these tools."""

import copy
import hashlib
import json

from sbot.core.mission_engine import InvalidGraphError
from sbot.core.turn_context import current_session_id, current_turn_id, current_turn_locale
from sbot.tools.base import Tool
from sbot.tools.cos import DelegateManyTool, DelegateTool
from sbot.tools.missions import MissionPlanTool, MissionStatusTool


class TeamSubmitTool(Tool):
    name = "team_submit"
    ends_turn_on_success = True
    description = (
        "Accept a new job and start it in the background. Returns a job receipt immediately, "
        "not its result. Use for specialist work so the user can keep chatting. Submit all steps "
        "of a request together; independent steps run concurrently, depends_on steps wait for "
        "successful results. A coordinator combines multi-step results automatically. Workers "
        "do not see chat history. Set input_files and required_files explicitly. No automatic "
        "replay of failed assignments; inspect their result before authorizing another attempt."
    )
    parameters = copy.deepcopy(MissionPlanTool.parameters)

    def __init__(self, service, owner_id, coordinator_id, member_ids=None):
        self.service = service
        self.owner_id = owner_id
        self.coordinator_id = coordinator_id
        self.member_ids = member_ids

    async def execute(self, goal, nodes, **kwargs):
        session_id, turn_id = current_session_id.get(), current_turn_id.get()
        if not session_id or not turn_id:
            return "Error: background work requires a conversation turn."
        # Same call replayed in this turn returns the same persisted job. A new
        # user turn can intentionally request the same work again.
        payload = json.dumps([self.owner_id, session_id, turn_id, goal, nodes], sort_keys=True, ensure_ascii=False)
        mission_id = hashlib.sha256(payload.encode()).hexdigest()[:32]
        try:
            mission = await self.service.submit_work(
                self.owner_id, session_id, mission_id, goal, nodes,
                self.coordinator_id, self.member_ids,
            )
        except (InvalidGraphError, ValueError) as exc:
            return f"Error: {exc}"
        if current_turn_locale.get() == "th":
            return (f"รับงานแล้ว: {mission.goal}\nรหัสงาน: {mission.id}\n"
                    f"สถานะ: {mission.status}\nงานอยู่ในระบบเบื้องหลัง คุณส่งงานใหม่หรือถามความคืบหน้าได้เลย "
                    "ผลลัพธ์จะรายงานกลับในห้องนี้")
        return (f"Job accepted: {mission.goal}\nJob ID: {mission.id}\nStatus: {mission.status}\n"
                "Background work is tracked separately. You can send another request or ask for progress; "
                "the result will be delivered here.")


class BackgroundDelegateTool(Tool):
    ends_turn_on_success = True

    def __init__(self, submit, delegate, many=False):
        self.submit, self.delegate, self.many = submit, delegate, many
        source = DelegateManyTool if many else DelegateTool
        self.name, self.parameters = source.name, copy.deepcopy(source.parameters)
        self.description = (
            "Hand off independent work in the background and return a job receipt immediately. "
            "The receipt is not a completed result. For dependencies use team_submit instead. "
            "Include all assignments in one call; use team_status to check progress."
        )

    async def execute(self, **kwargs):
        assignments = kwargs.get("assignments") if self.many else [kwargs]
        if not isinstance(assignments, list) or not assignments:
            return "Error: provide at least one assignment."
        if len(assignments) > self.submit.service.settings.team_work.max_steps:
            return "Error: too many assignments in one job."
        nodes = []
        for i, assignment in enumerate(assignments):
            if not isinstance(assignment, dict) or not str(assignment.get("task") or "").strip():
                return "Error: every assignment needs a task."
            bot = await self.delegate._resolve_target(assignment.get("bot_id"), assignment.get("bot_name"))
            if bot is None:
                return "Error: target bot is unavailable in this team; check list_bots."
            nodes.append({
                "id": f"step-{i + 1}", "title": assignment["task"][:120], "bot_id": bot.id,
                "instruction": assignment["task"] + "\n" + str(assignment.get("context") or ""),
                "required_files": assignment.get("required_files") or [],
                **{key: assignment[key] for key in (
                    "input_files", "delivery_target", "acceptance_criteria", "verification"
                ) if key in assignment},
            })
        goal = nodes[0]["title"] if len(nodes) == 1 else " / ".join(n["title"] for n in nodes)[:500]
        return await self.submit.execute(goal=goal, nodes=nodes)


class TeamStatusTool(Tool):
    name = "team_status"
    description = (
        "Read actual pending team jobs, including queued, running, paused and blocked work, "
        "without waiting for workers. Returns bot names, progress and job IDs. "
        "Use mission_id for a particular job including completed or failed results."
    )
    parameters = {"type": "object", "properties": {"mission_id": {"type": "string"}}}

    def __init__(self, service, owner_id, group=False):
        self.service, self.owner_id, self.group = service, owner_id, group

    async def execute(self, mission_id=None, **kwargs):
        session_id = current_session_id.get() if self.group else None
        if self.group and not session_id:
            return "Error: a group conversation is required."
        if mission_id:
            mission = await self.service.missions.get_mission(mission_id, self.owner_id)
            if mission is None or (self.group and mission.session_id != session_id):
                return "Error: job not found in this conversation."
            return await MissionStatusTool(self.service, self.owner_id).execute(mission_id)
        rows = await self.service.missions.active_missions(self.owner_id, limit=100, session_id=session_id)
        jobs = []
        for mission, _ in rows:
            status = await self.service.status(mission.id, self.owner_id)
            jobs.append({key: status[key] for key in ("id", "goal", "status", "spent", "progress")})
        recent = await self.service.missions.list_missions(self.owner_id, limit=10, session_id=session_id)
        return json.dumps({
            "jobs": jobs, "limit": 100, "possibly_more": len(rows) == 100,
            "recent_results": [{"id": m.id, "goal": m.goal, "status": m.status} for m in recent
                               if m.status in ('completed', 'failed', 'cancelled')],
        }, ensure_ascii=False)


class TeamCancelTool(TeamStatusTool):
    name = "team_cancel"
    description = (
        "Stop a job the user explicitly asked to stop. Prevents pending steps from starting. "
        "An already executing step may finish; completed external actions are not undone."
    )
    parameters = {"type": "object", "properties": {"mission_id": {"type": "string"}}, "required": ["mission_id"]}

    async def execute(self, mission_id, **kwargs):
        if self.group and not current_session_id.get():
            return 'Error: a group conversation is required.'
        mission = await self.service.missions.get_mission(mission_id, self.owner_id)
        if mission is None or (self.group and mission.session_id != current_session_id.get()):
            return "Error: job not found in this conversation."
        await self.service.cancel(mission_id, self.owner_id)
        return f"Stop requested for {mission_id}. Pending steps will not start; an executing step may finish."
