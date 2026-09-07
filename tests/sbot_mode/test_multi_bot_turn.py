"""Tests for multi-bot message handling and session routing."""

import asyncio
import pytest

from sbot.config import LLMSettings, SandboxSettings, Settings
from sbot.core.bus import EventBus
from sbot.core.memory import MemoryService
from sbot.core.runtime import AgentRuntime
from tests.sbot_mode.conftest import FakeProvider, text_turn


def make_runtime(stores, provider, tmp_path) -> AgentRuntime:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        workspaces_root=tmp_path / "workspaces",
        sandbox=SandboxSettings(enabled=False),
        llm=LLMSettings(),
    )
    memory = MemoryService(stores["memories"], stores["messages"], stores["sessions"], provider)
    return AgentRuntime(
        settings=settings,
        provider=provider,
        bus=EventBus(),
        users=stores["users"],
        bots=stores["bots"],
        sessions=stores["sessions"],
        messages=stores["messages"],
        memory=memory,
        audit=stores["audit"],
    )


@pytest.mark.asyncio
async def test_multi_bot_turn_execution(stores, tmp_path):
    user = await stores["users"].get_or_create_by_email("team_lead@sbot.ai")
    
    # 1. Chief of Staff session
    cos = await stores["bots"].get_or_create_cos(user.id)
    cos_session = await stores["sessions"].create(
        user.id, title="Chief of Staff Chat", bot_id=cos.id, kind="direct"
    )

    # 2. Specialist session (Researcher)
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="คุณคือผู้เชี่ยวชาญด้านการค้นหาข้อมูล",
    )
    researcher_session = await stores["sessions"].create(
        user.id, title="Research Task", bot_id=researcher.id, kind="direct"
    )

    provider = FakeProvider([
        text_turn("รับทราบครับหัวหน้า บุ้ยจะประสานงานทีมให้"),
        text_turn("ผลการค้นคว้าพบว่าข้อมูลเรียบร้อยดีครับ"),
    ])
    runtime = make_runtime(stores, provider, tmp_path)

    # Execute CoS turn
    cos_answer = await runtime.handle_message(
        user.id, cos_session.id, "วางแผนเปิดตัวโปรเจกต์ใหม่"
    )
    assert cos_answer == "รับทราบครับหัวหน้า บุ้ยจะประสานงานทีมให้"

    # Execute Specialist turn
    res_answer = await runtime.handle_message(
        user.id, researcher_session.id, "ค้นหาเทรนด์ AI ล่าสุด"
    )
    assert res_answer == "ผลการค้นคว้าพบว่าข้อมูลเรียบร้อยดีครับ"

    # Verify message persistence and separation
    cos_history = await stores["messages"].recent(cos_session.id)
    assert len(cos_history) == 2
    assert cos_history[0]["content"] == "วางแผนเปิดตัวโปรเจกต์ใหม่"
    assert cos_history[1]["content"] == "รับทราบครับหัวหน้า บุ้ยจะประสานงานทีมให้"

    res_history = await stores["messages"].recent(researcher_session.id)
    assert len(res_history) == 2
    assert res_history[0]["content"] == "ค้นหาเทรนด์ AI ล่าสุด"
    assert res_history[1]["content"] == "ผลการค้นคว้าพบว่าข้อมูลเรียบร้อยดีครับ"


@pytest.mark.asyncio
async def test_the_chief_of_staff_is_told_to_lead_and_who_it_leads(stores, tmp_path):
    """A leader that has to look up its own team mostly does not, and then
    answers as if it had none — which is what happened in practice."""
    user = await stores["users"].get_or_create_by_email("lead@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาและสรุปข้อมูลตลาด\nรายละเอียดเพิ่มเติมบรรทัดสอง",
    )
    session = await stores["sessions"].create(
        user.id, title="CoS", bot_id=cos.id, kind="direct"
    )
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ช่วยปรับปรุงแผนการตลาด")

    system_prompt = provider.calls[0][0]["content"]
    assert "# Leading your team" in system_prompt
    assert "delegate_many" in system_prompt
    assert "นักวิจัย" in system_prompt
    assert researcher.id in system_prompt
    assert "ค้นหาและสรุปข้อมูลตลาด" in system_prompt
    # Only the gist: the second charter line is what `list_bots` is for.
    assert "รายละเอียดเพิ่มเติมบรรทัดสอง" not in system_prompt
    # The Chief of Staff is not on its own roster — it cannot delegate to itself.
    assert f"bot_id: `{cos.id}`" not in system_prompt


@pytest.mark.asyncio
async def test_a_specialist_is_not_told_to_delegate(stores, tmp_path):
    """It has no orchestration tools, so the brief would only describe work it
    cannot hand out."""
    user = await stores["users"].get_or_create_by_email("solo@sbot.ai")
    researcher = await stores["bots"].create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="ค้นหาข้อมูล",
    )
    session = await stores["sessions"].create(
        user.id, title="Research", bot_id=researcher.id, kind="direct"
    )
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ค้นหาเทรนด์")

    assert "# Leading your team" not in provider.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_a_chief_of_staff_with_no_team_is_told_the_work_is_its_own(stores, tmp_path):
    """Otherwise the brief tells it to delegate and the roster is silent, which
    reads as 'find someone' — and it spends a call on `list_bots` to find none."""
    user = await stores["users"].get_or_create_by_email("alone@sbot.ai")
    cos = await stores["bots"].get_or_create_cos(user.id)
    session = await stores["sessions"].create(
        user.id, title="CoS", bot_id=cos.id, kind="direct"
    )
    provider = FakeProvider([text_turn("ครับ")])
    runtime = make_runtime(stores, provider, tmp_path)

    await runtime.handle_message(user.id, session.id, "ช่วยเขียนแผน")

    system_prompt = provider.calls[0][0]["content"]
    assert "# Leading your team" in system_prompt
    assert "no specialists yet" in system_prompt
    assert "bot_id:" not in system_prompt
