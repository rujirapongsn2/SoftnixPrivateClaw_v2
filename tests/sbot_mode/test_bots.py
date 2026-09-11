"""Tests for sbot Bot Registry, Chief of Staff seeding, and CRUD operations."""

import pytest
from sbot.db.stores import BotStore, UserStore


@pytest.mark.asyncio
async def test_cos_auto_seeding(stores):
    bot_store: BotStore = stores["bots"]
    user_store: UserStore = stores["users"]

    user = await user_store.get_or_create_by_email("test@sbot.ai", "tester")
    cos = await bot_store.get_or_create_cos(user.id)

    assert cos is not None
    assert cos.name == "Default"
    assert cos.kind == "chief_of_staff"
    assert cos.role_title == "Chief of Staff"
    assert cos.owner_id == user.id
    assert cos.avatar["initial"] == "D"

    # Calling again returns same instance
    cos_again = await bot_store.get_or_create_cos(user.id)
    assert cos_again.id == cos.id


@pytest.mark.asyncio
async def test_create_and_list_specialist_bots(stores):
    bot_store: BotStore = stores["bots"]
    user_store: UserStore = stores["users"]

    user = await user_store.get_or_create_by_email("specialist@sbot.ai", "specialist_owner")
    cos = await bot_store.get_or_create_cos(user.id)

    researcher = await bot_store.create(
        owner_id=user.id,
        name="นักวิจัย",
        role_title="Senior Researcher",
        charter="คุณคือนักวิจัยข้อมูล ค้นคว้าและสรุปข้อเท็จจริงอย่างแม่นยำ",
        tool_allowlist=["web_search", "web_fetch", "read_file"],
        created_by="user",
    )
    assert researcher.id is not None
    assert researcher.name == "นักวิจัย"
    assert researcher.role_title == "Senior Researcher"

    qa_bot = await bot_store.create(
        owner_id=user.id,
        name="QA Web",
        role_title="Quality Assurance",
        charter="ตรวจสอบคุณภาพเว็บและฟีเจอร์",
        created_by=f"bot:{cos.id}",
    )
    assert qa_bot.created_by == f"bot:{cos.id}"

    bots = await bot_store.list_for_user(user.id)
    bot_names = [b.name for b in bots]
    assert "Default" in bot_names
    assert "นักวิจัย" in bot_names
    assert "QA Web" in bot_names

    # Test update
    updated = await bot_store.update(researcher.id, user.id, role_title="Lead Researcher")
    assert updated.role_title == "Lead Researcher"

    # Test archive
    archived = await bot_store.archive(qa_bot.id, user.id)
    assert archived is True
    active_bots = await bot_store.list_for_user(user.id, include_archived=False)
    assert "QA Web" not in [b.name for b in active_bots]
    all_bots = await bot_store.list_for_user(user.id, include_archived=True)
    assert "QA Web" in [b.name for b in all_bots]


@pytest.mark.asyncio
async def test_legacy_cos_default_is_upgraded_without_replacing_the_bot(stores):
    bot_store: BotStore = stores["bots"]
    user_store: UserStore = stores["users"]
    user = await user_store.get_or_create_by_email("legacy@sbot.ai", "legacy")
    legacy = await bot_store.create(
        owner_id=user.id,
        name=BotStore.LEGACY_COS_NAME,
        role_title="Chief of Staff",
        charter=BotStore.LEGACY_COS_CHARTER,
        kind="chief_of_staff",
        avatar=BotStore.LEGACY_COS_AVATAR,
        created_by="system",
    )

    upgraded = await bot_store.get_or_create_cos(user.id)

    assert upgraded.id == legacy.id
    assert upgraded.name == "Default"
    assert upgraded.charter == BotStore.DEFAULT_COS_CHARTER
    assert upgraded.avatar == BotStore.DEFAULT_COS_AVATAR


@pytest.mark.asyncio
async def test_bot_access_is_owner_scoped(stores):
    """A bot id is guessable and flows in from LLM tool calls, so every read and
    write path must be scoped to the owner rather than trusting the id."""
    bot_store: BotStore = stores["bots"]
    user_store: UserStore = stores["users"]

    victim = await user_store.get_or_create_by_email("victim@sbot.ai", "victim")
    attacker = await user_store.get_or_create_by_email("attacker@sbot.ai", "attacker")

    secret = await bot_store.create(
        owner_id=victim.id,
        name="Secret Analyst",
        role_title="Analyst",
        charter="confidential charter",
    )

    assert await bot_store.get(secret.id, victim.id) is not None
    assert await bot_store.get(secret.id, attacker.id) is None
    assert await bot_store.update(secret.id, attacker.id, role_title="pwned") is None
    assert await bot_store.archive(secret.id, attacker.id) is False

    still_mine = await bot_store.get(secret.id, victim.id)
    assert still_mine.role_title == "Analyst"
    assert still_mine.is_archived is False


@pytest.mark.asyncio
async def test_create_bot_tool_rejects_undelegatable_tools_and_enforces_quota(stores):
    from sbot.tools.cos import MAX_BOTS_PER_OWNER, CreateBotTool

    bot_store: BotStore = stores["bots"]
    user_store: UserStore = stores["users"]
    user = await user_store.get_or_create_by_email("quota@sbot.ai", "quota")
    tool = CreateBotTool(bot_store, user.id, creator_bot_id="cos")

    # Chief of Staff is an LLM; the allowlist it proposes is untrusted.
    result = await tool.execute(
        name="Sneaky", role_title="X", charter="c", tool_allowlist=["exec", "browser", "spawn"]
    )
    assert "cannot grant" in result
    assert await bot_store.get_by_name(user.id, "Sneaky") is None

    for i in range(MAX_BOTS_PER_OWNER):
        await bot_store.create(owner_id=user.id, name=f"filler-{i}", role_title="Filler")

    result = await tool.execute(name="OneTooMany", role_title="X", charter="c")
    assert "maximum" in result
    assert await bot_store.get_by_name(user.id, "OneTooMany") is None
