"""Schedule tool: let the agent set up its own recurring/one-shot reminders.

Schedules fire into a fresh chat session for the user (not the current one), so
this is safe regardless of which session the agent is currently serving.
"""

from typing import Any

from claw.core.scheduler import SchedulerService, compute_next_run
from claw.db.stores import ScheduleStore
from claw.tools.base import Tool


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "Create, list, or delete your own scheduled tasks. A scheduled task runs a prompt "
        "automatically at a time you set and delivers the result to the user as a new chat. "
        "Use it for reminders, recurring summaries, or follow-ups. "
        "Provide either cron (e.g. '0 9 * * *' = daily 09:00) or interval_minutes; omit both to run once soon."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list", "delete"]},
            "name": {"type": "string", "description": "Short name (for create)"},
            "prompt": {"type": "string", "description": "Prompt to run on schedule (for create)"},
            "cron": {"type": "string", "description": "Cron expression (optional)"},
            "interval_minutes": {"type": "integer", "description": "Repeat every N minutes (optional)"},
            "schedule_id": {"type": "string", "description": "For delete"},
        },
        "required": ["action"],
    }

    def __init__(self, store: ScheduleStore, scheduler: SchedulerService | None, user_id: str):
        self.store = store
        self.scheduler = scheduler
        self.user_id = user_id

    async def execute(self, action: str, **kwargs: Any) -> str:
        if action == "list":
            rows = await self.store.list_for_user(self.user_id)
            if not rows:
                return "You have no scheduled tasks."
            return "\n".join(
                f"- [{r.id}] {r.name}: {r.cron or (str(r.interval_seconds // 60) + 'm interval' if r.interval_seconds else 'once')}"
                f" · next {r.next_run_at.isoformat() if r.next_run_at else 'n/a'} · {'on' if r.enabled else 'off'}"
                for r in rows
            )

        if action == "delete":
            sid = str(kwargs.get("schedule_id") or "").strip()
            if not sid:
                return "Error: delete requires schedule_id"
            ok = await self.store.delete(self.user_id, sid)
            if ok and self.scheduler:
                self.scheduler.notify_changed()
            return "Deleted." if ok else "Error: schedule not found"

        if action == "create":
            name = str(kwargs.get("name") or "Scheduled task").strip()
            prompt = str(kwargs.get("prompt") or "").strip()
            if not prompt:
                return "Error: create requires a prompt"
            cron = str(kwargs.get("cron") or "").strip()
            interval_seconds = max(0, int(kwargs.get("interval_minutes") or 0)) * 60
            try:
                next_run = compute_next_run(cron, interval_seconds)
            except ValueError as exc:
                return f"Error: {exc}"
            from datetime import datetime, timezone

            row = await self.store.create(
                self.user_id,
                name=name,
                prompt=prompt,
                cron=cron,
                interval_seconds=interval_seconds,
                enabled=True,
                next_run_at=next_run or datetime.now(timezone.utc),
            )
            if self.scheduler:
                self.scheduler.notify_changed()
            when = cron or (f"every {interval_seconds // 60} min" if interval_seconds else "shortly")
            return f"Scheduled '{name}' ({when}). Id: {row.id}"

        return f"Error: unknown action '{action}'"
