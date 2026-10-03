"""File lifecycle: zones, sweep, trash, reconcile. Real isolated SQLite stores, temp workspaces only."""
import json
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import event, select

import claw.workspace.lifecycle as lifecycle
from claw.db.models import AuditEvent
from claw.workspace.cleanup import WorkspaceCleanupService
from claw.workspace.lifecycle import FileConflict, FileLifecycle, Zone, control_root, zone_of
from claw.workspace.policy import WorkspacePolicy
from tests.conftest_app import build_api_app, client

DAY = 86400


async def setup(c, email='owner@example.test'):
    response = await c.post('/api/auth/register', json={'email': email, 'password': 'password123'})
    user = response.json()
    headers = {'Authorization': 'Bearer ' + user['access_token']}
    response = await c.post('/api/sessions', json={'title': 'ไฟล์'}, headers=headers)
    return user['user']['id'], headers, response.json()['id']


def write(app, uid, path, content='ข้อมูลภาษาไทย café', *, days_old=0.0, now=None):
    target = app.state.claw.settings.workspaces_root / uid / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    if days_old or now is not None:
        stamp = (time.time() if now is None else now) - days_old * DAY
        os.utime(target, (stamp, stamp))
    return target


def enforcing(**overrides):
    return WorkspacePolicy(**{'enforce': True, **overrides})


async def owner_app(db_factory, tmp_path, email='owner@example.test'):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, _, sid = await setup(c, email)
    return app, uid, sid, FileLifecycle(app.state.claw, 'privateclaw')


def trash_dir(tmp_path, uid):
    return control_root(tmp_path / 'ws', uid)


def tree(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob('*')}


@pytest.mark.parametrize('mode, path, zone', [
    ('privateclaw', 'report.xlsx', Zone.AI),
    ('privateclaw', 'outputs/q3/report.xlsx', Zone.AI),
    ('privateclaw', 'downloads/video.mp4', Zone.AI),
    ('privateclaw', 'browser/shot-1.png', Zone.AI),
    ('privateclaw', 'uploads/generated-1.png', Zone.AI),
    ('privateclaw', 'uploads/ab12cd34-in.pdf', Zone.USER),
    ('privateclaw', 'uploads/nested/generated-2.png', Zone.AI),
    ('privateclaw', 'uploads/nested/in.pdf', Zone.USER),
    ('privateclaw', '.tmp/scratch.py', Zone.SCRATCH),
    ('privateclaw', '.tmp/deep/x.bin', Zone.SCRATCH),
    ('privateclaw', '.claw_skills/pdf/SKILL.md', Zone.SYSTEM),
    ('privateclaw', '.cache/pip/x', Zone.SYSTEM),
    ('privateclaw', 'blueprints/a/plan.md', Zone.SYSTEM),
    ('privateclaw', 'skills/mine/SKILL.md', Zone.SYSTEM),
    ('privateclaw', 'outputs/.hidden', Zone.AI),
    ('sbot', 'report.xlsx', Zone.SYSTEM),
    ('sbot', 'projects/app/main.py', Zone.SYSTEM),
    ('sbot', 'uploads/generated-1.png', Zone.SYSTEM),
    ('sbot', 'uploads/ab12cd34-in.pdf', Zone.USER),
    ('sbot', '.tmp/scratch.py', Zone.SCRATCH),
    ('sbot', '.assignments/x/inputs/a.txt', Zone.SYSTEM),
])
def test_zone_of(mode, path, zone):
    assert zone_of(mode, path) is zone


async def test_sweep_trashes_expired_ai_files_even_when_a_chat_mentions_them(db_factory, tmp_path):
    app, uid, sid, life = await owner_app(db_factory, tmp_path)
    mentioned = write(app, uid, 'รายงาน café.xlsx', days_old=31)
    chip = write(app, uid, 'outputs/chart.png', days_old=40)
    fresh = write(app, uid, 'fresh.txt', days_old=29)
    system = [write(app, uid, p, days_old=400) for p in ('skills/s/SKILL.md', 'blueprints/b.md', '.claw_skills/x')]
    old_upload = write(app, uid, 'uploads/ab12cd34-in.pdf', days_old=8)
    new_upload = write(app, uid, 'uploads/ab12cd35-in.pdf', days_old=6)
    await app.state.claw.messages.append(sid, [{
        'role': 'assistant', 'content': 'ดู รายงาน café.xlsx', 'meta': {'artifacts': ['outputs/chart.png']},
        'tool_calls': [{'function': {'name': 'write_file', 'arguments': json.dumps({'path': 'รายงาน café.xlsx'})}}],
    }])

    observed = await life.sweep(uid, enforcing(enforce=False), now=time.time())
    assert (observed.trashed, sorted(observed.paths)) == (3, sorted(['รายงาน café.xlsx', 'outputs/chart.png',
                                                                      'uploads/ab12cd34-in.pdf']))
    assert mentioned.exists() and chip.exists() and old_upload.exists()
    assert life.list_trash(uid, retention_days=30)['files'] == []

    result = await life.sweep(uid, enforcing(), now=time.time())
    assert result.trashed == 3
    assert not mentioned.exists() and not chip.exists() and not old_upload.exists()
    assert fresh.exists() and new_upload.exists() and all(p.exists() for p in system)
    rules = {e['path']: e['rule'] for e in life.list_trash(uid, retention_days=30)['files']}
    assert rules == {'รายงาน café.xlsx': 'ai:30d', 'outputs/chart.png': 'ai:30d', 'uploads/ab12cd34-in.pdf': 'user:7d'}

    write(app, uid, 'uploads/ab12cd36-in.pdf', days_old=8)
    assert (await life.sweep(uid, enforcing(uploads_retention_days=0), now=time.time())).trashed == 0


async def test_sweep_audits_once_per_owner_and_service_reports_observe_only(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    for i in range(3):
        write(app, uid, f'old-{i}.txt', days_old=31)
    policy = enforcing(enforce=False)

    class Policies:
        async def effective(self):
            return policy

    service = WorkspaceCleanupService(life, Policies(), app.state.claw.settings)
    assert (await service.run_once()).trashed == 3
    assert len(life.list_files(uid, policy)['files']) == 3
    policy = enforcing()
    assert (await service.run_once()).trashed == 3
    async with db_factory() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind.like('%lifecycle%')))).all()
    assert [(e.kind, e.payload['trashed'], e.user_id) for e in events] == [('workspace_lifecycle', 3, uid)]
    async with db_factory() as db:
        assert not (await db.scalars(select(AuditEvent).where(AuditEvent.kind == 'file_trashed'))).all()


async def test_active_work_skips_only_the_owners_ai_zone(db_factory, tmp_path):
    from claw.db.stores import ArtifactJobStore

    app, uid, sid, life = await owner_app(db_factory, tmp_path)
    ai = write(app, uid, 'report.pdf', days_old=31)
    scratch = write(app, uid, '.tmp/x.py', days_old=8)
    jobs = ArtifactJobStore(db_factory)
    await jobs.create({'id': 'running', 'user_id': uid, 'status': 'running', 'session_id': sid})
    await jobs.create({'id': 'other-owner', 'user_id': 'f' * 32, 'status': 'running', 'session_id': sid})

    assert (await life.sweep(uid, enforcing(), now=time.time())).trashed == 1
    assert ai.exists() and not scratch.exists()
    with pytest.raises(FileConflict):
        await life.trash(uid, 'report.pdf', actor=uid)

    await jobs.finish('running', 'completed')
    assert (await life.sweep(uid, enforcing(), now=time.time())).trashed == 1
    assert not ai.exists()


async def test_sweep_cost_does_not_grow_with_chat_history(db_factory, tmp_path):
    app, uid, sid, life = await owner_app(db_factory, tmp_path)
    await app.state.claw.messages.append(sid, [
        {'role': 'assistant', 'content': f'file-{i % 200}.txt', 'meta': {'artifacts': [f'file-{i % 200}.txt']}}
        for i in range(10_000)
    ])
    for i in range(200):
        write(app, uid, f'file-{i}.txt', days_old=31)
    statements = []
    engine = db_factory.kw['bind'].sync_engine
    listener = lambda *args: statements.append(args[2])  # noqa: E731
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        started = time.perf_counter()
        result = await life.sweep(uid, enforcing(), now=time.time())
        elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    print(f'\nsweep of 200 expired files with 10,000 messages: {len(statements)} SQL statements, {elapsed:.3f}s')
    assert result.trashed == 200
    assert len(statements) <= 12
    assert not any('messages' in s.lower() for s in statements)
    assert elapsed < 2


async def test_trash_ttl_and_cap_purge_oldest_first_and_remove_entries(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    now = time.time()
    for day, size in ((40, 1), (20, 600_000), (10, 600_000), (5, 600_000)):
        write(app, uid, f'day-{day}.bin', 'x' * size, now=now - day * DAY - 31 * DAY)
        await life.sweep(uid, enforcing(trash_cap_mb=0, trash_retention_days=3650), now=now - day * DAY)
    paths = lambda: [e['path'] for e in life.list_trash(uid, retention_days=30)['files']]  # noqa: E731
    assert paths() == ['day-5.bin', 'day-10.bin', 'day-20.bin', 'day-40.bin']

    result = await life.sweep(uid, enforcing(trash_cap_mb=1), now=now)
    assert result.purged == 3
    assert paths() == ['day-5.bin']
    control = trash_dir(tmp_path, uid)
    assert len([p for p in control.iterdir() if p.name != 'index']) == 1
    assert len(list((control / 'index').iterdir())) == 1
    async with db_factory() as db:
        audit = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == 'workspace_lifecycle'))).all()
    assert audit[-1].payload['purged'] == 3


async def test_restore_refreshes_mtime_keeps_bytes_and_mode_and_is_not_swept_again(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    file = write(app, uid, 'รายงาน/café.txt', days_old=60)
    file.chmod(0o640)
    await life.sweep(uid, enforcing(), now=time.time())
    entry = life.list_trash(uid, retention_days=30)['files'][0]
    before = time.time()
    await life.restore(uid, entry['id'])
    assert file.read_text(encoding='utf-8') == 'ข้อมูลภาษาไทย café'
    assert file.stat().st_mode & 0o777 == 0o640
    assert file.stat().st_mtime >= before - 1
    assert (await life.sweep(uid, enforcing(), now=time.time())).trashed == 0
    assert file.exists()
    assert life.list_trash(uid, retention_days=30) == {'files': [], 'total': 0}


async def test_trash_restore_audits_owner_over_the_api(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        path = 'รายงาน/café.txt'
        file = write(app, uid, path)
        response = await c.post('/api/files/trash', headers=headers, json={'path': path})
        assert response.status_code == 200
        entry = response.json()
        assert entry['actor'] == uid and entry['rule'] == 'user' and not file.exists()
        listing = (await c.get('/api/files/trash', headers=headers)).json()
        assert listing['total'] == 1 and listing['retention_days'] == 30 and listing['cap_mb'] == 1024
        assert listing['permanent_delete_enabled'] is True
        assert listing['files'][0]['id'] == entry['id']
        assert listing['files'][0]['expires_at'] == pytest.approx(entry['at'] + 30 * DAY)
        assert (await c.post('/api/files/trash/' + entry['id'] + '/restore', headers=headers)).status_code == 200
        assert file.read_text(encoding='utf-8') == 'ข้อมูลภาษาไทย café'
        assert (await c.get('/api/files/trash', headers=headers)).json()['files'] == []
        files = (await c.get('/api/files', headers=headers)).json()
        assert files['truncated'] is False
        assert files['files'] == [{'path': path, 'size': file.stat().st_size, 'modified_at': file.stat().st_mtime,
                                   'zone': 'ai', 'expires_at': pytest.approx(file.stat().st_mtime + 30 * DAY)}]
    async with db_factory() as db:
        events = (await db.scalars(select(AuditEvent))).all()
    assert {'file_trashed', 'file_restored'} <= {event.kind for event in events}
    assert all(e.user_id == uid for e in events if e.kind.startswith('file_'))


async def test_chat_deletion_keeps_files(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, sid = await setup(c)
        path = 'uploads/generated-รายงาน café.txt'
        file = write(app, uid, path)
        await app.state.claw.messages.append(sid, [{'role': 'assistant', 'content': 'รายงาน',
                                                  'meta': {'artifacts': [path]}}])
        assert (await c.delete(f'/api/sessions/{sid}', headers=headers)).status_code == 200
        assert await app.state.claw.messages.recent(sid) == []
        assert file.read_text(encoding='utf-8') == 'ข้อมูลภาษาไทย café'
        assert (await c.get('/api/files/download/' + path, headers=headers)).text == 'ข้อมูลภาษาไทย café'
        _, other_headers, _ = await setup(c, 'other@example.test')
        assert (await c.get('/api/files/download/' + path, headers=other_headers)).status_code == 404


async def test_symlinks_escapes_and_other_tenant_trash_rejected(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        file = write(app, uid, 'normal.txt')
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'secret.txt').write_text('untouched')
        (file.parent / 'link').symlink_to(outside / 'secret.txt')
        (file.parent / 'dirlink').symlink_to(outside)
        for path in ['../outside/secret.txt', 'link', 'dirlink/secret.txt', '/etc/passwd', '.env', 'a\x00b']:
            assert (await c.post('/api/files/trash', headers=headers, json={'path': path})).status_code == 404
        entry = (await c.post('/api/files/trash', headers=headers, json={'path': 'normal.txt'})).json()
        _, foreign, _ = await setup(c, 'foreign@example.test')
        assert (await c.post(f"/api/files/trash/{entry['id']}/restore", headers=foreign)).status_code == 404
        assert (await c.post(f"/api/files/trash/{entry['id']}/purge", headers=foreign,
                             json={'confirm_permanent': True, 'expected_path': 'normal.txt'})).status_code == 404
        assert (outside / 'secret.txt').read_text() == 'untouched'
        assert (file.parent / 'link').is_symlink() and (file.parent / 'dirlink').is_symlink()


async def test_a_parent_swapped_for_a_symlink_mid_move_cannot_redirect_it(db_factory, tmp_path, monkeypatch):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    ws = tmp_path / 'ws' / uid
    write(app, uid, 'sub/victim.txt', 'original')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'victim.txt').write_text('outside')
    real_rename = os.rename
    swaps = []

    def swap_then_rename(src, dst, **kwargs):
        if not swaps:
            swaps.append(src)
            real_rename(ws / 'sub', ws / 'sub-moved')
            os.symlink(outside, ws / 'sub')
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, 'rename', swap_then_rename)
    entry = await life.trash(uid, 'sub/victim.txt', actor=uid)
    monkeypatch.undo()
    print(f'\nrace: outside file kept {(outside / "victim.txt").read_text()!r}, '
          f'moved file came from the original inode: {not (ws / "sub-moved" / "victim.txt").exists()}')
    assert (outside / 'victim.txt').read_text() == 'outside'
    assert not (ws / 'sub-moved' / 'victim.txt').exists()
    assert (trash_dir(tmp_path, uid) / entry['id'] / 'data').read_text() == 'original'
    with pytest.raises(FileNotFoundError):
        await life.restore(uid, entry['id'])  # the original parent is now a symlink
    assert (outside / 'victim.txt').read_text() == 'outside'
    assert life.list_trash(uid, retention_days=30)['total'] == 1


async def test_a_file_swapped_for_a_symlink_mid_move_is_dropped_not_followed(db_factory, tmp_path, monkeypatch):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    ws = tmp_path / 'ws' / uid
    write(app, uid, 'victim.txt', 'original')
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside')
    real_rename = os.rename
    swaps = []

    def swap_then_rename(src, dst, **kwargs):
        if not swaps:
            swaps.append(src)
            (ws / 'victim.txt').unlink()
            os.symlink(outside, ws / 'victim.txt')
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, 'rename', swap_then_rename)
    with pytest.raises(FileConflict):
        await life.trash(uid, 'victim.txt', actor=uid)
    monkeypatch.undo()
    assert outside.read_text() == 'outside'
    assert not os.path.lexists(ws / 'victim.txt')
    assert life.list_trash(uid, retention_days=30)['total'] == 0


async def test_reconcile_converges_from_every_crash_point(db_factory, tmp_path, monkeypatch):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    control = trash_dir(tmp_path, uid)

    class Crash(Exception):
        pass

    async def crash_during(fn, target, name, replacement):
        monkeypatch.setattr(target, name, replacement)
        with pytest.raises(Crash):
            await fn()
        monkeypatch.undo()

    async def converged():
        await life.reconcile()
        once = tree(control)
        await life.reconcile()
        assert tree(control) == once
        return once

    def raise_crash(*args, **kwargs):
        raise Crash

    real_write = lifecycle._write_json

    def crash_on(status):
        def write_json(path, value, **kwargs):
            if value.get('status') == status:
                raise Crash
            real_write(path, value, **kwargs)
        return write_json

    def crash_after(status):
        def write_json(path, value, **kwargs):
            real_write(path, value, **kwargs)
            if value.get('status') == status:
                raise Crash
        return write_json

    prepared = write(app, uid, 'prepared.txt')
    await crash_during(lambda: life.trash(uid, 'prepared.txt', actor=uid), os, 'rename', raise_crash)
    assert prepared.exists() and await converged() == set()

    moved = write(app, uid, 'moved.txt')
    await crash_during(lambda: life.trash(uid, 'moved.txt', actor=uid), lifecycle, '_write_json', crash_on('trashed'))
    assert not moved.exists()
    state = await converged()
    entry = life.list_trash(uid, retention_days=30)['files'][0]
    assert entry['path'] == 'moved.txt' and any(name.startswith('index/') for name in state)
    assert life.archived(uid, ['moved.txt']) == {'moved.txt': entry['id']}

    await crash_during(lambda: life.purge(uid, entry['id'], confirmed=True, expected_path='moved.txt'),
                       lifecycle, '_write_json', crash_after('purge_requested'))
    assert await converged() == {'index'}
    assert life.list_trash(uid, retention_days=30)['files'] == []

    restored = write(app, uid, 'restored.txt')
    entry = await life.trash(uid, 'restored.txt', actor=uid)
    await crash_during(lambda: life.restore(uid, entry['id']), lifecycle, '_drop_entry', raise_crash)
    assert restored.exists() and await converged() == {'index'}
    assert restored.read_text(encoding='utf-8') == 'ข้อมูลภาษาไทย café'


async def test_reconcile_rekeys_legacy_random_ids(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    write(app, uid, 'old.txt')
    entry = await life.trash(uid, 'old.txt', actor=uid)
    control = trash_dir(tmp_path, uid)
    legacy = 'f' * 32
    (control / entry['id']).rename(control / legacy)
    record = control / legacy / 'record.json'
    record.write_text(json.dumps({**entry, 'id': legacy, 'at': time.time() - 31 * DAY}))
    await life.reconcile()
    [listed] = life.list_trash(uid, retention_days=30)['files']
    assert listed['id'] != legacy and listed['path'] == 'old.txt'
    assert (await life.sweep(uid, enforcing(), now=time.time())).purged == 1


async def test_one_year_of_daily_use_stays_bounded(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    ws = tmp_path / 'ws' / uid
    start = time.time() - 400 * DAY
    policy = enforcing(trash_cap_mb=1)
    day_bytes = 10_000 + 2_000 + 3_000
    totals = {}
    for day in range(1, 366):
        now = start + day * DAY
        write(app, uid, f'outputs/day-{day}.bin', 'a' * 10_000, now=now)
        if day % 3 == 0:
            write(app, uid, f'.tmp/scratch-{day}.py', 'b' * 2_000, now=now)
        if day % 5 == 0:
            write(app, uid, f'uploads/{day:08x}-in.pdf', 'c' * 3_000, now=now)
        await life.sweep(uid, policy, now=now)
        live = sum(p.stat().st_size for p in ws.rglob('*') if p.is_file())
        trash = sum(p.stat().st_size for p in trash_dir(tmp_path, uid).glob('*/data'))
        totals[day] = (live, trash)
    print(f'\nsoak: day 200 live+trash={sum(totals[200])} bytes, day 365 live+trash={sum(totals[365])} bytes, '
          f'max trash={max(t for _, t in totals.values())} bytes, cap={1024 * 1024}')
    assert sum(totals[365]) <= sum(totals[200]) + day_bytes
    assert max(t for _, t in totals.values()) <= 1024 * 1024
    assert life.list_trash(uid, retention_days=30)['total'] <= 31 + 31


async def test_job_and_recovery_records_mark_active_work(db_factory, tmp_path):
    from claw.db.stores import ArtifactJobStore
    from claw.jobs.contracts import StepOutcome
    from claw.jobs.store import JobStore
    from claw.security.crypto import SecretBox

    app, uid, sid, life = await owner_app(db_factory, tmp_path)
    jobs = JobStore(db_factory, SecretBox('isolated-test-key'))
    await jobs.initialize()
    await jobs.submit('guard-job', uid, sid, 'privateclaw', [{'id': 'step', 'executor': 'fake', 'inputs': {}}])
    assert await life.active_work(uid)
    lease = await jobs.claim('test-worker', {'fake'})
    await jobs.settle(lease, StepOutcome('completed', evidence={}), validated=True)
    assert not await life.active_work(uid)
    await ArtifactJobStore(db_factory).create({'id': 'recover', 'user_id': uid, 'status': 'blocked', 'session_id': sid})
    assert await life.active_work(uid)


async def test_a_job_stuck_for_a_week_stops_pausing_cleanup(db_factory, tmp_path):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from claw.db.models import AppSetting
    from claw.db.stores import ArtifactJobStore

    app, uid, sid, life = await owner_app(db_factory, tmp_path)
    await ArtifactJobStore(db_factory).create({'id': 'stuck', 'user_id': uid, 'status': 'blocked', 'session_id': sid})
    assert await life.active_work(uid)
    async with db_factory() as db:
        await db.execute(update(AppSetting).where(AppSetting.key == 'artifact_job:stuck').values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=8)))
        await db.commit()
    assert not await life.active_work(uid)


async def test_guard_and_audit_failures_do_not_remove_files(db_factory, tmp_path, monkeypatch):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    file = write(app, uid, 'keep.txt')

    async def unavailable(*args, **kwargs):
        raise RuntimeError('isolated failure')
    monkeypatch.setattr(life, 'active_work', unavailable)
    with pytest.raises(RuntimeError):
        await life.trash(uid, 'keep.txt', actor=uid)
    assert file.exists()
    monkeypatch.undo()
    monkeypatch.setattr(app.state.claw.audit, 'log', unavailable)
    with pytest.raises(RuntimeError):
        await life.trash(uid, 'keep.txt', actor=uid)
    assert file.exists()


async def test_post_move_audit_failure_leaves_a_recoverable_entry(db_factory, tmp_path, monkeypatch):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    file = write(app, uid, 'recover.txt')
    original = app.state.claw.audit.log

    async def fail_completion(kind, *args, **kwargs):
        if kind == 'file_trashed':
            raise RuntimeError('isolated failure')
        await original(kind, *args, **kwargs)
    monkeypatch.setattr(app.state.claw.audit, 'log', fail_completion)
    with pytest.raises(RuntimeError):
        await life.trash(uid, 'recover.txt', actor=uid)
    assert not file.exists()
    entry = life.list_trash(uid, retention_days=30)['files'][0]
    monkeypatch.undo()
    await life.restore(uid, entry['id'])
    assert file.exists()


async def test_restore_collision_keeps_the_new_file(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    write(app, uid, 'file.txt', 'old')
    entry = await life.trash(uid, 'file.txt', actor=uid)
    replacement = write(app, uid, 'file.txt', 'new')
    with pytest.raises(FileConflict):
        await life.restore(uid, entry['id'])
    assert replacement.read_text() == 'new'
    assert life.list_trash(uid, retention_days=30)['files'][0]['id'] == entry['id']


async def test_permanent_purge_is_immediate_but_needs_the_switch_and_exact_path(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        write(app, uid, 'รายงาน.txt')
        life = FileLifecycle(app.state.claw, 'privateclaw')
        entry = await life.trash(uid, 'รายงาน.txt', actor=uid)
        url = '/api/files/trash/' + entry['id']
        yes = {'confirm_permanent': True, 'expected_path': 'รายงาน.txt'}
        await app.state.claw.workspace_policy.save(WorkspacePolicy(permanent_delete_enabled=False), uid)
        assert (await c.post(url + '/purge', json=yes, headers=headers)).status_code == 403
        assert (await c.post(url + '/purge', headers=headers)).status_code == 422
        await app.state.claw.workspace_policy.save(WorkspacePolicy(), uid)
        for confirmation in ({'expected_path': 'รายงาน.txt'}, {'confirm_permanent': True, 'expected_path': 'wrong.txt'}):
            assert (await c.post(url + '/purge', json=confirmation, headers=headers)).status_code == 409
            assert life.list_trash(uid, retention_days=30)['files']
        assert (await c.post(url + '/purge', json=yes, headers=headers)).status_code == 200
        assert life.list_trash(uid, retention_days=30) == {'files': [], 'total': 0}
        assert not (trash_dir(tmp_path, uid) / entry['id']).exists()
        write(app, uid, 'again.txt')
        again = await life.trash(uid, 'again.txt', actor=uid)
        alias = await c.request('DELETE', '/api/files/trash/' + again['id'], headers=headers,
                                json={'confirm_permanent': True, 'expected_path': 'again.txt'})
        assert alias.status_code == 200
    async with db_factory() as db:
        kinds = [e.kind for e in (await db.scalars(select(AuditEvent))).all()]
    assert kinds.count('file_purged') == 2


async def test_pending_local_delivery_sources_stay(db_factory, tmp_path):
    from types import SimpleNamespace

    from sbot.local_workspaces import LocalWorkspaces
    app, uid, _, _ = await owner_app(db_factory, tmp_path)
    source = write(app, uid, 'ส่งออก.txt', days_old=60)
    broker = LocalWorkspaces(tmp_path / 'broker')
    with broker.db() as db:
        db.execute("INSERT INTO delivery(id,owner,workspace,cloud_path,sha256,dest,name,status,attempts,created,expires) "
                   "VALUES (?,?,?,?,?,?,?,'pending',0,?,?)",
                   ('delivery', uid, 'folder', str(source), 'hash', 'out.txt', 'out.txt', 1, 9999999999))
    app.state.claw.runtime = SimpleNamespace(local_workspaces=broker)
    life = FileLifecycle(app.state.claw, 'privateclaw')
    with pytest.raises(FileConflict):
        await life.trash(uid, 'ส่งออก.txt', actor=uid)
    assert (await life.sweep(uid, enforcing(), now=time.time())).trashed == 0
    broker.cancel_delivery(uid, 'delivery')
    assert (await life.sweep(uid, enforcing(), now=time.time())).trashed == 1


async def test_stale_temp_record_does_not_block_restore_and_owner_mismatch_fails_closed(db_factory, tmp_path):
    app, uid, _, life = await owner_app(db_factory, tmp_path)
    file = write(app, uid, 'recover.txt')
    entry = await life.trash(uid, 'recover.txt', actor=uid)
    control = trash_dir(tmp_path, uid) / entry['id']
    (control / '.record-stale.new').write_text('stale interrupted write')
    record = control / 'record.json'
    record.write_text(json.dumps({**entry, 'owner': 'another-owner'}))
    with pytest.raises(FileNotFoundError):
        await life.restore(uid, entry['id'])
    record.write_text(json.dumps(entry))
    await life.restore(uid, entry['id'])
    assert file.exists()


async def test_independent_file_routes_do_not_grant_public_or_foreign_access(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, sid = await setup(c)
        write(app, uid, 'private.txt')
        await c.delete('/api/sessions/' + sid, headers=headers)
        for url in ('/api/files', '/api/files/trash', '/api/files/download/private.txt'):
            assert (await c.get(url)).status_code == 401
        assert (await c.post('/api/files/trash', json={'path': 'private.txt'})).status_code == 401
        _, foreign, _ = await setup(c, 'foreign-share@example.test')
        assert (await c.get('/api/files/download/private.txt', headers=foreign)).status_code == 404


async def test_messages_mark_only_missing_artifacts_that_are_in_trash(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, sid = await setup(c)
        write(app, uid, 'live.txt')
        write(app, uid, 'archived.txt')
        entry = await FileLifecycle(app.state.claw, 'privateclaw').trash(uid, 'archived.txt', actor=uid)
        await app.state.claw.messages.append(sid, [
            {'role': 'assistant', 'content': 'a', 'meta': {'artifacts': ['live.txt', 'archived.txt', 'gone.txt']}},
            {'role': 'assistant', 'content': 'b', 'meta': {'artifacts': ['live.txt', 'gone.txt']}},
        ])
        messages = (await c.get(f'/api/sessions/{sid}/messages', headers=headers)).json()
    assert messages[0]['meta']['archived'] == {'archived.txt': entry['id']}
    assert 'archived' not in messages[1]['meta']


async def test_deleting_an_account_removes_its_workspace_and_trash(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        _, admin, _ = await setup(c, 'admin@example.test')
        uid, _, _ = await setup(c, 'leaving@example.test')
        write(app, uid, 'keep-until-gone.txt')
        write(app, uid, 'trashed.txt')
        await FileLifecycle(app.state.claw, 'privateclaw').trash(uid, 'trashed.txt', actor=uid)
        control = trash_dir(tmp_path, uid)
        assert (await c.delete(f'/api/admin/users/{uid}', headers=admin)).status_code == 200
    assert not (tmp_path / 'ws' / uid).exists() and not control.exists()


async def test_a_failed_move_answers_503_and_leaves_no_trash_entry(db_factory, tmp_path, monkeypatch):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        file = write(app, uid, 'stuck.txt')
        before = tree(trash_dir(tmp_path, uid))

        def refuse(*args, **kwargs):
            raise OSError(5, 'Input/output error')

        monkeypatch.setattr(os, 'rename', refuse)
        response = await c.post('/api/files/trash', headers=headers, json={'path': 'stuck.txt'})
        monkeypatch.undo()
        assert response.status_code == 503
        assert 'Input/output' not in response.text
        assert file.exists()
        assert tree(trash_dir(tmp_path, uid)) == before


async def test_a_path_component_too_long_for_the_filesystem_is_not_found(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        write(app, uid, 'present.txt')
        long = 'a' * 300
        assert (await c.get('/api/files/download/' + long, headers=headers)).status_code == 404
        assert (await c.get(f'/api/files/download/{long}/x.txt', headers=headers)).status_code == 404
        assert (await c.post('/api/files/trash', headers=headers, json={'path': long})).status_code == 404
        assert (await c.post(f'/api/files/trash/{long}/restore', headers=headers)).status_code == 404


async def test_account_deletion_during_a_sweep_leaves_no_orphan_trash(db_factory, tmp_path, monkeypatch):
    import asyncio
    import threading

    app, uid, _, life = await owner_app(db_factory, tmp_path)
    write(app, uid, 'trashed.txt')
    await life.trash(uid, 'trashed.txt', actor=uid)
    control = trash_dir(tmp_path, uid)
    started, release = threading.Event(), threading.Event()
    real_purge_trash = life._purge_trash

    def held_purge_trash(owner, policy, now):
        started.set()
        release.wait(5)
        return real_purge_trash(owner, policy, now)

    monkeypatch.setattr(life, '_purge_trash', held_purge_trash)
    sweep = asyncio.create_task(life.sweep(uid, enforcing(), now=time.time()))
    assert await asyncio.to_thread(started.wait, 5)
    removal = asyncio.create_task(lifecycle.purge_user_files([tmp_path / 'ws'], uid))
    await asyncio.sleep(0.3)
    release.set()
    await sweep
    await removal
    assert not (tmp_path / 'ws' / uid).exists()
    assert not control.exists()


async def test_restore_over_a_file_where_the_folder_was_is_a_conflict(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / 'ws')
    async with client(app) as c:
        uid, headers, _ = await setup(c)
        write(app, uid, 'a/b.txt')
        entry = (await c.post('/api/files/trash', headers=headers, json={'path': 'a/b.txt'})).json()
        folder = app.state.claw.settings.workspaces_root / uid / 'a'
        folder.rmdir()
        folder.write_text('in the way')
        response = await c.post(f"/api/files/trash/{entry['id']}/restore", headers=headers)
        assert response.status_code == 409
        assert folder.read_text() == 'in the way'
        listing = FileLifecycle(app.state.claw, 'privateclaw').list_trash(uid, retention_days=30)
        assert [e['id'] for e in listing['files']] == [entry['id']]


async def test_cleanup_service_reconciles_before_its_first_sweep(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import claw.workspace.cleanup as cleanup

    monkeypatch.setattr(cleanup, '_STARTUP_DELAY_SECONDS', 0)
    (tmp_path / ('a' * 32)).mkdir()
    calls = []
    swept = asyncio.Event()

    class Lifecycle:
        root = tmp_path

        async def reconcile(self):
            calls.append('reconcile')
            return 0

        async def sweep(self, owner, policy, *, now):
            calls.append('sweep')
            swept.set()
            return lifecycle.SweepResult()

    class Policies:
        async def effective(self):
            return WorkspacePolicy()

    service = WorkspaceCleanupService(Lifecycle(), Policies(), SimpleNamespace())
    service.start()
    try:
        await asyncio.wait_for(swept.wait(), 5)
    finally:
        await service.stop()
    assert calls == ['reconcile', 'sweep']
