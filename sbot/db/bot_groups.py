"""Owner-scoped bot chat groups, each with one durable conversation."""
import uuid

from sqlalchemy import func, select, update

from sbot.db.models import Bot, BotChatGroup, ChatSession

MAX_MEMBERS = 12


class BotGroupStore:
    def __init__(self, factory):
        self.factory = factory

    async def list_for_user(self, owner_id):
        async with self.factory() as db:
            return list(await db.scalars(select(BotChatGroup).where(
                BotChatGroup.owner_id == owner_id
            ).order_by(BotChatGroup.created_at)))

    async def get(self, group_id, owner_id):
        async with self.factory() as db:
            return await db.scalar(select(BotChatGroup).where(
                BotChatGroup.id == group_id, BotChatGroup.owner_id == owner_id
            ))

    async def save(self, owner_id, name, member_ids, leader_id, group_id=None):
        name = name.strip()
        if not name or len(name) > 64:
            raise ValueError('Group name must contain 1–64 characters')
        if len(member_ids) != len(set(member_ids)) or not 2 <= len(member_ids) <= MAX_MEMBERS:
            raise ValueError(f'Choose 2–{MAX_MEMBERS} distinct bots')
        if leader_id not in member_ids:
            raise ValueError('Group leader must be a member')
        async with self.factory() as db:
            group = None
            if group_id:
                group = await db.scalar(select(BotChatGroup).where(
                    BotChatGroup.id == group_id, BotChatGroup.owner_id == owner_id
                ))
                if group is None:
                    raise LookupError('Group not found')
            bots = list(await db.scalars(select(Bot).where(
                Bot.owner_id == owner_id, Bot.id.in_(member_ids), Bot.is_archived.is_(False)
            )))
            if len(bots) != len(member_ids):
                raise ValueError('All members must be active bots owned by you')
            if group is None:
                count = await db.scalar(select(func.count()).select_from(BotChatGroup).where(
                    BotChatGroup.owner_id == owner_id
                ))
                if count >= 50:
                    raise ValueError('Maximum of 50 bot groups reached')
                gid = uuid.uuid4().hex
                session = ChatSession(user_id=owner_id, title=name, kind='group', group_id=gid)
                db.add(session)
                await db.flush()
                group = BotChatGroup(id=gid, owner_id=owner_id, session_id=session.id,
                                     name=name, member_ids=member_ids, leader_id=leader_id)
                db.add(group)
            else:
                group.name, group.member_ids, group.leader_id = name, member_ids, leader_id
                await db.execute(update(ChatSession).where(
                    ChatSession.id == group.session_id, ChatSession.user_id == owner_id
                ).values(title=name))
            await db.commit()
            return group
