"""Schedule tool: let the agent set up and manage its own recurring/one-shot tasks.

A task is stamped with the bot that created it and later runs as that bot, in
its own thread — not in the session the agent happens to be serving now, so
this is safe to call from anywhere.

Tasks are referenced by name or id everywhere, and `create` upserts by name, so
"change the time of X" edits the existing task instead of creating a duplicate.
"""

from datetime import datetime, timezone
from typing import Any

from sbot.core.scheduler import SchedulerService, compute_next_run
from sbot.db.models import Schedule
from sbot.db.stores import ScheduleStore
from sbot.tools.base import Tool


class ScheduleTool(Tool):
    name = "schedule"
    description = (
        "Create, update, list, or delete your own scheduled tasks. A scheduled task runs a prompt "
        "automatically at a time you set, as you, and delivers the result to the user in your chat. "
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

    def __init__(
        self,
        store: ScheduleStore,
        scheduler: SchedulerService | None,
        user_id: str,
        bot_id: str | None = None,
        is_cos: bool = False,
        session_id: str | None = None,
    ):
        self.store = store
        self.scheduler = scheduler
        self.user_id = user_id
        self.bot_id = bot_id
        self.is_cos = is_cos
        # Set only for a group chat, where the bot's own thread is the wrong
        # place to deliver: a group session carries no bot_id, so the scheduler
        # could never route back to it and every run landed in the leader's
        # private chat, out of sight of everyone else in the group.
        self.session_id = session_id

    @property
    def _tz(self) -> str:
        return getattr(self.scheduler, "timezone", "UTC") or "UTC"

    def _notify(self) -> None:
        if self.scheduler:
            self.scheduler.notify_changed()

    async def _scoped(self) -> tuple[list[Schedule], int]:
        """(the tasks this bot may see, how many of the owner's it may not).

        A specialist is scoped to its own, so that two bots given the same
        obvious task name ("daily report") don't collide — `create` upserts by
        name, and without this a second bot would quietly take over the first
        one's task instead of getting its own. The Chief of Staff coordinates
        for the owner and sees everything, including tasks created from
        Settings, which carry no bot.

        The count of what's hidden is returned so a specialist can say "not
        mine, ask your team lead" instead of "you have no scheduled tasks" while
        the owner's tasks keep arriving on time.
        """
        rows = await self.store.list_for_user(self.user_id)
        if self.is_cos:
            return rows, 0
        mine = [r for r in rows if r.bot_id == self.bot_id]
        return mine, len(rows) - len(mine)

    async def _rows(self) -> list[Schedule]:
        return (await self._scoped())[0]

    def _mutable(self, row: Schedule) -> bool:
        """Whether `create`'s upsert-by-name may rewrite this row in place.

        Own rows, plus — for the Chief of Staff — the owner's own tasks, which
        carry no bot because they were made in Settings. A teammate's task is
        not: the Chief of Staff can still reach it deliberately by id or name,
        but creating a task that happens to share its name must not silently
        take it over.
        """
        return row.bot_id == self.bot_id or (self.is_cos and row.bot_id is None)

    @staticmethod
    def _elsewhere(hidden: int) -> str:
        if not hidden:
            return ""
        return (
            f"\n({hidden} other scheduled task(s) belong to your team lead or were set up in "
            "Settings. Ask your team lead to change those.)"
        )

    async def _resolve(self, kwargs: dict) -> tuple[Schedule | None, str | None]:
        """Find a task by schedule_id or name. Returns (row, error_message)."""
        ref = str(kwargs.get("schedule_id") or kwargs.get("name") or "").strip()
        if not ref:
            return None, "Error: provide the task's name or schedule_id."
        rows, hidden = await self._scoped()
        for r in rows:
            if r.id == ref:
                return r, None
        matches = [r for r in rows if r.name.lower() == ref.lower()]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            # Two bots may legitimately own tasks of the same name. If exactly
            # one of them is this bot's to manage, that is the one meant here.
            own = [r for r in matches if self._mutable(r)]
            if len(own) == 1:
                return own[0], None
            ids = ", ".join(f"{m.name} [{m.id}]" for m in matches)
            return None, f"Error: multiple tasks named '{ref}'. Use schedule_id: {ids}"
        return None, f"Error: no task found matching '{ref}'." + self._elsewhere(hidden)

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

    async def _apply_update(
        self, target: Schedule, kwargs: dict, repoint: bool = False
    ) -> dict | str:
        """Build and apply the field changes for an update/upsert. Returns the
        applied fields dict, or an error string.

        `repoint` comes from the create path, where an existing task of the same
        name is edited instead of duplicated: that task has to pick up the chat
        `create` would have given it, or a task set up from a group chat keeps
        delivering into the leader's private thread where nobody sees it.
        """
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
        if repoint and self.session_id and target.session_id != self.session_id:
            fields["session_id"] = self.session_id
        await self.store.update(self.user_id, target.id, **fields)
        self._notify()
        return fields

    @staticmethod
    def _when(cron: str, interval_seconds: int) -> str:
        return cron or (f"every {interval_seconds // 60} min" if interval_seconds else "once, shortly")

    async def execute(self, action: str, **kwargs: Any) -> str:
        action = str(action or "").strip()

        if action == "list":
            rows, hidden = await self._scoped()
            if not rows:
                return "You have no scheduled tasks." + self._elsewhere(hidden)
            return "\n".join(
                f"- [{r.id}] {r.name}: {self._when(r.cron, r.interval_seconds)}"
                f" · next {r.next_run_at.isoformat() if r.next_run_at else 'n/a'}"
                f" · {'on' if r.enabled else 'off'}"
                for r in rows
            ) + self._elsewhere(hidden)

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
            # Upsert by name: editing "change the time of X" must not duplicate
            # X. Only over rows this bot may rewrite, so the Chief of Staff
            # naming a task the same as a teammate's gets its own rather than
            # quietly repurposing theirs.
            rows = [r for r in await self._rows() if self._mutable(r)]
            same = [r for r in rows if r.name.lower() == name.lower()]
            if len(same) == 1:
                applied = await self._apply_update(same[0], kwargs, repoint=True)
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
                bot_id=self.bot_id,
                session_id=self.session_id,
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
