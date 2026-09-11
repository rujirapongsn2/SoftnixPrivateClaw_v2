"""Sharing boundaries apply equally to discovery, tool reads and mutations."""
import pytest
from sqlalchemy import update
from claw.db.models import User, UserGroup
from claw.db.stores import SkillStore
from sbot.db.stores import SkillStore as SbotSkillStore
from claw.tools.skills import ReadSkillTool
from sbot.tools.skills import ReadSkillTool as SbotReadSkillTool
from tests.conftest_app import build_api_app, client
from tests.test_manage import _register, _bearer


@pytest.mark.parametrize('store_type,tool_type', [(SkillStore, ReadSkillTool), (SbotSkillStore, SbotReadSkillTool)])
async def test_sharing_access_and_revocation(db_factory, stores, store_type, tool_type):
    owner, peer, outsider = [await stores['users'].get_or_create_by_email(f'{n}@test.local') for n in ('owner', 'peer', 'outsider')]
    async with db_factory() as db:
        group = UserGroup(name='Engineering')
        db.add(group)
        await db.flush()
        await db.execute(update(User).where(User.id.in_([owner.id, peer.id])).values(group_id=group.id))
        await db.commit()
    skills = store_type(db_factory)
    private = await skills.upsert(owner.id, 'private-procedure', content='PRIVATE')
    shared = await skills.upsert(owner.id, 'team-procedure', content='TEAM', visibility='group')
    public = await skills.upsert(owner.id, 'public-procedure', content='PUBLIC', visibility='public')
    assert {s.id for s in await skills.available_for_user(peer.id)} == {shared.id, public.id}
    assert {s.id for s in await skills.available_for_user(outsider.id)} == {public.id}
    reader = tool_type(skills, peer.id)
    assert 'TEAM' in await reader.execute(name=shared.name)
    assert 'Error' in await reader.execute(name=private.name)
    assert not await skills.delete(peer.id, shared.id)
    # Saving the same name creates the caller's private copy, never overwrites the owner.
    await skills.upsert(peer.id, public.name, content='OWN')
    assert 'OWN' in await reader.execute(name=public.name)
    assert (await skills.get_by_name(owner.id, public.name)).content == 'PUBLIC'
    await skills.upsert(owner.id, shared.name, visibility='private')
    assert 'Error' in await reader.execute(name=shared.name)
    await skills.upsert(owner.id, shared.name, visibility='group')
    async with db_factory() as db:
        await db.execute(update(User).where(User.id == peer.id).values(group_id=None))
        await db.commit()
    assert 'Error' in await reader.execute(name=shared.name)
    await skills.upsert(owner.id, public.name, enabled=False)
    assert not await skills.available_for_user(outsider.id)
    with pytest.raises(ValueError, match='Join a group'):
        await skills.upsert(outsider.id, 'invalid-share', visibility='group')


@pytest.mark.parametrize("bot_mode", [False, True])
async def test_api_shared_skill_is_read_only(db_factory, bot_mode):
    app = build_api_app(db_factory)
    if bot_mode:
        from sbot.api.manage import router
        from sbot.api.deps import get_state as sbot_state, current_user as sbot_user
        from claw.api.deps import get_state, current_user
        app.router.routes = [r for r in app.router.routes if not getattr(r, "path", "").startswith("/api/skills")]
        app.include_router(router)
        app.dependency_overrides[sbot_state] = get_state
        app.dependency_overrides[sbot_user] = current_user
        app.state.claw.skills = SbotSkillStore(db_factory)
    async with client(app) as c:
        owner, _ = await _register(c, 'sharing-owner@example.com')
        peer, _ = await _register(c, 'sharing-peer@example.com')
        payload = {'name': 'shared-procedure', 'content': 'Original', 'visibility': 'public'}
        response = await c.put('/api/skills/shared-procedure', json=payload, headers=_bearer(owner))
        assert response.status_code == 200, response.text
        skill_id = response.json()['id']
        rows = (await c.get('/api/skills', headers=_bearer(peer))).json()
        shared = next(s for s in rows if s['id'] == skill_id)
        assert shared['read_only'] and shared['visibility'] == 'public'
        response = await c.put('/api/skills/shared-procedure', json={**shared, 'content': 'Changed'}, headers=_bearer(peer))
        assert response.status_code == 403
        assert (await c.delete(f'/api/skills/{skill_id}', headers=_bearer(peer))).status_code == 404
        # Old clients omitting visibility do not accidentally revoke sharing.
        await c.put('/api/skills/shared-procedure', json={'name': 'shared-procedure', 'content': 'Updated'}, headers=_bearer(owner))
        rows = (await c.get('/api/skills', headers=_bearer(peer))).json()
        assert next(s for s in rows if s['id'] == skill_id)['content'] == 'Updated'
        response = await c.put('/api/skills/shared-procedure', json={**payload, 'visibility': 'private'}, headers=_bearer(owner))
        assert response.status_code == 200
        assert skill_id not in {s['id'] for s in (await c.get('/api/skills', headers=_bearer(peer))).json()}


def test_visibility_migration_preserves_existing_private_skills():
    import importlib
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module("migrations.versions.a3d4e5f6b7c8_skill_visibility")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE skills (id VARCHAR(32) PRIMARY KEY, content TEXT)"))
        connection.execute(sa.text("INSERT INTO skills VALUES ('existing', 'private instructions')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT visibility, content FROM skills")).one() == ("private", "private instructions")
    engine.dispose()
