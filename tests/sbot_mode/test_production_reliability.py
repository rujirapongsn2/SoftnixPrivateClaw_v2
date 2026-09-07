"""Regression coverage for mission acceptance, context and durable delivery."""

import json
from types import SimpleNamespace

import pytest

from sbot.core.bus import EventBus
from sbot.core.memory import MemoryService
from sbot.core.mission_engine import InvalidGraphError, NodeContext
from sbot.core.specialist import SpecialistOutcome
from sbot.core.turn_context import current_session_id
from sbot.filenames import safe_filename
from sbot.tools.artifacts import PublishArtifactTool
from sbot.tools.blackboard import BlackboardTool
from sbot.tools.finish_step import FinishStepTool
from sbot.tools.missions import GroupMissionTool, MissionStartTool, MissionStatusTool
from tests.sbot_mode.conftest import FakeProvider, text_turn
from tests.sbot_mode.test_missions import _two_specialists, make_service


def test_unicode_filename_keeps_extension_with_utf8_byte_limit():
    from sbot.api.blueprints import _safe_name as blueprint_name
    from sbot.api.routes import _safe_name as upload_name

    name = "แบบฟอร์มเบิกค่ารักษาพยาบาล.xlsx"
    assert blueprint_name(name) == upload_name(name) == name
    long_name = safe_filename("ใบเสนอราคา" * 100 + ".xlsx")
    assert long_name.endswith(".xlsx") and len(long_name.encode()) <= 220
    assert "/" not in safe_filename("../../รายงาน.docx")


async def test_publish_existing_source_file_is_a_separate_download(stores, tmp_path):
    source = tmp_path / "ตัวอย่าง.py"
    source.write_text("print(1)")
    events = []
    tool = PublishArtifactTool(tmp_path)
    await tool.execute("/workspace/ตัวอย่าง.py", progress=events.append)
    published = tmp_path / events[0]["path"]
    source.write_text("print(2)")
    assert published.read_text() == "print(1)"
    from sbot.core.loop import _is_artifact_hidden

    assert not _is_artifact_hidden(events[0]["path"])
    with pytest.raises(ValueError):
        await tool.execute("../outside.txt")


async def test_finish_requires_planned_files_and_valid_office_package(tmp_path):
    finish = FinishStepTool(tmp_path, ["รายงาน.docx"])
    assert (await finish.execute("completed", "done", "checked", [])).startswith("Error:")
    (tmp_path / "รายงาน.docx").write_text("not a Word document")
    assert "invalid Office" in await finish.execute("completed", "done", "checked", ["รายงาน.docx"])
    assert finish.result is None
    from docx import Document

    doc = Document()
    doc.add_paragraph("รายงานฉบับทดสอบ")
    doc.save(tmp_path / "รายงาน.docx")
    assert (
        await finish.execute("completed", "done", "opened Word document", ["รายงาน.docx"])
        == "Step result recorded."
    )
    assert (tmp_path / finish.result["artifacts"][0]).is_file()


@pytest.mark.parametrize("reported", [None, "blocked", "completed"])
@pytest.mark.parametrize("cut_off", [False, True])
async def test_mission_acceptance_does_not_trust_final_text(stores, tmp_path, reported, cut_off):
    user, bot, _ = await _two_specialists(stores, f"{reported}@test.local")
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.require_verified_results = True
    mission = await service.plan(
        user.id, "Do work", [{"id": "n", "bot_id": bot.id, "instruction": "Do work"}]
    )
    node = (await stores["missions"].get_nodes(mission.id))[0]

    async def run(*args, **kwargs):
        if reported:
            await kwargs["extra_tools"][1].execute(reported, "result", "checked requirements", [])
        return SpecialistOutcome(text="Done!", timed_out=cut_off)

    async def context_block(bot):
        return ""

    runner = SimpleNamespace(workspace=tmp_path, run=run, context_block=context_block)
    result = await service._execute_node(
        mission, runner, node, NodeContext(manifest={}, parent_outputs={}, attempt=1)
    )
    assert result.status == ("done" if reported == "completed" else "error")


async def test_full_parent_result_survives_summary_and_can_be_paged(stores, tmp_path):
    user, bot, writer = await _two_specialists(stores, "context@test.local")
    full = "ข้อมูล" * 4000 + "FINAL_REQUIREMENT"
    provider = FakeProvider([text_turn(full), text_turn("processed")])
    service = make_service(stores, provider, tmp_path)
    mission = await service.plan(
        user.id,
        "Analyze",
        [
            {"id": "source", "bot_id": bot.id, "instruction": "research"},
            {"id": "consumer", "bot_id": writer.id, "instruction": "summarize", "depends_on": ["source"]},
        ],
    )
    assert await service.run_to_completion(mission.id, user.id) == "completed"
    assert await stores["missions"].blackboard_read(mission.id, "result:source") == full
    reader = BlackboardTool(stores["missions"], mission.id, "consumer")
    assert "FINAL_REQUIREMENT" in await reader.execute("read", key="result:source", offset=len(full) - 100)
    assert (await reader.execute("write", key="scope:members", value="[]")).startswith("Error:")
    page = json.loads(
        await MissionStatusTool(service, user.id).execute(
            mission.id, node_id="source", offset=len(full) - 100
        )
    )
    assert page["output"].endswith("FINAL_REQUIREMENT") and page["next_offset"] is None


async def test_committed_results_retry_without_llm_and_only_deliver_once(stores, tmp_path, monkeypatch):
    user, bot, _ = await _two_specialists(stores, "delivery@test.local")
    session = await stores["sessions"].create(user.id)
    service = make_service(stores, FakeProvider([]), tmp_path)
    service.messages = stores["messages"]
    service.bus = EventBus()
    mission = await service.plan(
        user.id, "Deliver file", [{"id": "n", "bot_id": bot.id}], session_id=session.id
    )
    await stores["missions"].finish_node(
        mission.id, "n", status="done", output="A verified result", artifacts=[".deliveries/test/file.txt"]
    )
    await stores["missions"].update_mission(mission.id, status="completed")
    original = service.messages.append

    async def unavailable(*args, **kwargs):
        raise OSError("temporary database outage")

    monkeypatch.setattr(service.messages, "append", unavailable)
    await service.reconcile_reports()
    monkeypatch.setattr(service.messages, "append", original)
    async with service.bus.subscribe(session.id) as queue:
        await service.reconcile_reports()
        event = queue.get_nowait()
        assert event.type == "mission_reported" and event.artifacts
        await service.reconcile_reports()
        assert queue.empty()
    rows = await stores["messages"].recent(session.id)
    assert len(rows) == 1 and "A verified result" in rows[0]["content"]
    assert not service.provider.calls


async def test_resume_scans_every_page(stores, tmp_path, monkeypatch):
    user, bot, _ = await _two_specialists(stores, "resume@test.local")
    service = make_service(stores, FakeProvider([]), tmp_path)
    ids = []
    for i in range(5):
        mission = await service.plan(user.id, f"Job {i}", [{"id": "n", "bot_id": bot.id}])
        await stores["missions"].update_mission(mission.id, status="running")
        ids.append(mission.id)
    spawned = []
    monkeypatch.setattr(service, "_spawn", spawned.append)
    assert await service.resume_interrupted(limit=2) == 5
    assert set(spawned) == set(ids)


async def test_group_rejects_outsiders_and_cross_conversation_missions(stores, tmp_path):
    user, bot, outsider = await _two_specialists(stores, "group@test.local")
    service = make_service(stores, FakeProvider([]), tmp_path)
    with pytest.raises(InvalidGraphError, match="outside"):
        await service.plan(
            user.id, "Group work", [{"id": "n", "bot_id": outsider.id}], member_ids=frozenset([bot.id])
        )
    session = await stores["sessions"].create(user.id)
    other = await stores["sessions"].create(user.id)
    mission = await service.plan(
        user.id,
        "Group work",
        [{"id": "n", "bot_id": bot.id}],
        session_id=session.id,
        member_ids=frozenset([bot.id]),
    )
    with pytest.raises(InvalidGraphError, match="outside"):
        await service.apply_replan(mission.id, user.id, add=[{"id": "other", "bot_id": outsider.id}])
    token = current_session_id.set(other.id)
    try:
        tool = GroupMissionTool(MissionStartTool(service, user.id), service, user.id)
        assert (await tool.execute(mission_id=mission.id)).startswith("Error:")
        listing = GroupMissionTool(MissionStatusTool(service, user.id), service, user.id)
        assert json.loads(await listing.execute()) == []
    finally:
        current_session_id.reset(token)


async def test_failed_memory_summary_never_discards_source_messages(stores):
    user = await stores["users"].get_or_create_by_email("memory@test.local")
    session = await stores["sessions"].create(user.id)
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], FakeProvider([]))
    for _ in range(6):
        assert not await memory._note_unusable(session.id, 50, "invalid response")
    fresh = await stores["sessions"].get(session.id)
    assert fresh.last_consolidated_seq == 0


async def test_delegate_gives_coordinator_system_attachment_evidence(stores, tmp_path):
    from sbot.providers.base import ChatResult, ToolCall
    from sbot.tools.cos import DelegateTool, ListBotsTool

    user, bot, _ = await _two_specialists(stores, "manifest@test.local")
    (tmp_path / "report.txt").write_text("Verified content")
    provider = FakeProvider(
        [
            [
                ChatResult(
                    content=None,
                    tool_calls=[ToolCall(id="p", name="publish_artifact", arguments={"path": "report.txt"})],
                )
            ],
            text_turn("Ready"),
        ]
    )
    tool = DelegateTool(stores["bots"], user.id, provider, None, tmp_path)
    events = []
    result = await tool.execute(task="Send existing file", bot_id=bot.id, progress=events.append)
    finished = next(event for event in events if event.get("kind") == "delegation_finished")
    assert "Runtime attachment manifest" in result
    assert finished["artifacts"][0] in result
    assert {"publish_artifact", "read_docx"} <= set(provider.offered_tools[0])
    roster = json.loads(await ListBotsTool(stores["bots"], user.id).execute())
    assert all("publish_artifact" in member["intrinsic_tools"] for member in roster)
