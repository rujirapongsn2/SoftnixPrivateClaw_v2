async def test_message_append_assigns_monotonic_seq(stores):
    users, sessions, messages = stores["users"], stores["sessions"], stores["messages"]
    user = await users.get_or_create_by_email("a@b.c")
    session = await sessions.create(user.id)

    await messages.append(session.id, [{"role": "user", "content": "one"}])
    await messages.append(
        session.id,
        [{"role": "assistant", "content": "two"}, {"role": "user", "content": "three"}],
    )

    history = await messages.recent(session.id)
    assert [m["content"] for m in history] == ["one", "two", "three"]
    assert await messages.max_seq(session.id) == 3


async def test_recent_respects_after_seq(stores):
    users, sessions, messages = stores["users"], stores["sessions"], stores["messages"]
    user = await users.get_or_create_by_email("a@b.c")
    session = await sessions.create(user.id)
    await messages.append(session.id, [{"role": "user", "content": f"m{i}"} for i in range(5)])

    history = await messages.recent(session.id, after_seq=3)
    assert [m["content"] for m in history] == ["m3", "m4"]


async def test_memory_core_and_history(stores):
    users, memories = stores["users"], stores["memories"]
    user = await users.get_or_create_by_email("a@b.c")

    assert await memories.get_core(user.id) == ""
    await memories.set_core(user.id, "User likes Thai food.")
    await memories.set_core(user.id, "User likes Thai food. Works at Softnix.")
    assert "Softnix" in await memories.get_core(user.id)

    await memories.append_history(user.id, "[2026-07-04] discussed rebuild")
    assert (await memories.recent_history(user.id)) == ["[2026-07-04] discussed rebuild"]


async def test_user_get_or_create_is_idempotent(stores):
    users = stores["users"]
    first = await users.get_or_create_by_email("same@x.y")
    second = await users.get_or_create_by_email("same@x.y")
    assert first.id == second.id
