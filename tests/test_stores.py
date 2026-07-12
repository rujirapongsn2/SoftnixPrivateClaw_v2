import pytest


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


async def test_bulk_create_imported_distinguishes_race_from_other_errors(stores):
    """A row that races a concurrent insert (email already exists) must be
    reported as already_exists; a row that fails for any OTHER reason (here:
    a colliding primary key, which every dialect enforces — a real FK
    violation isn't reliably enforced on SQLite test DBs without a pragma
    this codebase doesn't set) must NOT be silently mislabeled as a
    duplicate."""
    users = stores["users"]
    existing = await users.create(email="raced@x.io", signup_method="imported")

    failed = await users.bulk_create_imported(
        [
            {"email": "raced@x.io", "password_hash": "", "display_name": "", "signup_method": "imported"},
            {
                "id": existing.id,  # PK collision with a DIFFERENT email — not a duplicate email
                "email": "collides-on-id@x.io",
                "password_hash": "",
                "display_name": "",
                "signup_method": "imported",
            },
            {"email": "fine@x.io", "password_hash": "", "display_name": "", "signup_method": "imported"},
        ]
    )
    assert failed == {"raced@x.io": "already_exists", "collides-on-id@x.io": "error"}
    assert await users.get_by_email("fine@x.io") is not None


async def test_email_uniqueness_is_case_insensitive_at_db_layer(stores):
    """The lower(email) unique index must reject a second account differing
    only by case, not just the application-level case-insensitive lookup."""
    from sqlalchemy.exc import IntegrityError

    users = stores["users"]
    await users.create(email="Jane@Example.com", signup_method="password")
    with pytest.raises(IntegrityError):
        await users.create(email="jane@example.com", signup_method="password")


async def test_claim_activation_send_lets_only_one_concurrent_caller_win(stores):
    """The resend cooldown must be enforced by an atomic conditional UPDATE,
    not a read-then-write race — otherwise concurrent callers (e.g. an
    attacker firing repeated login attempts for a known imported email) all
    observe the same stale timestamp and all send, defeating the cooldown."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    users = stores["users"]
    user = await users.create(email="race-activate@x.io", signup_method="imported")
    now = datetime.now(timezone.utc)

    results = await asyncio.gather(*[users.claim_activation_send(user.id, now, 300) for _ in range(8)])
    assert sum(results) == 1

    # And a claim well past the cooldown window succeeds again.
    later = now + timedelta(seconds=301)
    assert await users.claim_activation_send(user.id, later, 300) is True
