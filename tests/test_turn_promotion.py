"""A chat turn that runs out of time while still making progress continues in the background."""

import asyncio
from typing import Any

import pytest

from claw.core.runtime import _is_artifact_task, _is_delivery_task
from claw.providers.base import ChatResult, ToolCall
from claw.tools.base import Tool
from tests.test_artifact_jobs import CountingReadTool, DelayedProvider, make_runtime


class RenderTool(Tool):
    """Stands in for a connector tool the artifact tool scope would otherwise drop."""

    name = "mcp_demo_render"
    description = "Render a scene."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **_: Any) -> str:
        return "rendered"


# A slow tool, not a slow reply, crosses the turn budget: the loop checks the
# deadline between steps, so the turn always stops right after this tool.
_TIMING = {"max_turn_seconds": 0.3, "artifact_job_max_seconds": 60}
_LATE = 0.5


class SlowReadTool(CountingReadTool):
    async def execute(self, path: str, **_: Any) -> str:
        await asyncio.sleep(_LATE)
        return await super().execute(path)


def _read(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, name="read_file", arguments={"path": "stations.csv"})


async def _user_session(stores, email: str):
    user = await stores["users"].get_or_create_by_email(email)
    return user, await stores["sessions"].create(user.id)


async def test_progressing_turn_is_promoted_and_finishes(stores, db_factory, tmp_path):
    provider = DelayedProvider(
        [
            [ChatResult(content=None, tool_calls=[_read("read-1")])],
            [ChatResult(content="Here is the station summary")],
        ],
        [0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **_TIMING)
    reader = SlowReadTool()
    user, session = await _user_session(stores, "promote-progress@example.com")
    runtime.get_agent(user.id).tools.register(reader)

    final = await runtime.handle_message(user.id, session.id, "Collect the water levels for Bangkok stations")

    assert final == "Here is the station summary"
    [job] = await jobs.list_for_session(user.id, session.id, active_only=False)
    assert job["promoted"] is True
    assert job["status"] == "completed"
    assert job["segment"] == 2
    assert job["checkpoint_messages"] == []  # terminal jobs discard recovery payloads
    assert reader.calls == 1
    # The first segment ran live, so its tool steps stay in the transcript.
    history = await stores["messages"].recent(session.id)
    assert [m["role"] for m in history].count("user") == 1
    assert any(m["role"] == "tool" and m.get("content") == "source rows" for m in history)
    assert history[-1]["content"] == "Here is the station summary"
    await runtime.drain()


async def test_turn_without_progress_stops_and_says_where(stores, db_factory, tmp_path):
    failing = ToolCall(id="exec-1", name="exec", arguments={"command": "find / -name video.mp4"})
    provider = DelayedProvider(
        [[ChatResult(content=None, tool_calls=[failing])], [ChatResult(content="too late")]],
        [0, _LATE],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **_TIMING)
    user, session = await _user_session(stores, "promote-stuck@example.com")

    final = await runtime.handle_message(user.id, session.id, "Find the rendered clip", locale="en")

    assert await jobs.list_for_session(user.id, session.id, active_only=False) == []
    assert "taking too long" in final
    assert "made no progress" in final
    assert "- exec: Error: whole-filesystem scan blocked" in final
    await runtime.drain()


async def test_promoted_job_stops_when_a_segment_only_repeats(stores, db_factory, tmp_path):
    provider = DelayedProvider(
        [
            [ChatResult(content=None, tool_calls=[_read("read-1")])],
            [ChatResult(content=None, tool_calls=[_read("read-2")])],
            [ChatResult(content="must not run")],
        ],
        [0, 0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **_TIMING)
    reader = SlowReadTool()
    user, session = await _user_session(stores, "promote-repeat@example.com")
    runtime.get_agent(user.id).tools.register(reader)

    final = await runtime.handle_message(
        user.id, session.id, "Collect the water levels for Bangkok stations", locale="th"
    )

    [job] = await jobs.list_for_session(user.id, session.id, active_only=False)
    assert job["status"] == "limit_reached"  # resumable later with "ทำต่อ"
    assert job["stop_reason"] == "no_progress"
    assert reader.calls == 2
    assert "ไม่มีความคืบหน้า" in final
    assert "ขีดจำกัดสะสม" not in final  # no cumulative limit was hit
    assert provider.turns == [[ChatResult(content="must not run")]]
    await runtime.drain()


async def test_auto_continue_can_be_disabled(stores, db_factory, tmp_path):
    provider = DelayedProvider(
        [[ChatResult(content=None, tool_calls=[_read("read-1")])], [ChatResult(content="must not run")]],
        [0, 0],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, auto_continue_turns=False, **_TIMING)
    user, session = await _user_session(stores, "promote-off@example.com")
    runtime.get_agent(user.id).tools.register(SlowReadTool())

    final = await runtime.handle_message(user.id, session.id, "Collect the water levels", locale="en")

    assert await jobs.list_for_session(user.id, session.id, active_only=False) == []
    assert "taking too long" in final
    await runtime.drain()


async def test_promoted_job_can_be_cancelled(stores, db_factory, tmp_path):
    provider = DelayedProvider(
        [
            [ChatResult(content=None, tool_calls=[_read("read-1")])],
            [ChatResult(content="never delivered")],
        ],
        [0, 30],
    )
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **_TIMING)
    user, session = await _user_session(stores, "promote-cancel@example.com")
    runtime.get_agent(user.id).tools.register(SlowReadTool())
    task = asyncio.create_task(runtime.handle_message(user.id, session.id, "Collect the water levels"))
    active = []
    for _ in range(200):
        active = [j for j in await jobs.list_for_session(user.id, session.id) if j.get("segment") == 2]
        if active:
            break
        await asyncio.sleep(0.01)
    assert active and active[0]["promoted"]

    assert await runtime.cancel_artifact_job(user.id, active[0]["id"]) is True
    await asyncio.wait_for(task, timeout=2)

    saved = await jobs.get(active[0]["id"])
    assert saved and saved["status"] == "cancelled"
    assert active[0]["id"] not in runtime._artifact_tasks
    await runtime.drain()


async def test_delivery_job_keeps_connector_tools(stores, db_factory, tmp_path):
    provider = DelayedProvider([[ChatResult(content="Rendered and ready")]], [0])
    runtime, jobs = make_runtime(stores, db_factory, provider, tmp_path, **_TIMING)
    user, session = await _user_session(stores, "delivery-scope@example.com")
    runtime.get_agent(user.id).tools.register(RenderTool())

    await runtime.handle_message(
        user.id, session.id, "เรนเดอร์ครบ 5 ซีน แล้วรวมให้ผมเป็นไฟล์เดียวแล้วให้ผมดาวน์โหลด"
    )

    [job] = await jobs.list_for_session(user.id, session.id, active_only=False)
    assert job["full_scope"] is True
    assert "mcp_demo_render" in provider.tool_sets[0]
    await runtime.drain()


@pytest.mark.parametrize(
    "text",
    [
        "เรนเดอร์ครบ 5 ซีน แล้วรายงานสถานะแต่ละ job เมื่อทำเสร็จแล้วรวมให้ผมเป็นไฟล์เดียวแล้วให้ผมดาวน์โหลด",
        "render the intro video and give me a download link",
        "can you merge the three clips into one mp4?",
        "ช่วยรวมคลิปให้หน่อยได้ไหม",
        "รวมให้ผมเป็นไฟล์เดียว",
        "เรนเดอร์ 5 ซีนแล้วรวมเป็นวิดีโอเดียว",
    ],
)
def test_delivery_wording_is_recognised(text):
    assert _is_delivery_task(text)


@pytest.mark.parametrize(
    "text",
    [
        "อธิบายวิธีรวมไฟล์ PDF",
        "รวมยอดขายเดือนนี้ให้หน่อย",
        "how do I merge files in git?",
        "what does render mean",
        "ทำไมรวมไฟล์ไม่ได้",
        "สรุปข่าววันนี้",
        "ช่วยรวมยอดจากไฟล์ Excel",
        "รวมไฟล์ที่แนบ",
    ],
)
def test_questions_about_rendering_are_not_delivery(text):
    assert not _is_delivery_task(text)
    assert not _is_artifact_task(text)
