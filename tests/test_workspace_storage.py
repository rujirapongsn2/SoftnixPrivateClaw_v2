"""Workspace storage: usage, quota gate, policy store, retention cleanup, chat-delete cleanup."""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from claw.config import Settings, WorkspaceSettings
from claw.core.loop import _Snapshot, _snapshot_workspace
from claw.tools.filesystem import WriteFileTool
from claw.tools.registry import ToolRegistry
from claw.workspace.cleanup import WorkspaceCleanupService, sweep_workspace
from claw.workspace.policy import WorkspacePolicy, WorkspacePolicyStore
from claw.workspace.session_files import candidate_paths, remove_files
from claw.workspace.usage import WorkspaceAccounts, is_cleanup_command, measure
from tests.conftest_app import build_api_app, client
from tests.test_manage import _bearer, _register

DAY = 86400


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _write(path: Path, size: int = 10, days_old: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if days_old:
        _age(path, days_old)
    return path


class FixedPolicies:
    """A policy source with byte-level quotas, which the MB-based real one cannot express."""

    def __init__(self, quota_bytes=0, quota_files=0, **extra):
        self.policy = SimpleNamespace(
            quota_bytes=quota_bytes,
            quota_mb=max(1, quota_bytes // (1024 * 1024)) if quota_bytes else 0,
            quota_files=quota_files,
            tmp_retention_days=extra.get("tmp_retention_days", 7),
            uploads_retention_days=extra.get("uploads_retention_days", 7),
            cleanup_enabled=extra.get("cleanup_enabled", True),
            enforce=extra.get("enforce", True),
        )

    async def effective(self):
        return self.policy


# ------------------------------------------------------------------ usage


def test_measure_counts_everything_but_links(tmp_path):
    _write(tmp_path / "a.bin", 100)
    _write(tmp_path / "sub" / "b.bin", 300)
    _write(tmp_path / ".hidden" / "c.bin", 50)
    outside = _write(tmp_path.parent / f"{tmp_path.name}-outside" / "big.bin", 9999)
    (tmp_path / "link").symlink_to(outside.parent)
    (tmp_path / "filelink").symlink_to(outside)

    usage = measure(tmp_path)

    assert (usage.bytes, usage.files, usage.complete) == (450, 3, True)
    assert usage.largest[0] == (os.path.join("sub", "b.bin"), 300)


def test_measure_stops_at_its_file_cap(tmp_path):
    for index in range(20):
        _write(tmp_path / f"f{index}", 1)
    usage = measure(tmp_path, file_cap=10)
    assert usage.complete is False and usage.files == 10


async def test_check_allows_until_the_quota_and_names_the_big_files(tmp_path):
    _write(tmp_path / "u1" / "video.mp4", 800)
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=1000))

    assert await accounts.check("u1", 100, 1) is None
    full = await accounts.check("u1", 300, 1)
    assert full.startswith("Error: the workspace storage quota is full")
    assert "video.mp4" in full and "Nothing was written" in full


async def test_file_count_quota_and_unlimited(tmp_path):
    for index in range(3):
        _write(tmp_path / "u1" / f"f{index}", 1)
    assert await WorkspaceAccounts(tmp_path, FixedPolicies(quota_files=3)).check("u1", 0, 1)
    assert await WorkspaceAccounts(tmp_path, FixedPolicies(quota_files=4)).check("u1", 0, 1) is None
    assert await WorkspaceAccounts(tmp_path, FixedPolicies()).check("u1", 10**12, 1) is None


async def test_writes_inside_the_cache_window_still_count(tmp_path):
    (tmp_path / "u1").mkdir()
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=1000))
    assert await accounts.check("u1", 400, 1) is None  # measures: empty
    accounts.note_written("u1", 400)
    accounts.note_written("u1", 400)
    assert await accounts.check("u1", 400, 1)  # 800 + 400 > 1000, without re-measuring
    accounts.forget("u1", now=True)
    assert await accounts.check("u1", 400, 1) is None  # nothing was really written


async def test_a_command_makes_the_next_check_measure_again_but_not_every_time(tmp_path, monkeypatch):
    import claw.workspace.usage as usage_module

    clock = [1000.0]
    monkeypatch.setattr(usage_module.time, "monotonic", lambda: clock[0])
    measured = []
    real_measure = usage_module.measure
    monkeypatch.setattr(usage_module, "measure", lambda *a, **k: measured.append(1) or real_measure(*a, **k))
    (tmp_path / "u1").mkdir()
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=10_000))

    await accounts.check("u1", 1, 1)
    assert len(measured) == 1
    for _ in range(5):  # a run of commands: each marks the figure out of date ...
        accounts.forget("u1")
        await accounts.check("u1", 1, 1)
    assert len(measured) == 1  # ... but it is reused for a few seconds
    clock[0] += 6
    await accounts.check("u1", 1, 1)
    assert len(measured) == 2
    accounts.forget("u1", now=True)  # a deletion is seen at once
    await accounts.check("u1", 1, 1)
    assert len(measured) == 3


@pytest.mark.parametrize("quota_files,expected_cap", [(2, 10_000), (20_000, 40_000), (0, 1_000_000)])
async def test_the_walk_stops_far_past_the_file_quota(tmp_path, monkeypatch, quota_files, expected_cap):
    import claw.workspace.usage as usage_module

    caps = []
    real_measure = usage_module.measure
    monkeypatch.setattr(usage_module, "measure", lambda root, **kw: caps.append(kw["file_cap"]) or real_measure(root, **kw))
    for index in range(30):
        _write(tmp_path / "u1" / f"f{index}", 1)
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=10**9, quota_files=quota_files))

    await accounts.check("u1", 0, 1)

    assert caps == [expected_cap]


async def test_observe_only_never_blocks_but_says_so(tmp_path):
    from loguru import logger

    _write(tmp_path / "u1" / "big.bin", 5000)
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=1000, enforce=False))
    lines = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        assert await accounts.check("u1", 10, 1) is None
        assert await accounts.gate("u1", "write_file", {"path": "a", "content": "x"}) is None
        assert await accounts.gate("u1", "exec", {"command": "python make.py"}) is None
        assert await accounts.check("u1", 10, 1) is None
    finally:
        logger.remove(sink)
    assert len([line for line in lines if "observe-only" in line]) == 1  # once, not per write


@pytest.mark.parametrize(
    "command,allowed",
    [
        ("rm -rf big", True),
        ("rm a b; ls -la", True),
        ("du -sh * | head", True),
        ("find . -size +10M -delete", True),
        ("find . -name '*.mp4' 2>/dev/null", True),
        ("echo hi > note.txt", False),
        ("cat a >> b", False),
        ("find . -exec cp {} /workspace/x \\;", False),
        ("pip install pandas", False),
        ("rm a && wget http://x/y", False),
        ("rm $(ls)", False),
        ("ls & dd if=/dev/zero of=big bs=1M count=5000", False),
        ("rm a & rm b", True),
        ("echo done 2>&1", True),
        ("", False),
    ],
)
def test_cleanup_only_commands(command, allowed):
    assert is_cleanup_command(command) is allowed


async def test_gate_refuses_writes_when_full_but_lets_cleanup_run(tmp_path):
    _write(tmp_path / "u1" / "big.bin", 5000)
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=1000))

    assert await accounts.gate("u1", "write_file", {"path": "a", "content": "x"})
    assert await accounts.gate("u1", "edit_file", {"path": "a", "old_text": "", "new_text": "yyyy"})
    assert await accounts.gate("u1", "generate_workbook", {})
    assert await accounts.gate("u1", "exec", {"command": "python make.py"})
    assert await accounts.gate("u1", "exec", {"command": "rm big.bin"}) is None
    assert await accounts.gate("u1", "read_file", {"path": "big.bin"}) is None


async def test_registry_gate_blocks_the_tool_before_it_runs(tmp_path):
    workspace = tmp_path / "u1"
    _write(workspace / "big.bin", 5000)
    accounts = WorkspaceAccounts(tmp_path, FixedPolicies(quota_bytes=1000))
    registry = ToolRegistry(gate=lambda name, params: accounts.gate("u1", name, params))
    registry.register(WriteFileTool(workspace))

    result = await registry.execute("write_file", {"path": "new.txt", "content": "hello"})

    assert result.startswith("Error: the workspace storage quota is full")
    assert not (workspace / "new.txt").exists()


# ----------------------------------------------------------- policy store


def test_policy_bounds():
    for bad in ({"quota_mb": -1}, {"tmp_retention_days": 99999}, {"unknown": 1}):
        with pytest.raises(ValueError):
            WorkspacePolicy(**bad)
    assert WorkspacePolicy(quota_mb=0, quota_files=0).quota_bytes == 0


async def test_policy_defaults_come_from_the_environment_and_overrides_persist(db_factory):
    settings = Settings(_env_file=None, workspace=WorkspaceSettings(quota_mb=10, tmp_retention_days=3))
    store = WorkspacePolicyStore(db_factory, settings, ttl=0)
    assert (await store.effective()).quota_mb == 10
    # Nothing is enforced until someone says so: an update must not delete or block by itself.
    assert (await store.effective()).enforce is False

    await store.save(WorkspacePolicy(quota_mb=77, quota_files=5, tmp_retention_days=1, uploads_retention_days=2), "admin")

    # Another worker (a fresh store) sees the override; the environment still supplies the defaults.
    other = WorkspacePolicyStore(db_factory, settings, ttl=0)
    effective = await other.effective()
    assert (effective.quota_mb, effective.quota_files, effective.uploads_retention_days) == (77, 5, 2)
    assert other.defaults().quota_mb == 10


async def test_a_corrupt_stored_policy_falls_back_to_defaults(db_factory):
    from claw.db.models import AppSetting

    async with db_factory() as db:
        db.add(AppSetting(key="workspace_policy", value={"policy": {"quota_mb": "lots"}}))
        await db.commit()
    store = WorkspacePolicyStore(db_factory, Settings(_env_file=None), ttl=0)
    assert (await store.effective()).quota_mb == 2048


# ---------------------------------------------------------------- cleanup


def test_sweep_deletes_only_expired_scratch_and_attachments(tmp_path):
    now = time.time()
    _write(tmp_path / ".tmp" / "old.txt", 5, days_old=8)
    _write(tmp_path / ".tmp" / "deep" / "old2.txt", 5, days_old=30)
    _write(tmp_path / ".tmp" / "fresh.txt", 5, days_old=1)
    _write(tmp_path / "uploads" / "ab12cd34-report.pdf", 7, days_old=8)
    _write(tmp_path / "uploads" / "ab12cd35-new.pdf", 7, days_old=2)
    _write(tmp_path / "uploads" / "generated-1.png", 9, days_old=60)
    _write(tmp_path / "report.xlsx", 9, days_old=60)
    _write(tmp_path / "downloads" / "video.mp4", 9, days_old=60)
    _age(tmp_path / ".tmp" / "deep", 30)

    result = sweep_workspace(tmp_path, now=now, tmp_days=7, uploads_days=7)

    assert (result.files, result.bytes) == (3, 17)
    assert sorted(result.paths) == [".tmp/deep/old2.txt", ".tmp/old.txt", "uploads/ab12cd34-report.pdf"]
    survivors = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()}
    assert survivors == {
        ".tmp/fresh.txt", "uploads/ab12cd35-new.pdf", "uploads/generated-1.png", "report.xlsx", "downloads/video.mp4",
    }
    # The emptied folder goes on a later pass, once it has sat untouched for a while.
    assert (tmp_path / ".tmp" / "deep").exists()
    _age(tmp_path / ".tmp" / "deep", 1)
    sweep_workspace(tmp_path, now=now, tmp_days=7, uploads_days=7)
    assert not (tmp_path / ".tmp" / "deep").exists()
    assert (tmp_path / ".tmp").exists()


def test_sweep_dry_run_and_disabled_categories(tmp_path):
    _write(tmp_path / ".tmp" / "old.txt", 5, days_old=30)
    _write(tmp_path / "uploads" / "old.pdf", 5, days_old=30)

    dry = sweep_workspace(tmp_path, now=time.time(), tmp_days=7, uploads_days=7, dry_run=True)
    assert dry.files == 2 and (tmp_path / ".tmp" / "old.txt").exists()

    only_tmp = sweep_workspace(tmp_path, now=time.time(), tmp_days=7, uploads_days=0)
    assert only_tmp.files == 1 and (tmp_path / "uploads" / "old.pdf").exists()
    assert sweep_workspace(tmp_path, now=time.time(), tmp_days=0, uploads_days=0).files == 0


def test_sweep_never_follows_links_out_of_the_workspace(tmp_path):
    victim = _write(tmp_path / "victim" / "keep.txt", 5, days_old=90)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".tmp").symlink_to(victim.parent)  # the scratch folder itself is a link
    (workspace / "uploads").mkdir()
    (workspace / "uploads" / "link.pdf").symlink_to(victim)
    _age(workspace / "uploads" / "link.pdf", 90)

    assert sweep_workspace(workspace, now=time.time(), tmp_days=7, uploads_days=7).files == 0
    assert victim.exists()

    (workspace / ".tmp").unlink()
    (workspace / ".tmp").mkdir()
    (workspace / ".tmp" / "inner-link").symlink_to(victim)
    _age(workspace / ".tmp" / "inner-link", 90)
    assert sweep_workspace(workspace, now=time.time(), tmp_days=7, uploads_days=7).files == 0
    assert victim.exists()


async def test_cleanup_service_pass_covers_user_workspaces_only(tmp_path):
    user = "0123456789abcdef0123456789abcdef"
    _write(tmp_path / user / ".tmp" / "old.txt", 5, days_old=30)
    _write(tmp_path / "_browser_broker" / ".tmp" / "old.txt", 5, days_old=30)
    _write(tmp_path / "not-a-user" / ".tmp" / "old.txt", 5, days_old=30)
    settings = Settings(_env_file=None)
    audited = []

    class Audit:
        async def log(self, kind, payload, **_):
            audited.append((kind, payload))

    service = WorkspaceCleanupService(tmp_path, FixedPolicies(), settings, Audit())

    result = await service.run_once()

    assert result.files == 1
    assert not (tmp_path / user / ".tmp" / "old.txt").exists()
    assert (tmp_path / "_browser_broker" / ".tmp" / "old.txt").exists()
    assert (tmp_path / "not-a-user" / ".tmp" / "old.txt").exists()
    assert audited == [
        ("workspace_cleanup", {"files": 1, "bytes": 5, "workspaces": 1, "paths": [f"{user}/.tmp/old.txt"]})
    ]


async def test_cleanup_service_respects_the_switch_and_dry_run(tmp_path):
    user = "0123456789abcdef0123456789abcdef"
    _write(tmp_path / user / ".tmp" / "old.txt", 5, days_old=30)

    off = WorkspaceCleanupService(tmp_path, FixedPolicies(cleanup_enabled=False), Settings(_env_file=None))
    assert (await off.run_once()).files == 0

    # Observe-only (enforcement not switched on): it reports, it does not delete.
    dry = WorkspaceCleanupService(tmp_path, FixedPolicies(enforce=False), Settings(_env_file=None))
    assert (await dry.run_once()).files == 1
    assert (tmp_path / user / ".tmp" / "old.txt").exists()


# ------------------------------------------------- files of a deleted chat


def test_candidate_paths_from_a_chat_record():
    hints = {
        "meta": [{"artifacts": ["report.xlsx", "uploads/generated-1.png"]}, {"image_model": "x"}],
        "tool_calls": [
            [{"function": {"name": "write_file", "arguments": json.dumps({"path": "build.py", "content": "x"})}}],
            [{"function": {"name": "generate_workbook", "arguments": json.dumps({"output": "out/data.xlsx"})}}],
            [{"function": {"name": "exec", "arguments": json.dumps({"command": "rm -rf /"})}}],
            [{"function": {"name": "write_file", "arguments": "{not json"}}],
        ],
        "user_text": ["see this\n\n[Attached: ab12cd34-a.pdf, ab12cd35-b b.png]"],
    }
    assert candidate_paths(hints) == [
        "report.xlsx", "uploads/generated-1.png", "build.py", "out/data.xlsx",
        "uploads/ab12cd34-a.pdf", "uploads/ab12cd35-b b.png",
    ]


def test_a_file_the_chat_only_edited_is_not_its_to_delete():
    def call(name, **args):
        return {"function": {"name": name, "arguments": json.dumps(args)}}

    hints = {
        # Every edited file is also surfaced as a chip, so the chip must not count as proof of creation.
        "meta": [{"artifacts": ["notes.md", "made.py", "from-shell.png", "rewritten.txt"]}],
        "tool_calls": [
            [call("edit_file", path="notes.md", old_text="a", new_text="b")],
            [call("write_file", path="made.py", content="x"), call("edit_file", path="made.py", old_text="x", new_text="y")],
            [call("edit_file", path="rewritten.txt", old_text="a", new_text="b"), call("write_file", path="rewritten.txt", content="z")],
        ],
    }
    # notes.md and rewritten.txt were first touched by an edit (they pre-dated the chat);
    # made.py was created here; from-shell.png has no tool call, so a chip is all there is.
    assert candidate_paths(hints) == ["made.py", "from-shell.png"]


def test_remove_files_is_confined_and_cautious(tmp_path):
    workspace = tmp_path / "ws"
    outside = _write(tmp_path / "outside.txt", 5)
    mine = _write(workspace / "mine.txt", 5, days_old=1)
    later = _write(workspace / "later.txt", 5)  # touched after the chat ended
    (workspace / "folder").mkdir()
    (workspace / "link.txt").symlink_to(outside)
    (workspace / "dirlink").symlink_to(outside.parent)

    deleted, freed = remove_files(
        workspace,
        ["mine.txt", "later.txt", "folder", "link.txt", "dirlink/outside.txt", "../outside.txt", "missing.txt"],
        not_modified_after=time.time() - 3600,
    )

    assert (deleted, freed) == (1, 5)
    assert not mine.exists() and later.exists() and outside.exists() and (workspace / "folder").is_dir()
    assert (workspace / "link.txt").is_symlink()


async def test_deleting_a_chat_removes_its_files_and_keeps_shared_ones(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "w")
    state = app.state.claw
    async with client(app) as c:
        token, user = await _register(c, "cleanup-chat@example.com")
        workspace = tmp_path / "w" / user["id"]
        mine = _write(workspace / "mine.xlsx", 5, days_old=1)
        script = _write(workspace / "build.py", 5, days_old=1)
        attachment = _write(workspace / "uploads" / "ab12cd34-in.pdf", 5, days_old=1)
        shared = _write(workspace / "shared.pdf", 5, days_old=1)
        rewritten = _write(workspace / "rewritten.txt", 5)  # a later chat overwrote it just now
        unrelated = _write(workspace / "unrelated.txt", 5, days_old=1)

        doomed = (await c.post("/api/sessions", json={"title": "doomed"}, headers=_bearer(token))).json()["id"]
        other = (await c.post("/api/sessions", json={"title": "other"}, headers=_bearer(token))).json()["id"]
        write_call = {"function": {"name": "write_file", "arguments": json.dumps({"path": "build.py", "content": "x"})}}
        await state.messages.append(doomed, [
            {"role": "user", "content": "go\n\n[Attached: ab12cd34-in.pdf]"},
            {"role": "assistant", "content": "", "tool_calls": [write_call]},
            {"role": "assistant", "content": "done", "meta": {"artifacts": ["mine.xlsx", "shared.pdf", "rewritten.txt"]}},
        ])
        await state.messages.append(other, [
            {"role": "assistant", "content": "also", "meta": {"artifacts": ["shared.pdf"]}},
        ])

        assert (await c.delete(f"/api/sessions/{doomed}", headers=_bearer(token))).json() == {"deleted": True}

    assert not mine.exists() and not script.exists() and not attachment.exists()
    assert shared.exists()  # another chat still shows it
    assert rewritten.exists()  # modified after the chat ended
    assert unrelated.exists()
    assert await state.messages.recent(other)  # the other chat is untouched


async def test_deleting_a_chat_keeps_files_it_only_edited_or_shared_through_tool_calls(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "w")
    state = app.state.claw
    async with client(app) as c:
        token, user = await _register(c, "cleanup-shared@example.com")
        workspace = tmp_path / "w" / user["id"]
        created = _write(workspace / "created.py", 5, days_old=1)
        edited = _write(workspace / "notes.md", 5, days_old=1)  # existed before this chat
        both = _write(workspace / "data.csv", 5, days_old=1)  # also written by another chat

        def call(name, **args):
            return {"function": {"name": name, "arguments": json.dumps(args)}}

        doomed = (await c.post("/api/sessions", json={"title": "a"}, headers=_bearer(token))).json()["id"]
        other = (await c.post("/api/sessions", json={"title": "b"}, headers=_bearer(token))).json()["id"]
        await state.messages.append(doomed, [
            {"role": "assistant", "content": "", "tool_calls": [
                call("write_file", path="created.py", content="x"),
                call("edit_file", path="notes.md", old_text="a", new_text="b"),
                call("write_file", path="data.csv", content="x"),
            ]},
            # The agent surfaces edited files as chips too; that must not make notes.md this chat's.
            {"role": "assistant", "content": "done", "meta": {"artifacts": ["created.py", "notes.md"]}},
        ])
        await state.messages.append(other, [
            {"role": "assistant", "content": "", "tool_calls": [call("edit_file", path="data.csv", old_text="a", new_text="b")]},
        ])

        await c.delete(f"/api/sessions/{doomed}", headers=_bearer(token))

    assert not created.exists()
    assert edited.exists()  # this chat only edited it
    assert both.exists()  # another chat still works on it


async def test_a_failure_cleaning_files_never_fails_the_delete(db_factory, tmp_path, monkeypatch):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "w")
    import claw.workspace.session_files as session_files

    def boom(*_a, **_k):
        raise OSError("disk on fire")

    monkeypatch.setattr(session_files, "remove_files", boom)
    async with client(app) as c:
        token, user = await _register(c, "cleanup-fail@example.com")
        _write(tmp_path / "w" / user["id"] / "a.txt", 5, days_old=1)
        sid = (await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))).json()["id"]
        await app.state.claw.messages.append(sid, [{"role": "assistant", "content": "x", "meta": {"artifacts": ["a.txt"]}}])
        response = await c.delete(f"/api/sessions/{sid}", headers=_bearer(token))
        assert response.status_code == 200 and response.json() == {"deleted": True}
        assert (await c.get("/api/sessions", headers=_bearer(token))).json() == []


# ------------------------------------------------------------ API surface


async def test_workspace_policy_endpoint_is_admin_only_and_validated(db_factory, tmp_path):
    app = build_api_app(db_factory, workspaces_root=tmp_path / "w")
    async with client(app) as c:
        admin, _ = await _register(c, "ws-admin@example.com")
        user, _ = await _register(c, "ws-user@example.com")
        assert (await c.get("/api/admin/workspace-policy")).status_code == 401
        assert (await c.get("/api/admin/workspace-policy", headers=_bearer(user))).status_code == 403

        current = (await c.get("/api/admin/workspace-policy", headers=_bearer(admin))).json()
        assert current["quota_mb"] == 2048 and current["defaults"]["tmp_retention_days"] == 7
        assert current["enforce"] is False  # observe-only until switched on

        body = {k: v for k, v in current.items() if k != "defaults"}
        assert (await c.put("/api/admin/workspace-policy", json=body, headers=_bearer(user))).status_code == 403
        assert (
            await c.put("/api/admin/workspace-policy", json={**body, "quota_mb": -5}, headers=_bearer(admin))
        ).status_code == 422
        saved = await c.put(
            "/api/admin/workspace-policy", json={**body, "quota_mb": 500, "uploads_retention_days": 3}, headers=_bearer(admin)
        )
        assert saved.status_code == 200 and saved.json()["quota_mb"] == 500
        again = (await c.get("/api/admin/workspace-policy", headers=_bearer(admin))).json()
        assert (again["quota_mb"], again["uploads_retention_days"]) == (500, 3)


async def test_upload_is_refused_when_the_workspace_is_full(db_factory, tmp_path):
    root = tmp_path / "w"
    app = build_api_app(db_factory, workspaces_root=root)
    app.state.claw.runtime = SimpleNamespace(
        workspace_accounts=WorkspaceAccounts(root, FixedPolicies(quota_bytes=1000))
    )
    async with client(app) as c:
        token, user = await _register(c, "upload-quota@example.com")
        _write(root / user["id"] / "big.bin", 990)
        sid = (await c.post("/api/sessions", json={"title": "t"}, headers=_bearer(token))).json()["id"]

        response = await c.post(
            f"/api/sessions/{sid}/attachments", files={"files": ("a.txt", b"y" * 100)}, headers=_bearer(token)
        )

        assert response.status_code == 413
        assert "storage quota is full" in response.json()["detail"]
        assert list((root / user["id"] / "uploads").glob("*")) == []


# ------------------------------------------- agent loop: the workspace scan


def test_snapshot_does_not_enter_skipped_folders(tmp_path):
    _write(tmp_path / "keep.txt")
    _write(tmp_path / "app" / "main.py")
    _write(tmp_path / "node_modules" / "pkg" / "index.js")
    _write(tmp_path / ".git" / "objects" / "ab")
    _write(tmp_path / ".tmp" / "scratch.txt")
    _write(tmp_path / "pkg" / "__pycache__" / "x.pyc")
    _write(tmp_path / "venv" / "lib" / "site-packages" / "m.py")
    _write(tmp_path / ".hidden.txt")

    snap = _snapshot_workspace(tmp_path)

    assert set(snap) == {"keep.txt", "app/main.py"}
    assert snap.truncated is False


def test_snapshot_reports_a_cut_short_walk(tmp_path):
    for index in range(30):
        _write(tmp_path / f"f{index}")
    snap = _snapshot_workspace(tmp_path, limit=10)
    assert snap.truncated is True and len(snap) == 10
    assert _snapshot_workspace(tmp_path / "missing") == _Snapshot()


def test_snapshot_skips_links(tmp_path):
    outside = _write(tmp_path / "outside" / "x.txt")
    workspace = tmp_path / "ws"
    _write(workspace / "a.txt")
    (workspace / "dir-link").symlink_to(outside.parent)
    (workspace / "file-link").symlink_to(outside)
    assert set(_snapshot_workspace(workspace)) == {"a.txt"}


async def test_an_agent_over_quota_can_free_space_and_continue(stores, db_factory, tmp_path):
    from tests.conftest import FakeProvider
    from tests.test_artifact_jobs import make_runtime

    runtime, _ = make_runtime(stores, db_factory, FakeProvider([]), tmp_path)
    runtime.workspace_accounts = WorkspaceAccounts(tmp_path / "workspaces", FixedPolicies(quota_bytes=1000))
    user = await stores["users"].get_or_create_by_email("quota-agent@example.com")
    agent = runtime.get_agent(user.id)
    _write(agent.workspace / "old-render.mp4", 5000)

    refused = await agent.tools.execute("write_file", {"path": "report.txt", "content": "hello"})
    assert refused.startswith("Error: the workspace storage quota is full") and "old-render.mp4" in refused
    assert not (agent.workspace / "report.txt").exists()
    assert (await agent.tools.execute("exec", {"command": "python3 -c 'print(1)'"})).startswith("Error: the workspace")

    freed = await agent.tools.execute("exec", {"command": "rm old-render.mp4"})
    assert "[exit code: 0]" in freed

    assert (await agent.tools.execute("write_file", {"path": "report.txt", "content": "hello"})).startswith("Wrote")
    assert (agent.workspace / "report.txt").read_text() == "hello"
    await runtime.drain()
