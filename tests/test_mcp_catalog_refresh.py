import asyncio
import importlib
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import anyio
import pytest
from mcp.server import Server
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import Tool


@pytest.fixture(params=['claw', 'sbot'])
def impl(request):
    module = importlib.import_module(f'{request.param}.core.connectors')
    registry = importlib.import_module(f'{request.param}.tools.registry').ToolRegistry
    return module, registry


def connector(name, owner='u'):
    return NS(name=name, owner_id=owner, id=name, updated_at=datetime.now(timezone.utc),
              kind='mcp', timeout_ms=None, transport='http', env={}, url='https://example.invalid')


def remote(name, description='old'):
    return NS(name=name, description=description, inputSchema={'type': 'object', 'properties': {}})


class Session:
    def __init__(self, tools):
        self.tools = tools
        self.lists = 0
        self.calls = 0
        self.fail = False
        self.gate = None

    async def list_tools(self, cursor=None):
        self.lists += 1
        if self.gate:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError('secret credential must never be logged')
        return NS(tools=self.tools, nextCursor=None)

    async def call_tool(self, *args, **kwargs):
        self.calls += 1
        return NS(isError=True, content=[NS(text='Unknown tool')])


class Store:
    def __init__(self, own, global_):
        self.own, self.global_ = own, global_

    async def enabled_for_user(self, user):
        return self.own

    async def enabled_for_global(self):
        return self.global_


async def until(check):
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(.001)


async def setup(impl, monkeypatch, own=None, globals_=None, interval=3600):
    mod, Registry = impl
    rows = (own or []) + (globals_ or [])
    sessions = {c.name: Session([remote('old')]) for c in rows}
    manager = mod.ConnectorManager(Store(own or [], globals_ or []))
    manager.tool_refresh_seconds = interval

    async def connect(stack, row):
        session = sessions[row.name]
        return session, await manager._list_catalog(session)

    monkeypatch.setattr(manager, '_connect_and_list', connect)
    registry = Registry()
    await manager.sync_tools('u', registry)
    return manager, registry, sessions


@pytest.mark.parametrize('scope', ['private', 'global'])
async def test_idle_hourly_multi_registry_schema_and_removal(impl, monkeypatch, scope):
    manager, first, sessions = await setup(
        impl, monkeypatch, own=[connector('personal')] if scope == 'private' else [],
        globals_=[connector('personal', None)] if scope == 'global' else [], interval=.02)
    second = impl[1]()
    await manager.sync_tools('u', second)
    old = first.get_registered('mcp_personal_old')
    sessions['personal'].tools = [remote('new', 'changed')]
    await until(lambda: first.has('mcp_personal_new'))
    assert not first.has('mcp_personal_old')
    assert second.get_registered('mcp_personal_new') is first.get_registered('mcp_personal_new')
    assert 'changed' in first.get('mcp_personal_new').description
    assert old._session is sessions['personal']
    await manager.shutdown()
    assert not second.has('mcp_personal_new')


async def test_error_refresh_never_replays_call_and_failure_retains_catalog(impl, monkeypatch):
    manager, registry, sessions = await setup(impl, monkeypatch, own=[connector('personal')])
    session = sessions['personal']
    session.tools = [remote('new')]
    assert 'Unknown tool' in await registry.get('mcp_personal_old').execute()
    await until(lambda: registry.has('mcp_personal_new'))
    assert session.calls == 1
    good = registry.get('mcp_personal_new')
    session.fail = True
    worker = manager._users['u'].workers['personal']
    worker.wake.set()
    await until(lambda: 'refresh_error' in manager._users['u'].statuses['personal'])
    assert registry.get('mcp_personal_new') is good
    before = session.lists
    worker.wake.set()
    await asyncio.sleep(.03)
    assert session.lists == before
    await manager.shutdown()
    assert worker.task.done()


async def test_global_collision_reveals_private_and_restricted_registry(impl, monkeypatch):
    manager, registry, sessions = await setup(
        impl, monkeypatch, own=[connector('a_b')], globals_=[connector('a', None)])
    # a + b_run and a_b + run occupy the same public name.
    sessions['a'].tools = [remote('b_run', 'global')]
    sessions['a_b'].tools = [remote('run', 'personal')]
    for state in (manager._global, manager._users['u']):
        for worker in state.workers.values():
            worker.wake.set()
    await until(lambda: registry.get_registered('mcp_a_b_run') is not None)
    if hasattr(registry, 'restrict_to'):
        registry.restrict_to([])
        assert registry.get('mcp_a_b_run') is None
    await until(lambda: 'personal' in manager._users['u'].catalogs['a_b'][0].description)
    assert 'global' in registry.get_registered('mcp_a_b_run').description
    sessions['a'].tools = []
    manager._global.workers['a'].wake.set()
    await until(lambda: 'personal' in registry.get_registered('mcp_a_b_run').description)
    if hasattr(registry, 'restrict_to'):
        assert registry.get('mcp_a_b_run') is None
        registry.restrict_to(['mcp_a_b_run'])
        assert registry.has('mcp_a_b_run')
    await manager.shutdown()
    assert registry.get_registered('mcp_a_b_run') is None


async def test_paginated_initial_catalog_and_stale_publication(impl, monkeypatch):
    mod, _ = impl
    manager, registry, sessions = await setup(impl, monkeypatch, own=[connector('p')])
    session = sessions['p']
    cursors = []

    async def pages(cursor=None):
        cursors.append(cursor)
        return NS(tools=[remote('one' if cursor is None else 'two')],
                  nextCursor='next' if cursor is None else None)

    monkeypatch.setattr(session, 'list_tools', pages)
    listed = await manager._list_catalog(session)
    assert [t.name for t in listed.tools] == ['one', 'two']
    assert cursors == [None, 'next']
    session.gate = asyncio.Event()
    monkeypatch.undo()
    worker = manager._users['u'].workers['p']
    worker.wake.set()
    await until(lambda: session.lists == 2)
    await manager.shutdown()
    session.gate.set()
    await asyncio.sleep(.01)
    assert not registry.has('mcp_p_one')
    assert not registry.has('mcp_p_old')


async def test_real_sdk_notification_refresh_does_not_deadlock(impl, monkeypatch):
    mod, Registry = impl
    server = Server('catalog-test')
    catalog = [Tool(name='old', description='old', inputSchema={'type': 'object'})]
    server_session = None
    lists = 0

    @server.list_tools()
    async def tools():
        nonlocal server_session, lists
        lists += 1
        server_session = server.request_context.session
        # A second notification arrives during discovery and must survive it.
        if lists == 2:
            await server_session.send_tool_list_changed()
        return catalog

    async with create_client_server_memory_streams() as (client, streams):
        async with anyio.create_task_group() as group:
            group.start_soon(server.run, *streams, server.create_initialization_options())

            @asynccontextmanager
            async def transport(*args, **kwargs):
                yield *client, None

            monkeypatch.setattr('mcp.client.streamable_http.streamablehttp_client', transport)
            manager = mod.ConnectorManager(Store([], []))
            row = connector('sdk', None)
            async with AsyncExitStack() as stack:
                session, listed = await manager._connect_and_list(stack, row)
                state = mod._UserConnections(own_names={'sdk'})
                state.registries.add(Registry())
                registry = Registry()
                state.registries.add(registry)
                manager._users['u'] = state
                state.statuses['sdk'] = {'status': 'connected'}
                worker = manager._start_catalog_worker(row, session, state, user_id='u')
                catalog[:] = [Tool(name='new', description='new schema', inputSchema={
                    'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']})]
                await server_session.send_tool_list_changed()
                await until(lambda: lists >= 3 and registry.has('mcp_sdk_new'))
                assert registry.get('mcp_sdk_new').parameters['required'] == ['value']
                assert worker.session is session
                await manager.shutdown()
            group.cancel_scope.cancel()


async def test_reconnect_lists_again_and_preserves_all_tracked_registries(impl, monkeypatch):
    manager, first, sessions = await setup(impl, monkeypatch, own=[connector('p')])
    second = impl[1]()
    await manager.sync_tools('u', second)
    old_worker = manager._users['u'].workers['p']
    old_session = sessions['p']
    replacement = Session([remote('replacement')])
    sessions['p'] = replacement
    await manager.invalidate('u')
    await manager.sync_tools('u', first)
    assert old_worker.task.done()
    assert replacement.lists == 1
    assert old_session.lists == 1
    assert first.has('mcp_p_replacement') and second.has('mcp_p_replacement')
    assert not second.has('mcp_p_old')
    await manager.shutdown()


async def test_builtin_ownership_and_personal_same_name_shadowing(impl, monkeypatch):
    mod, Registry = impl
    manager, registry, sessions = await setup(
        impl, monkeypatch, own=[connector('same')], globals_=[connector('same', None)])
    assert manager._users['u'].tools[0]._session is sessions['same']
    builtin = mod.McpToolProxy(Session([]), 'same', 'new', 'builtin', {})
    registry.register(builtin)
    sessions['same'].tools = [remote('new', 'remote')]
    manager._users['u'].workers['same'].wake.set()
    await until(lambda: manager._users['u'].catalogs['same'][0].name == builtin.name)
    assert registry.get_registered(builtin.name) is builtin
    await manager.shutdown()
    assert registry.get_registered(builtin.name) is builtin


@pytest.mark.parametrize('message,code,expected', [
    ('Unknown tool', None, True), ('input schema validation failed', None, True),
    ('bad parameters', -32602, True), ('unauthorized', 401, False),
    ('network timeout', 408, False), ('business operation rejected', None, False),
])
def test_catalog_error_classification(impl, message, code, expected):
    assert impl[0]._catalog_error(message, code) is expected


async def test_superseded_worker_cannot_publish(impl, monkeypatch):
    mod, _ = impl
    manager, registry, sessions = await setup(impl, monkeypatch, own=[connector('p')])
    session = sessions['p']
    session.tools = [remote('stale')]
    session.gate = asyncio.Event()
    state = manager._users['u']
    old = state.workers['p']
    old.wake.set()
    await until(lambda: session.lists == 2)
    state.workers['p'] = mod._CatalogWorker(Session([]), asyncio.Event())
    session.gate.set()
    await until(old.task.done)
    assert registry.has('mcp_p_old')
    assert not registry.has('mcp_p_stale')
    await manager.shutdown()


async def test_refresh_timeout_retains_catalog_and_shutdown_cancels_retry(impl, monkeypatch):
    manager, registry, sessions = await setup(impl, monkeypatch, own=[connector('p')])
    manager.connect_timeout_seconds = .01
    session = sessions['p']
    session.gate = asyncio.Event()
    worker = manager._users['u'].workers['p']
    worker.wake.set()
    await until(lambda: 'refresh_error' in manager._users['u'].statuses['p'])
    assert registry.has('mcp_p_old')
    before = session.lists
    await manager.shutdown()
    session.gate.set()
    await asyncio.sleep(.02)
    assert worker.task.done()
    assert session.lists == before


async def test_expired_global_collision_owner_is_not_republished(impl, monkeypatch):
    manager, registry, sessions = await setup(
        impl, monkeypatch, globals_=[connector('a', None), connector('a_b', None)])
    name = 'mcp_a_b_run'
    sessions['a'].tools = [remote('b_run', 'first credentials')]
    sessions['a_b'].tools = [remote('run', 'second credentials')]
    for worker in manager._global.workers.values():
        worker.wake.set()
    await until(lambda: registry.has(name))
    await until(lambda: manager._global.proxies['a_b'][0]._remote_name == 'run')
    assert registry.get(name)._session is sessions['a']

    async def expired(*args, **kwargs):
        sessions['a'].calls += 1
        return NS(isError=True, content=[NS(text='token has been expired')])

    monkeypatch.setattr(sessions['a'], 'call_tool', expired)
    assert 'token has been expired' in await registry.execute(name, {})
    assert manager._global.statuses['a']['status'] == 'reauthorization_required'
    sessions['a_b'].tools = [remote('run', 'second credentials refreshed')]
    manager._global.workers['a_b'].wake.set()
    await until(lambda: 'second credentials refreshed' in manager._global.proxies['a_b'][0].description)
    assert registry.get(name)._session is sessions['a_b']
    assert registry.get(name)._session_ref() is sessions['a_b']
    assert manager._global.statuses['a']['tool_names'] == []
    assert manager._global.statuses['a']['tools'] == 0
    assert manager._global.statuses['a_b']['tool_names'] == [name]
    await registry.execute(name, {})
    assert sessions['a'].calls == 1
    assert sessions['a_b'].calls == 1
    await manager.shutdown()


@pytest.mark.parametrize('scope', ['private', 'global'])
async def test_local_validation_rejection_refreshes_without_executing(impl, monkeypatch, scope):
    manager, registry, sessions = await setup(
        impl, monkeypatch, own=[connector('p')] if scope == 'private' else [],
        globals_=[connector('p', None)] if scope == 'global' else [])
    session = sessions['p']
    old = registry.get('mcp_p_old')
    old.parameters = {'type': 'object', 'properties': {'removed': {'type': 'string'}},
                      'required': ['removed']}
    session.tools = [remote('old', 'new schema')]
    expected = "Error: invalid parameters for 'mcp_p_old': " + '; '.join(old.validate_params({}))
    result = await registry.execute('mcp_p_old', {})
    assert result == expected + '\n\n[Analyze the error above and try a different approach.]'
    assert session.calls == 0
    await until(lambda: registry.get('mcp_p_old') is not old)
    assert session.lists == 2
    assert session.calls == 0
    assert registry.get('mcp_p_old').validate_params({}) == []
    await registry.execute('mcp_p_old', {})
    assert session.calls == 1
    await manager.shutdown()
