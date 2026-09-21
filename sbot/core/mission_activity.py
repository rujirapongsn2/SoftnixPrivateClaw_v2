"""Bounded, display-only observation of mission work. Never invokes a bot."""
import asyncio
import copy
import hashlib
from datetime import datetime, timezone

from loguru import logger

from sbot.core.tool_preview import safe_text_preview

MAX_ACTIVITY_TEXT_CHARS = 12_000
MAX_ACTIVITY_RESULT_CHARS = 20_000
MAX_ACTIVITY_TOOL_STEPS = 100


def result_preview(value: str | None) -> tuple[str, bool]:
    """Return a bounded UI preview; the authoritative output stays on the node."""
    text = value or ''
    return text[:MAX_ACTIVITY_RESULT_CHARS], len(text) > MAX_ACTIVITY_RESULT_CHARS


class MissionActivity:
    def __init__(self, service, mission, node):
        self.service, self.mission, self.node = service, mission, node
        self.key = hashlib.sha256(f'activity:{mission.id}:{node.id}:{node.attempts}'.encode()).hexdigest()[:32]
        self.session_id = None
        self.data = {}
        self.changed = asyncio.Event()
        self.worker = None
        self.closed = False
        self.revision = 0
        self.ready = asyncio.Event()

    def start(self):
        """Start observation without putting its database I/O on the work path."""
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())
            self.service._activity_tasks.add(self.worker)
            self.worker.add_done_callback(self.service._activity_tasks.discard)

    def finish(self, result=None, status='interrupted'):
        """Record the executor outcome and let the scheduler confirm final state."""
        if self.closed:
            return
        self.closed = True
        self.data['status'] = 'finishing' if result else status
        if result:
            preview, truncated = result_preview(result.output)
            self.data['result'] = preview
            self.data['result_truncated'] = truncated
            self.data['artifacts'] = result.artifacts or []
        for step in self.data.get('steps', []):
            if step['status'] == 'running':
                finished_at = self.now()
                step['status'] = 'interrupted'
                step['finished_at'] = finished_at
                try:
                    started = datetime.fromisoformat(step.get('started_at') or step['at'])
                    finished = datetime.fromisoformat(finished_at)
                    step['duration_ms'] = max(0, int((finished - started).total_seconds() * 1000))
                except (TypeError, ValueError):
                    step['duration_ms'] = None
        self.changed.set()

    async def wait(self):
        if self.worker:
            await self.worker

    async def open(self):
        s = self.service
        if not s.sessions or not s.messages or not self.node.bot_id or self.node.id == '__summary':
            return
        try:
            members = await s.missions.blackboard_read(self.mission.id, 'scope:members')
            current = await s._current_members(self.mission.owner_id, self.mission.session_id)
            if current is not None:
                members = current if members is None else set(members) & current
            if members is not None and self.node.bot_id not in members:
                return
            bot = await s.bots.get(self.node.bot_id, self.mission.owner_id)
            if bot is None:
                return
            session = await s.sessions.thread_for_bot(self.mission.owner_id, bot.id, title=bot.name)
            origin = await s.sessions.get(self.mission.session_id) if self.mission.session_id else None
            leader = await s.bots.get(origin.bot_id, self.mission.owner_id) if origin and origin.bot_id else None
            self.session_id = session.id
            initial = dict(mission_id=self.mission.id, node_id=self.node.id, attempt=self.node.attempts,
                             title=self.node.title, instruction=self.node.instruction,
                             inputs=(self.node.budget or {}).get('input_files', []),
                             required_files=(self.node.budget or {}).get('required_files', []),
                             depends_on=self.node.depends_on or [],
                             leader_name=leader.name if leader else 'Team Lead',
                             origin_session=self.mission.session_id, bot_name=bot.name,
                             status='queued', text='', steps=[], artifacts=[], result='',
                             result_truncated=False)
            # Events can arrive while the room lookup is in flight. Preserve
            # their bounded fields while filling in assignment metadata.
            initial.update({
                key: self.data[key]
                for key in ('status', 'text', 'steps', 'inputs', 'artifacts', 'result', 'result_truncated')
                if key in self.data
            })
            self.data = initial
            await self.save()
            # A link only in the leader's room; activity remains in the worker's room.
            if origin and origin.id != session.id:
                link_key = hashlib.sha256(f'handoff:{self.key}'.encode()).hexdigest()[:32]
                await s.messages.append(origin.id, [dict(role='observation',
                    content=self.node.title, meta={'delivery_id': link_key, 'mission_handoff': {
                        'title': self.node.title, 'bot_name': bot.name, 'session_id': session.id,
                        'activity_id': self.key, 'attempt': self.node.attempts}})], delivery_key=link_key)
        except Exception:
            logger.warning('Mission activity unavailable for {} / {}', self.mission.id, self.node.id)
            self.session_id = None

    def emit(self, event):
        if self.closed:
            return
        try:
            self._emit(event)
        except Exception:
            logger.warning('Invalid mission activity event for {}', self.key)

    def _emit(self, event):
        kind = getattr(event, 'type', '')
        if kind == 'text_delta':
            self.data['text'] = (self.data.get('text', '') + event.text)[-MAX_ACTIVITY_TEXT_CHARS:]
        elif kind == 'tool_started':
            steps = self.data.setdefault('steps', [])
            if steps and steps[-1]['tool'] == event.tool and steps[-1]['status'] == 'running':
                return
            # The loop has already redacted structured credentials from
            # args_preview. Apply the plain-text guard as a second boundary
            # before persisting it in the specialist's room.
            started_at = self.now()
            steps.append(dict(
                tool=event.tool,
                status='running',
                at=started_at,
                started_at=started_at,
                args_preview=safe_text_preview(event.args_preview),
                result_preview='',
                finished_at=None,
                duration_ms=None,
            ))
            self.data['steps'] = steps[-MAX_ACTIVITY_TOOL_STEPS:]
        elif kind == 'tool_finished':
            for step in reversed(self.data.get('steps', [])):
                if step['tool'] == event.tool and step['status'] == 'running':
                    step['status'] = 'error' if event.is_error else 'done'
                    step['result_preview'] = safe_text_preview(event.result_preview)
                    step['finished_at'] = self.now()
                    try:
                        started = datetime.fromisoformat(step.get('started_at') or step['at'])
                        finished = datetime.fromisoformat(step['finished_at'])
                        step['duration_ms'] = max(0, int((finished - started).total_seconds() * 1000))
                    except (TypeError, ValueError):
                        step['duration_ms'] = None
                    break
        else:
            return
        self.changed.set()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def running(self):
        self.data['status'] = 'running'
        self.changed.set()

    async def save(self):
        from claw.jobs.provider import current_execution
        ctx = current_execution.get()
        if ctx is not None:
            async with ctx.store.transaction() as db:
                await ctx.store._guard(db, ctx.lease)
        self.revision += 1
        self.data['updated_at'] = self.now()
        self.data['revision'] = self.revision
        # Copy before await so concurrent streaming cannot mutate a DB snapshot.
        snapshot = copy.deepcopy(self.data)
        await self.service.messages.save_mission_activity(self.session_id, self.key, snapshot)

    async def _run(self):
        try:
            await self.open()
        finally:
            self.ready.set()
        if not self.session_id:
            return
        while not self.closed:
            await self.changed.wait()
            await asyncio.sleep(0.5)
            self.changed.clear()
            if self.closed:
                break
            try:
                await asyncio.wait_for(self.save(), 3)
            except Exception:
                logger.warning('Mission activity checkpoint failed for {}', self.key)
        try:
            await asyncio.wait_for(self.save(), 3)
        except Exception:
            logger.warning('Mission activity final checkpoint failed for {}', self.key)

    async def close(self, result=None, status='interrupted'):
        """Test/support helper. Mission execution uses non-blocking ``finish``."""
        self.finish(result, status)
        if self.worker:
            await self.wait()
        elif self.session_id:
            await self.save()
