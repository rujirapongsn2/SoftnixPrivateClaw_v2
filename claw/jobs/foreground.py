"""Encrypted, revision-fenced pre-admission receipts. Never replays a tool."""
from sqlalchemy import select
from claw.jobs.models import ForegroundJournal
from claw.jobs.store import LeaseLost


async def save(store, journal_id, owner_id, session_id, mode, revision, payload):
    async with store.transaction() as db:
        row = await db.get(ForegroundJournal, journal_id)
        if row is None:
            if revision != 0 or mode not in {'privateclaw', 'sbot'}:
                raise LeaseLost('invalid foreground journal')
            if mode == 'sbot':
                from sbot.db.models import ChatSession
            else:
                from claw.db.models import ChatSession
            session = await db.get(ChatSession, session_id)
            if session is None or session.user_id != owner_id:
                raise PermissionError('foreground session ownership mismatch')
            row = ForegroundJournal(id=journal_id, owner_id=owner_id, session_id=session_id,
                mode=mode, revision=0, payload='', updated_at=store.clock())
            db.add(row)
        elif (row.owner_id != owner_id or row.session_id != session_id or row.mode != mode
              or row.revision != revision or row.status != 'open'):
            raise LeaseLost('foreground journal changed or already adopted')
        row.revision = revision + 1
        stamp = store.clock()
        row.payload, row.updated_at = store.pack({**payload, 'saved_at': stamp}), stamp
        return row.revision


async def recoverable(store, owner_id):
    """Inspection only: unknown side effects require reconciliation before replay."""
    async with store.factory() as db:
        rows = (await db.scalars(select(ForegroundJournal).where(
            ForegroundJournal.owner_id == owner_id, ForegroundJournal.status == 'open')
            .order_by(ForegroundJournal.updated_at).limit(100))).all()
        return [dict(id=r.id, revision=r.revision, mode=r.mode, session_id=r.session_id,
                     payload=store.unpack(r.payload)) for r in rows]


async def close(store, journal_id, owner_id, revision):
    async with store.transaction() as db:
        row = await db.get(ForegroundJournal, journal_id)
        if row is None or row.status == 'adopted':
            return
        payload = store.unpack(row.payload)
        if row.owner_id == owner_id and (payload.get('delivered') or payload.get('checkpoint', {}).get('terminal_delivery')):
            return  # retain pending delivery after an uncertain commit or exception
        if row.owner_id != owner_id or row.revision != revision or row.status != 'open':
            raise LeaseLost('foreground journal changed')
        row.status, row.revision, row.updated_at = 'closed', revision + 1, store.clock()


async def heartbeat(store, journal_id, owner_id):
    async with store.transaction() as db:
        row = await db.get(ForegroundJournal, journal_id)
        if row is None or row.owner_id != owner_id or row.status != 'open':
            return False
        row.updated_at = store.clock()
        return True


async def deliver(store, journal_id, owner_id, revision, *, orphan_before=None):
    """Commit transcript, known usage and terminal receipt in one transaction.

    No provider/tool calls: retrying after an uncertain commit is harmless. Unknown
    provider usage remains in the encrypted receipt for later reconciliation.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from sqlalchemy import func, update
    from claw.db.models import User
    from claw.jobs.contracts import digest
    async with store.transaction() as db:
        row = await db.get(ForegroundJournal, journal_id)
        if row is None or row.owner_id != owner_id:
            raise PermissionError('foreground journal ownership mismatch')
        payload = store.unpack(row.payload)
        if payload.get('delivered') and row.status == 'closed':
            return False
        if (row.status != 'open' or row.revision != revision
                or (orphan_before is not None and row.updated_at > orphan_before)):
            raise LeaseLost('foreground journal changed or active')
        terminal = payload.get('checkpoint', {}).get('terminal_delivery')
        if not isinstance(terminal, dict) or terminal.get('version') != 1:
            raise ValueError('unsupported terminal delivery')
        if row.mode == 'sbot':
            from sbot.db.models import ChatSession, Message
        else:
            from claw.db.models import ChatSession, Message
        await db.execute(update(ChatSession).where(ChatSession.id == row.session_id)
                         .values(updated_at=datetime.now(timezone.utc)))
        session = await db.get(ChatSession, row.session_id)
        user = await db.get(User, owner_id)
        if not user or not user.is_active or not session or session.user_id != owner_id:
            raise PermissionError('foreground delivery access revoked')
        if row.mode == 'sbot':
            from sbot.db.models import Bot, BotChatGroup
            actors = {e.get('speaker_bot_id') for e in terminal['entries']} - {None, ''}
            if session.bot_id:
                actors.add(session.bot_id)
            group = await db.scalar(select(BotChatGroup).where(BotChatGroup.session_id == row.session_id))
            if session.kind == 'group' and group is None:
                raise PermissionError('foreground group unavailable')
            for actor in actors:
                bot = await db.get(Bot, actor)
                if (not bot or bot.owner_id != owner_id or bot.is_archived
                        or (group and (group.owner_id != owner_id or actor not in group.member_ids))):
                    raise PermissionError('foreground bot delivery access revoked')
        seq = (await db.scalar(select(func.coalesce(func.max(Message.seq), 0))
                               .where(Message.session_id == row.session_id))) + 1
        for offset, entry in enumerate(terminal['entries']):
            extra = {'speaker_bot_id': entry.get('speaker_bot_id')} if row.mode == 'sbot' else {}
            db.add(Message(id=digest(f'foreground:{row.id}:{offset}')[:32],
                session_id=row.session_id, seq=seq + offset, role=entry['role'],
                content=entry.get('content') or '', tool_calls=entry.get('tool_calls'),
                tool_call_id=entry.get('tool_call_id'), tool_name=entry.get('name'),
                meta=entry.get('meta'), **extra))
        root = SimpleNamespace(id='foreground:' + row.id, mode=row.mode, owner_id=owner_id,
                               session_id=row.session_id, policy={})
        for receipt in payload['calls']:
            if receipt['actual'] is not None:
                await store._record_usage(db, root, SimpleNamespace(**receipt, fence=0),
                                          receipt['usage'], payload.get('count_plan_turn', False))
        from claw.db.models import UsageRecord
        first = await db.get(UsageRecord, digest('job-turn:' + root.id)[:32])
        if first is not None:
            for key in ('iterations', 'tool_calls', 'ttft_ms', 'duration_ms'):
                setattr(first, key, max(0, int(terminal.get('metrics', {}).get(key, 0) or 0)))
        payload['delivered'] = True
        row.payload = store.pack(payload)
        row.status, row.revision, row.updated_at = 'closed', row.revision + 1, store.clock()
        return True
