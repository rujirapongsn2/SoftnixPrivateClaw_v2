"""Schedule tool: let the agent set up and manage its own recurring/one-shot tasks.

Schedules fire into a fresh chat session for the user (not the current one), so
this is safe regardless of which session the agent is currently serving.

Tasks are referenced by name or id everywhere, and `create` upserts by name, so
"change the time of X" edits the existing task instead of creating a duplicate.
"""

from datetime import datetime, timezone
from typing import Any

from claw.core.scheduler import SchedulerService, compute_next_run
from claw.db.models import Schedule
from claw.db.stores import ScheduleStore
from claw.tools.base import Tool


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "Create, update, list, or delete your own scheduled tasks. A scheduled task runs a prompt "
        "automatically at a time you set and delivers the result to the user as a new chat. "
        "To CHANGE an existing task (e.g. a different time), use action 'update' with its name or "
        "schedule_id — do NOT create a new one. To cancel/remove one, use 'delete' with its name or "
        "schedule_id. Provide either cron (e.g. '0 9 * * *' = daily 09:00) or interval_minutes; times "
        "are in the server's configured timezone. Omit both on create to run once soon."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "list", "delete"]},
            "name": {
                "type": "string",
                "description": "Task name — used on create, and to identify the task for update/delete",
            },
            "prompt": {"type": "string", "description": "Prompt to run on schedule (create/update)"},
            "cron": {"type": "string", "description": "Cron expression, e.g. '0 7 * * *' (create/update)"},
            "interval_minutes": {"type": "integer", "description": "Repeat every N minutes (create/update)"},
            "enabled": {"type": "boolean", "description": "Pause (false) or resume (true) a task (update)"},
            "schedule_id": {"type": "string", "description": "Task id, an alternative to name for update/delete"},
        },
        "required": ["action"],
    }

    def __init__(self, store: ScheduleStore, scheduler: SchedulerService | None, user_id: str):
        self.store = store
        self.scheduler = scheduler
        self.user_id = user_id

    @property
    def _tz(self) -> str:
        return getattr(self.scheduler, "timezone", "UTC") or "UTC"

    def _notify(self) -> None:
        if self.scheduler:
            self.scheduler.notify_changed()

    async def _resolve(self, kwargs: dict) -> tuple[Schedule | None, str | None]:
        """Find a task by schedule_id or name. Returns (row, error_message)."""
        ref = str(kwargs.get("schedule_id") or kwargs.get("name") or "").strip()
        if not ref:
            return None, "Error: provide the task's name or schedule_id."
        rows = await self.store.list_for_user(self.user_id)
        for r in rows:
            if r.id == ref:
                return r, None
        matches = [r for r in rows if r.name.lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            ids = ", ".join(f"{m.name} [{m.id}]" for m in matches)
            return None, f"Error: multiple tasks named '{ref}'. Use schedule_id: {ids}"
        return None, f"Error: no task found matching '{ref}'."

    def _timing(self, existing: Schedule, kwargs: dict) -> tuple[str, int, bool]:
        """Resolve new (cron, interval_seconds, changed) from the args, keeping the
        existing timing when neither cron nor interval is supplied."""
        cron_in = kwargs.get("cron")
        interval_in = kwargs.get("interval_minutes")
        if cron_in is not None and str(cron_in).strip():
            return str(cron_in).strip(), 0, True
        if interval_in is not None and int(interval_in) > 0:
            return "", int(interval_in) * 60, True
        return existing.cron, existing.interval_seconds, False

    async def _apply_update(self, target: Schedule, kwargs: dict) -> dict | str:
        """Build and apply the field changes for an update/upsert. Returns the
        applied fields dict, or an error string."""
        fields: dict[str, Any] = {}
        cron, interval_seconds, timing_changed = self._timing(target, kwargs)
        if timing_changed:
            try:
                next_run = compute_next_run(cron, interval_seconds, tz=self._tz)
            except ValueError as exc:
                return f"Error: {exc}"
            fields["cron"] = cron
            fields["interval_seconds"] = interval_seconds
            fields["next_run_at"] = next_run or datetime.now(timezone.utc)
        prompt = str(kwargs.get("prompt") or "").strip()
        if prompt:
            fields["prompt"] = prompt
        enabled = kwargs.get("enabled")
        if enabled is not None:
            fields["enabled"] = bool(enabled)
            # Re-enabling with no fresh next_run computed above → schedule the next one.
            if bool(enabled) and "next_run_at" not in fields:
                try:
                    nxt = compute_next_run(cron, interval_seconds, tz=self._tz)
                except ValueError:
                    nxt = None
                fields["next_run_at"] = nxt or datetime.now(timezone.utc)
        if not fields:
            return "Error: nothing to update — provide cron, interval_minutes, prompt, or enabled."
        await self.store.update(self.user_id, target.id, **fields)
        self._notify()
        return fields

    @staticmethod
    def _when(cron: str, interval_seconds: int) -> str:
        return cron or (f"every {interval_seconds // 60} min" if interval_seconds else "once, shortly")

    async def execute(self, action: str, **kwargs: Any) -> str:
        action = str(action or "").strip()

        if action == "list":
            rows = await self.store.list_for_user(self.user_id)
            if not rows:
                return "You have no scheduled tasks."
            return "\n".join(
                f"- [{r.id}] {r.name}: {self._when(r.cron, r.interval_seconds)}"
                f" · next {r.next_run_at.isoformat() if r.next_run_at else 'n/a'}"
                f" · {'on' if r.enabled else 'off'}"
                for r in rows
            )

        if action == "delete":
            target, err = await self._resolve(kwargs)
            if err:
                return err
            await self.store.delete(self.user_id, target.id)
            self._notify()
            return f"Deleted task '{target.name}'."

        if action == "update":
            target, err = await self._resolve(kwargs)
            if err:
                return err
            applied = await self._apply_update(target, kwargs)
            if isinstance(applied, str):
                return applied
            return self._describe_update(target, applied)

        if action == "create":
            name = str(kwargs.get("name") or "Scheduled task").strip()
            # Upsert by name: editing "change the time of X" must not duplicate X.
            rows = await self.store.list_for_user(self.user_id)
            same = [r for r in rows if r.name.lower() == name.lower()]
            if len(same) == 1:
                applied = await self._apply_update(same[0], kwargs)
                if isinstance(applied, str):
                    return applied
                return f"A task named '{name}' already existed — updated it instead of creating a duplicate. " + self._describe_update(same[0], applied)
            if len(same) > 1:
                ids = ", ".join(f"[{m.id}]" for m in same)
                return f"Error: several tasks are named '{name}' ({ids}). Update or delete by schedule_id."

            prompt = str(kwargs.get("prompt") or "").strip()
            if not prompt:
                return "Error: create requires a prompt"
            cron = str(kwargs.get("cron") or "").strip()
            interval_seconds = max(0, int(kwargs.get("interval_minutes") or 0)) * 60
            try:
                next_run = compute_next_run(cron, interval_seconds, tz=self._tz)
            except ValueError as exc:
                return f"Error: {exc}"
            row = await self.store.create(
                self.user_id,
                name=name,
                prompt=prompt,
                cron=cron,
                interval_seconds=interval_seconds,
                enabled=True,
                next_run_at=next_run or datetime.now(timezone.utc),
            )
            self._notify()
            return f"Scheduled '{name}' ({self._when(cron, interval_seconds)}). Id: {row.id}"

        return f"Error: unknown action '{action}'"

    @staticmethod
    def _describe_update(target: Schedule, applied: dict) -> str:
        cron = applied.get("cron", target.cron)
        interval = applied.get("interval_seconds", target.interval_seconds)
        parts = [f"now {ScheduleTool._when(cron, interval)}"]
        if "next_run_at" in applied:
            parts.append(f"next run {applied['next_run_at'].isoformat()}")
        if "enabled" in applied:
            parts.append("enabled" if applied["enabled"] else "paused")
        return f"Task '{target.name}': " + ", ".join(parts) + "."
