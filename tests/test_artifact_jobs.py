"""Resumable normal-chat artifact jobs: timeout, recovery and cancellation."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claw.config import LLMSettings, SandboxSettings, Settings, TeamWorkSettings
from claw.core.bus import EventBus
from claw.core.memory import MemoryService
from claw.core.runtime import AgentRuntime, _artifact_tool_scope, _is_artifact_task
from claw.db.stores import ArtifactJobStore, SkillStore
from claw.providers.base import ChatResult, LLMProvider, ProviderEvent, ToolCall
from claw.security.policy import PolicyEngine
from claw.tools.base import Tool
from tests.conftest import FakeProvider


class DelayedProvider(FakeProvider):
    def __init__(self, turns: list[list[ProviderEvent]], delays: list[float]):
        super().__init__(turns)
        self.delays = list(delays)
        self.tool_sets: list[set[str]] = []

    async def stream_chat(self, messages, *args, **kwargs) -> AsyncIterator[ProviderEvent]:
        self.calls.append(list(messages))
        self.tool_sets.append(
            {
                str(definition.get("function", {}).get("name") or "")
                for definition in (kwargs.get("tools") or [])
            }
        )
        events = self.turns.pop(0) if self.turns else [ChatResult(content="(exhausted)")]
        delay = self.delays.pop(0) if self.delays else 0
        if delay:
            await asyncio.sleep(delay)
        for event in events:
            yield event


class HangingProvider(LLMProvider):
    async def stream_chat(self, *args, **kwargs) -> AsyncIterator[ProviderEvent]:
        await asyncio.sleep(60)
        yield ChatResult(content="unreachable")

    def count_tokens(self, messages, model=None) -> int:
        return 1


class CountingReadTool(Tool):
    name = "read_file"
    description = "Read a source once."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self):
        self.calls = 0

    async def execute(self, path: str, **_: Any) -> str:
        self.calls += 1
        return "source rows"


class CountingWriteTool(Tool):
    name = "write_file"
    description = "Write one final artifact."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def __init__(self):
        self.calls = 0

    async def execute(self, path: str, content: str, **_: Any) -> str:
        self.calls += 1
        return f"Successfully wrote {path}"


def make_runtime(stores, db_factory, provider, tmp_path, **llm_overrides):
    llm_options = {
        "max_turn_seconds": 0.1,
        "artifact_job_max_segments": 3,
        "artifact_job_max_seconds": 1,
        "artifact_job_max_tokens": 10_000,
        "artifact_job_max_cost_usd": 10,
    }
    llm_options.update(llm_overrides)
    llm = LLMSettings(**llm_options)
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=llm,
        team_work=TeamWorkSettings(
            max_job_tokens=max(1000, llm.artifact_job_max_tokens),
            max_job_seconds=max(60, int(llm.artifact_job_max_seconds)),
            automatic_resources=False,
        ),
    )
    # Tests exercise sub-production caps (1 token / fractions of a second),
    # below the administrator-facing validation minima.
    settings.team_work.max_job_tokens = llm.artifact_job_max_tokens
    settings.team_work.max_job_seconds = llm.artifact_job_max_seconds
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    jobs = ArtifactJobStore(db_factory)
    runtime = AgentRuntime(
        settings=settings,
        provider=provider,
        bus=EventBus(),
        users=stores["users"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=memory,
        audit=stores["audit"],
        skills=SkillStore(db_factory),
        artifact_jobs=jobs,
    )
    return runtime, jobs


async def test_new_job_uses_effective_control_plane_budget_and_message_locale(
    stores, db_factory, tmp_path
):
    runtime, jobs = make_runtime(
        stores,
        db_factory,
        FakeProvider([[ChatResult(content="เสร็จแล้ว")]]),
        tmp_path,
        artifact_job_max_tokens=12_345,
        artifact_job_max_seconds=321,
    )
    user = await stores["users"].get_or_create_by_email("effective-policy@example.com")
    session = await stores["sessions"].create(user.id)

    await runtime.handle_message(user.id, session.id, "ช่วยสร้างไฟล์ Excel", locale="en")

    job = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert job["budget"]["tokens"] == 12_345
    assert job["budget"]["seconds"] == 321
    assert job["locale"] == "th"
    await runtime.drain()


async def test_timeout_auto_resumes_and_returns_one_artifact(stores, db_factory, tmp_path):
    write = ToolCall(id="write", name="write_file", arguments={"path": "report.xlsx", "content": "xlsx"})
    provider = DelayedProvider(
        [
            [ChatResult(content="too late")],
            [ChatResult(content=None, tool_calls=[write])],
            [ChatResult(content="Completed")],
        ],
        [0.15, 0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("artifact-timeout@example.com")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(user.id, session.id, "Create an Excel report")

    assert final == "Completed"
    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["status"] == "completed"
    assert saved["segment"] == 2
    assert saved["written"] == ["report.xlsx"]
    assert saved["metrics"]["iterations"] >= 2
    assert saved["metrics"]["tool_calls"] == 1
    assert saved["metrics"]["duration_ms"] > 0
    history = await stores["messages"].recent(session.id)
    artifacts = [path for msg in history for path in (msg.get("meta") or {}).get("artifacts", [])]
    assert artifacts == ["report.xlsx"]
    await runtime.drain()


async def test_checkpoint_prevents_source_reread_after_timeout(stores, db_factory, tmp_path):
    read = ToolCall(id="read-1", name="read_file", arguments={"path": "source.csv"})
    repeated = ToolCall(id="read-2", name="read_file", arguments={"path": "source.csv"})
    write = ToolCall(id="write", name="write_file", arguments={"path": "result.xlsx", "content": "ok"})
    provider = DelayedProvider(
        [
            [ChatResult(content=None, tool_calls=[read])],
            [ChatResult(content="too late")],
            [ChatResult(content=None, tool_calls=[repeated])],
            [ChatResult(content=None, tool_calls=[write])],
            [ChatResult(content="Done")],
        ],
        [0, 0.15, 0, 0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path)
    counting = CountingReadTool()
    user = await stores["users"].get_or_create_by_email("artifact-idempotent@example.com")
    session = await stores["sessions"].create(user.id)
    runtime.get_agent(user.id).tools.register(counting)

    assert await runtime.handle_message(user.id, session.id, "Create an Excel workbook from source.csv") == "Done"

    assert counting.calls == 1
    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["tool_results"] == {}  # terminal jobs discard sensitive recovery payloads
    assert saved["checkpoint_messages"] == []
    assert saved["content"] == ""
    assert saved["written"] == ["result.xlsx"]
    await runtime.drain()


async def test_restart_recovery_continues_checkpoint_without_duplicate_user_message(
    stores, db_factory, tmp_path
):
    runtime, jobs = make_runtime(stores, db_factory, FakeProvider([[ChatResult(content="Recovered")]]), tmp_path)
    user = await stores["users"].get_or_create_by_email("artifact-recovery@example.com")
    session = await stores["sessions"].create(user.id)
    await stores["messages"].append(session.id, [{"role": "user", "content": "Create Excel"}])
    job_id = "restart-recovery"
    await jobs.create(
        {
            "id": job_id,
            "turn_id": "turn-recovery",
            "user_id": user.id,
            "session_id": session.id,
            "content": "Create Excel",
            "channel": "web",
            "locale": "en",
            "media": [],
            "model": "",
            "permission_mode": "auto",
            "status": "running",
            "segment": 2,
            "max_segments": 3,
            "elapsed_seconds": 0.03,
            "token_count": 10,
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
            "checkpoint_messages": [],
            "tool_results": {},
            "written": [],
            "user_message_persisted": True,
            "created_at": "2026-09-18T00:00:00+00:00",
            "updated_at": "2026-09-18T00:00:00+00:00",
        }
    )

    await runtime.recover_artifact_jobs()
    for _ in range(100):
        saved = await jobs.get(job_id)
        if saved and saved["status"] not in {"queued", "running", "recovering"}:
            break
        await asyncio.sleep(0.005)

    saved = await jobs.get(job_id)
    assert saved and saved["status"] == "completed"
    history = await stores["messages"].recent(session.id)
    assert [msg["role"] for msg in history].count("user") == 1
    assert history[-1]["content"] == "Recovered"
    await runtime.drain()


async def test_recovery_rejects_inactive_user(stores, db_factory, tmp_path):
    provider = FakeProvider([[ChatResult(content="must not run")]])
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("artifact-inactive@example.com")
    session = await stores["sessions"].create(user.id)
    await stores["users"].update_flags(user.id, is_active=False)
    await jobs.create(
        {
            "id": "inactive-recovery",
            "user_id": user.id,
            "session_id": session.id,
            "content": "Create Excel",
            "status": "running",
            "segment": 1,
            "created_at": "2026-09-18T00:00:00+00:00",
        }
    )

    await runtime.recover_artifact_jobs()

    saved = await jobs.get("inactive-recovery")
    assert saved and saved["status"] == "failed"
    assert saved["content"] == ""
    assert provider.calls == []


async def test_cancel_stops_job_and_persists_cancelled_status(stores, db_factory, tmp_path):
    runtime, jobs = make_runtime(stores, db_factory, HangingProvider(), tmp_path)
    user = await stores["users"].get_or_create_by_email("artifact-cancel@example.com")
    session = await stores["sessions"].create(user.id)
    task = asyncio.create_task(runtime.handle_message(user.id, session.id, "Create an Excel report"))
    for _ in range(50):
        active = await jobs.list_for_session(user.id, session.id)
        if active:
            break
        await asyncio.sleep(0.005)
    assert active

    assert await runtime.cancel_artifact_job(user.id, active[0]["id"]) is True
    await asyncio.wait_for(task, timeout=1)

    saved = await jobs.get(active[0]["id"])
    assert saved and saved["status"] == "cancelled"
    await runtime.drain()


async def test_slow_provider_stops_at_segment_cap_in_thai(stores, db_factory, tmp_path):
    provider = DelayedProvider(
        [[ChatResult(content="late")], [ChatResult(content="late again")]],
        [0.15, 0.15],
    )
    runtime, jobs = make_runtime(
        stores, db_factory, provider, tmp_path, artifact_job_max_segments=2
    )
    user = await stores["users"].get_or_create_by_email("artifact-limit@example.com")
    session = await stores["sessions"].create(user.id)

    final = await runtime.handle_message(
        user.id, session.id, "สร้างไฟล์ Excel รายงาน", locale="th"
    )

    assert "ขีดจำกัดสะสม" in final
    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["status"] == "limit_reached"
    assert saved["segment"] == 2
    await runtime.drain()


async def test_recovery_replays_completed_write_without_duplicate_artifact(
    stores, db_factory, tmp_path
):
    write_once = ToolCall(
        id="write-1",
        name="write_file",
        arguments={"path": "final.xlsx", "content": "same bytes"},
    )
    write_again = ToolCall(
        id="write-2",
        name="write_file",
        arguments={"path": "final.xlsx", "content": "same bytes"},
    )
    provider = DelayedProvider(
        [
            [ChatResult(content=None, tool_calls=[write_once])],
            [ChatResult(content="late")],
            [ChatResult(content=None, tool_calls=[write_again])],
            [ChatResult(content="Done")],
        ],
        [0, 0.15, 0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path)
    writer = CountingWriteTool()
    user = await stores["users"].get_or_create_by_email("artifact-no-duplicate@example.com")
    session = await stores["sessions"].create(user.id)
    runtime.get_agent(user.id).tools.register(writer)

    assert await runtime.handle_message(user.id, session.id, "Create an Excel workbook") == "Done"

    assert writer.calls == 1
    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["written"] == ["final.xlsx"]
    history = await stores["messages"].recent(session.id)
    artifacts = [path for msg in history for path in (msg.get("meta") or {}).get("artifacts", [])]
    assert artifacts == ["final.xlsx"]
    await runtime.drain()


async def test_uncertain_tool_intent_is_not_reexecuted_after_restart(
    stores, db_factory, tmp_path
):
    call = ToolCall(
        id="write-retry",
        name="write_file",
        arguments={"path": "final.xlsx", "content": "same bytes"},
    )
    provider = FakeProvider(
        [[ChatResult(content=None, tool_calls=[call])], [ChatResult(content="Recovered")]]
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path)
    writer = CountingWriteTool()
    user = await stores["users"].get_or_create_by_email("artifact-intent@example.com")
    session = await stores["sessions"].create(user.id)
    runtime.get_agent(user.id).tools.register(writer)
    arguments = {"path": "final.xlsx", "content": "same bytes"}
    await jobs.create(
        {
            "id": "uncertain-intent",
            "turn_id": "turn-intent",
            "user_id": user.id,
            "session_id": session.id,
            "content": "Create an Excel workbook",
            "channel": "web",
            "locale": "en",
            "status": "running",
            "segment": 2,
            "max_segments": 3,
            "user_message_persisted": True,
            "pending_call": {
                "signature": f"write_file:{json.dumps(arguments, sort_keys=True)}",
                "name": "write_file",
                "arguments": arguments,
            },
            "created_at": "2026-09-18T00:00:00+00:00",
        }
    )

    await runtime.recover_artifact_jobs()
    for _ in range(100):
        saved = await jobs.get("uncertain-intent")
        if saved and saved["status"] not in {"queued", "running", "recovering"}:
            break
        await asyncio.sleep(0.005)

    saved = await jobs.get("uncertain-intent")
    assert saved and saved["status"] == "completed"
    assert saved["pending_call"] is None
    assert writer.calls == 0


@pytest.mark.parametrize(
    "limit",
    [
        {"artifact_job_max_seconds": 0.01},
        {"artifact_job_max_tokens": 1},
    ],
)
async def test_cumulative_time_and_token_caps_stop_auto_resume(
    stores, db_factory, tmp_path, limit
):
    provider = DelayedProvider([[ChatResult(content="late")]], [0.15])
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **limit)
    user = await stores["users"].get_or_create_by_email(f"artifact-cap-{next(iter(limit))}@example.com")
    session = await stores["sessions"].create(user.id)

    await runtime.handle_message(user.id, session.id, "Create an Excel report")

    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["status"] == "limit_reached"
    assert saved["segment"] == 1
    expected_calls = 0 if "artifact_job_max_tokens" in limit else 1
    assert len(provider.calls) == expected_calls
    await runtime.drain()


async def test_cumulative_usd_cap_stops_auto_resume(
    stores, db_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr("claw.core.runtime.estimated_cost_usd", lambda model, usage: 0.02)
    provider = DelayedProvider([[ChatResult(content="late")]], [0.15])
    runtime, jobs = make_runtime(
        stores, db_factory, provider, tmp_path, artifact_job_max_cost_usd=0.01
    )
    user = await stores["users"].get_or_create_by_email("artifact-cost@example.com")
    session = await stores["sessions"].create(user.id)

    await runtime.handle_message(user.id, session.id, "Create an Excel report")

    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["status"] == "limit_reached"
    assert saved["segment"] == 1
    assert saved["cost_usd"] == pytest.approx(0.02)
    assert len(provider.calls) == 1


async def test_read_cache_is_invalidated_after_source_mutation(
    stores, db_factory, tmp_path
):
    read = ToolCall(id="read-1", name="read_file", arguments={"path": "source.csv"})
    write = ToolCall(
        id="write", name="write_file", arguments={"path": "source.csv", "content": "new"}
    )
    reread = ToolCall(id="read-2", name="read_file", arguments={"path": "source.csv"})
    provider = FakeProvider(
        [
            [ChatResult(content=None, tool_calls=[read])],
            [ChatResult(content=None, tool_calls=[write])],
            [ChatResult(content=None, tool_calls=[reread])],
            [ChatResult(content="Done")],
        ]
    )
    runtime, _ = make_runtime(stores, db_factory, provider, tmp_path)
    reader = CountingReadTool()
    writer = CountingWriteTool()
    user = await stores["users"].get_or_create_by_email("artifact-reread@example.com")
    session = await stores["sessions"].create(user.id)
    runtime.get_agent(user.id).tools.register(reader)
    runtime.get_agent(user.id).tools.register(writer)

    assert await runtime.handle_message(
        user.id, session.id, "Create an Excel workbook from source.csv"
    ) == "Done"
    assert reader.calls == 2
    assert writer.calls == 1


async def test_artifact_e2e_streams_progress_and_uses_scoped_context(
    stores, db_factory, tmp_path
):
    provider = DelayedProvider([[ChatResult(content="Ready")]], [0])
    runtime, _ = make_runtime(stores, db_factory, provider, tmp_path)
    user = await stores["users"].get_or_create_by_email("artifact-progress@example.com")
    session = await stores["sessions"].create(user.id)

    async with runtime.bus.subscribe(session.id) as queue:
        task = asyncio.create_task(
            runtime.handle_message(user.id, session.id, "Create an Excel workbook")
        )
        events = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=1)
            events.append(event)
            if getattr(event, "type", "") == "artifact_job_progress" and event.status == "completed":
                break
        assert await task == "Ready"

    progress = [event for event in events if getattr(event, "type", "") == "artifact_job_progress"]
    assert [event.status for event in progress] == ["running", "completed"]
    assert "write_file" in provider.tool_sets[0]
    assert "read_skill" in provider.tool_sets[0]
    assert "spawn" not in provider.tool_sets[0]
    serialized_prompt = str(provider.calls[0])
    assert "[Resumable Artifact Job]" in serialized_prompt
    assert "deterministic script" in serialized_prompt
    assert "xlsx" in serialized_prompt.lower()
    assert "Assumption/TBD" in serialized_prompt
    assert "duplicate scope" in serialized_prompt
    assert "skill-creator" not in serialized_prompt
    await runtime.drain()


async def test_preflight_failure_is_terminal_and_not_recovered(
    stores, db_factory, tmp_path
):
    runtime, jobs = make_runtime(stores, db_factory, FakeProvider([]), tmp_path)
    runtime.policy = PolicyEngine()
    runtime._process_turn = AsyncMock(return_value="Request blocked")
    user = await stores["users"].get_or_create_by_email("artifact-blocked@example.com")
    session = await stores["sessions"].create(user.id)

    assert await runtime.handle_message(user.id, session.id, "Create an Excel workbook") == "Request blocked"

    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["status"] == "failed"
    assert saved["error"] == "Request blocked"
    assert await jobs.list_recoverable() == []


async def test_policy_masks_content_before_artifact_checkpoint(
    stores, db_factory, tmp_path
):
    runtime, jobs = make_runtime(stores, db_factory, FakeProvider([]), tmp_path)
    runtime.policy = PolicyEngine()
    runtime._process_turn = AsyncMock(return_value="Stopped")
    user = await stores["users"].get_or_create_by_email("artifact-mask@example.com")
    session = await stores["sessions"].create(user.id)

    await runtime.handle_message(
        user.id,
        session.id,
        "Create an Excel file for alice@example.com",
    )

    artifact_job = runtime._process_turn.await_args.kwargs["artifact_job"]
    assert artifact_job["content"] == "Create an Excel file for [REDACTED_EMAIL]"
    saved = (await jobs.list_for_session(user.id, session.id, active_only=False))[0]
    assert saved["content"] == ""
    assert saved["checkpoint_messages"] == []


def test_plain_text_report_is_not_promoted_to_artifact_job():
    assert not _is_artifact_task("Create a report summarizing sales")
    assert not _is_artifact_task("จัดทำรายงานสรุปยอดขาย")
    assert _is_artifact_task("Create an Excel report")


def test_artifact_classifier_keeps_filename_extensions_together():
    assert _is_artifact_task("Create report.pdf")
    assert _is_artifact_task("Save invoice.xlsx")
    assert _is_artifact_task("Create report.pdf. Then email it to me.")


def test_artifact_connector_scope_requires_connector_name():
    available = ["mcp_gmail_search", "mcp_gmail_read", "write_file", "read_file"]
    assert "mcp_gmail_search" not in _artifact_tool_scope(
        available, "Create an Excel file from the report", [], {"gmail"}
    )
    assert "mcp_gmail_search" in _artifact_tool_scope(
        available, "Create an Excel file from Gmail", [], {"gmail"}
    )
