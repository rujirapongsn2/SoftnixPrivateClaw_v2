from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from sbot.api.bot_groups import router
from sbot.api.deps import current_user, get_state
from sbot.api.routes import router as sessions_router
from sbot.db.bot_groups import BotGroupStore
from sbot.providers.base import ChatResult, ToolCall
from sbot.tools.cos import DelegateTool, ListBotsTool
from tests.sbot_mode.conftest import FakeProvider, text_turn
from tests.sbot_mode.test_multi_bot_turn import make_runtime


async def team(stores):
    user = await stores['users'].get_or_create_by_email('group@sbot.test')
    bots = [await stores['bots'].create(owner_id=user.id, name=name, role_title=name,
                                        charter=f'You handle {name}.', tool_allowlist=[])
            for name in ['Leader', 'Engineer', 'Outsider']]
    return user, bots


@pytest.mark.asyncio
async def test_group_create_update_and_session_survive_reload(stores):
    user, bots = await team(stores)
    store = BotGroupStore(stores['sessions'].factory)
    group = await store.save(user.id, ' Project team ', [b.id for b in bots[:2]], bots[0].id)
    session = await stores['sessions'].get(group.session_id)
    assert session.kind == 'group' and session.group_id == group.id and session.bot_id is None
    updated = await store.save(user.id, 'Release team', [b.id for b in bots[1:]], bots[1].id, group.id)
    assert updated.session_id == group.session_id
    assert (await BotGroupStore(store.factory).get(group.id, user.id)).leader_id == bots[1].id
    assert (await stores['sessions'].get(group.session_id)).title == 'Release team'
    await stores['sessions'].delete(group.session_id)
    assert await store.get(group.id, user.id) is None


@pytest.mark.asyncio
async def test_membership_rejects_foreign_archived_duplicate_and_missing_leader(stores):
    user, bots = await team(stores)
    other = await stores['users'].get_or_create_by_email('other@sbot.test')
    foreign = await stores['bots'].create(owner_id=other.id, name='foreign')
    store = BotGroupStore(stores['sessions'].factory)
    for ids, leader in [([bots[0].id, foreign.id], bots[0].id), ([bots[0].id]*2, bots[0].id),
                        ([b.id for b in bots[:2]], bots[2].id)]:
        with pytest.raises(ValueError):
            await store.save(user.id, 'team', ids, leader)
    await stores['bots'].archive(bots[1].id, user.id)
    with pytest.raises(ValueError):
        await store.save(user.id, 'team', [b.id for b in bots[:2]], bots[0].id)
    assert await store.list_for_user(user.id) == []


@pytest.mark.asyncio
async def test_group_api_owner_scope_and_no_duplicate_session(stores, tmp_path):
    user, bots = await team(stores)
    other = await stores['users'].get_or_create_by_email('outsider@sbot.test')
    state = SimpleNamespace(sessions=stores['sessions'], bots=stores['bots'],
                            runtime=make_runtime(stores, FakeProvider([]), tmp_path))
    app = FastAPI()
    app.include_router(router)
    app.include_router(sessions_router)
    app.dependency_overrides[get_state] = lambda: state
    app.dependency_overrides[current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        data = {'name': 'Team', 'member_ids': [b.id for b in bots[:2]], 'leader_id': bots[0].id}
        response = await client.post('/api/bot-groups', json=data)
        assert response.status_code == 201, response.text
        group = response.json()
        opened = await client.post('/api/sessions', json={'kind': 'group', 'group_id': group['id']})
        assert opened.json()['id'] == group['session_id']
        assert len(await stores['sessions'].list_for_user(user.id)) == 1
        app.dependency_overrides[current_user] = lambda: other
        assert (await client.get('/api/bot-groups')).json() == []
        assert (await client.patch(f"/api/bot-groups/{group['id']}", json=data)).status_code == 404
        assert (await client.delete(f"/api/bot-groups/{group['id']}")).status_code == 404
        assert (await client.post('/api/sessions', json={'kind': 'group', 'group_id': group['id']})).status_code == 404
        app.dependency_overrides[current_user] = lambda: user
        assert (await client.delete(f"/api/bot-groups/{group['id']}")).status_code == 200
        assert await stores['sessions'].get(group['session_id']) is None


@pytest.mark.asyncio
async def test_group_delegation_stays_in_group_and_preserves_shared_transcript(stores, tmp_path):
    user, bots = await team(stores)
    group = await BotGroupStore(stores['sessions'].factory).save(
        user.id, 'Development', [b.id for b in bots[:2]], bots[0].id)
    provider = FakeProvider([
        [ChatResult(content='', tool_calls=[ToolCall(id='d1', name='delegate', arguments={
            'bot_id': bots[1].id, 'task': 'Build the feature', 'context': 'shared requirements',
        })])], text_turn('Feature implemented'), text_turn('Team result: implemented'),
        text_turn('Direct conversation'),
    ])
    runtime = make_runtime(stores, provider, tmp_path)
    answer = await runtime.handle_message(user.id, group.session_id, 'Engineer, build the feature')
    assert answer == 'Team result: implemented'
    assert 'delegate' in provider.offered_tools[0]
    assert 'create_bot' not in provider.offered_tools[0]
    prompt = str(provider.calls[0])
    assert 'Group chat: Development' in prompt and bots[1].id in prompt and bots[2].id not in prompt
    history = await stores['messages'].recent(group.session_id)
    assert any(m.get('content') == 'Team result: implemented' for m in history)
    # The group does not mirror assignments into a member's private chat.
    assert len(await stores['sessions'].list_for_user(user.id)) == 1
    direct = await stores['sessions'].thread_for_bot(user.id, bots[0].id, 'Leader')
    await runtime.handle_message(user.id, direct.id, 'Hello privately')
    assert 'delegate' not in provider.offered_tools[-1]
    assert 'Engineer, build the feature' not in str(provider.calls[-1])
    assert runtime.get_agent(user.id, bots[0].id).tools.get('delegate') is None


@pytest.mark.asyncio
async def test_group_tools_reject_outsider_by_id_or_name(stores, tmp_path):
    user, bots = await team(stores)
    ids = frozenset(b.id for b in bots[:2])
    delegate = DelegateTool(stores['bots'], user.id, FakeProvider([]), None, tmp_path,
                            leader_bot_id=bots[0].id, member_ids=ids)
    assert await delegate._resolve_target(bots[2].id, None) is None
    assert await delegate._resolve_target(None, bots[2].name) is None
    assert await delegate._resolve_target(bots[0].id, None) is None
    assert (await delegate._resolve_target(bots[1].id, None)).id == bots[1].id
    roster = await ListBotsTool(stores['bots'], user.id, member_ids=ids).execute()
    assert bots[2].id not in roster


@pytest.mark.asyncio
async def test_archived_group_leader_never_falls_back_to_cos(stores, tmp_path):
    user, bots = await team(stores)
    group = await BotGroupStore(stores['sessions'].factory).save(
        user.id, 'Team', [b.id for b in bots[:2]], bots[0].id)
    await stores['bots'].archive(bots[0].id, user.id)
    provider = FakeProvider([])
    result = await make_runtime(stores, provider, tmp_path).handle_message(user.id, group.session_id, 'work')
    assert 'Group unavailable' in result
    assert provider.calls == []


@pytest.mark.asyncio
async def test_same_leader_has_independent_group_rosters_and_refreshes_members(stores, tmp_path):
    user, bots = await team(stores)
    store = BotGroupStore(stores['sessions'].factory)
    first = await store.save(user.id, 'First', [bots[0].id, bots[1].id], bots[0].id)
    second = await store.save(user.id, 'Second', [bots[0].id, bots[2].id], bots[0].id)
    provider = FakeProvider([text_turn('first'), text_turn('second'), text_turn('updated')])
    runtime = make_runtime(stores, provider, tmp_path)
    await runtime.handle_message(user.id, first.session_id, 'Plan work')
    await runtime.handle_message(user.id, second.session_id, 'Plan other work')
    await store.save(user.id, 'First', [bots[0].id, bots[2].id], bots[0].id, first.id)
    await runtime.handle_message(user.id, first.session_id, 'Use the new member')
    systems = ['\n'.join(m['content'] for m in call if m['role'] == 'system') for call in provider.calls]
    assert bots[1].id in systems[0] and bots[2].id not in systems[0]
    assert bots[2].id in systems[1] and bots[1].id not in systems[1]
    assert bots[2].id in systems[2] and bots[1].id not in systems[2]
    assert all('spawn' not in tools and 'workflow' not in tools for tools in provider.offered_tools)


@pytest.mark.asyncio
async def test_group_reply_retains_coordinator_snapshot_after_leader_changes(stores, tmp_path):
    user, bots = await team(stores)
    store = BotGroupStore(stores['sessions'].factory)
    group = await store.save(user.id, 'Team', [b.id for b in bots[:2]], bots[0].id)
    runtime = make_runtime(stores, FakeProvider([text_turn('Original leader reply')]), tmp_path)
    await runtime.handle_message(user.id, group.session_id, 'Summarize')
    await store.save(user.id, 'Team', [b.id for b in bots[:2]], bots[1].id, group.id)
    from sqlalchemy import select

    from sbot.db.models import Message
    async with stores['sessions'].factory() as db:
        message = await db.scalar(select(Message).where(Message.session_id == group.session_id, Message.role == 'assistant'))
        assert message.meta['coordinator_bot_id'] == bots[0].id
        assert message.meta['speaker_name'] == bots[0].name
        assert message.speaker_bot_id is None  # retained in the leader's model history
