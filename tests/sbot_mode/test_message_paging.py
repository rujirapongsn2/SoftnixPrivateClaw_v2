"""A session's transcript is read a page at a time (PRD §5.4).

The endpoint used to ask for a fixed 500 messages and return them as a bare
list. A session past that ceiling silently lost its oldest messages — nothing
in the response said the history had been cut, so the UI could not tell a short
conversation from a truncated one, and there was no way to ask for the rest.
"""

from types import SimpleNamespace

import pytest

from sbot.api.routes import list_messages


async def _session(stores, email="paging@sbot.ai", turns=0, tools=True):
    user = await stores["users"].get_or_create_by_email(email)
    session = await stores["sessions"].create(user.id, title="งาน")
    for i in range(turns):
        # A real turn interleaves tool traffic with the visible exchange, which
        # is what makes a page of the raw table not a page of the transcript.
        tool_rows = (
            [
                {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{i}"}]},
                {"role": "tool", "content": "ผลลัพธ์", "tool_call_id": f"t{i}"},
            ]
            if tools
            else []
        )
        await stores["messages"].append(
            session.id,
            [
                {"role": "user", "content": f"ถาม {i}"},
                *tool_rows,
                {"role": "assistant", "content": f"ตอบ {i}"},
            ],
        )
    return user, session


def _state(stores):
    return SimpleNamespace(sessions=stores["sessions"], messages=stores["messages"])


async def _page(stores, user, session_id, before_seq=None, limit=100):
    return await list_messages(
        session_id, before_seq=before_seq, limit=limit, user=user, state=_state(stores)
    )


@pytest.mark.asyncio
async def test_walking_back_yields_every_message_exactly_once(stores):
    user, session = await _session(stores, turns=10)

    seen: list[str] = []
    cursor, pages = None, 0
    while True:
        page = await _page(stores, user, session.id, before_seq=cursor, limit=7)
        pages += 1
        assert pages < 20, "the walk is not terminating"
        # Every page carries real transcript. The raw table is 3/4 tool traffic
        # here, so a reader that filtered after paging would hand back blanks.
        assert page["messages"], "a page came back with nothing to show"
        seen = [m["content"] for m in page["messages"]] + seen
        if not page["has_more"]:
            break
        cursor = page["next_before_seq"]

    assert pages > 1, "10 turns at 7 per page must take more than one page"
    expected = [c for i in range(10) for c in (f"ถาม {i}", f"ตอบ {i}")]
    assert seen == expected


@pytest.mark.asyncio
async def test_the_first_page_is_the_newest_and_reports_more(stores):
    """The transcript opens at the bottom, so page one is the tail."""
    user, session = await _session(stores, turns=6, tools=False)

    page = await _page(stores, user, session.id, limit=4)
    assert [m["content"] for m in page["messages"]] == ["ถาม 4", "ตอบ 4", "ถาม 5", "ตอบ 5"]
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_a_page_that_exactly_empties_the_history_reports_no_more(stores):
    """`has_more` is read from one row past the page, so the boundary case is a
    history whose length is an exact multiple of the page size — the naive
    "the page came back full" test promises a next page that is empty."""
    user, session = await _session(stores, turns=2, tools=False)

    page = await _page(stores, user, session.id, limit=4)
    assert len(page["messages"]) == 4
    assert page["has_more"] is False


@pytest.mark.asyncio
async def test_an_artifact_only_message_survives_a_reload(stores):
    """A generated image with no caption has no text — dropping empty content
    without checking artifacts would erase it from the transcript."""
    user, session = await _session(stores)
    await stores["messages"].append(
        session.id,
        [
            {"role": "user", "content": "วาดรูป"},
            {"role": "assistant", "content": "", "meta": {"artifacts": ["a.png"]}},
        ],
    )

    page = await _page(stores, user, session.id)
    assert [(m["meta"] or {}).get("artifacts") for m in page["messages"]] == [None, ["a.png"]]


@pytest.mark.asyncio
async def test_tool_call_narration_is_hidden_but_retained_for_agent_history(stores):
    user, session = await _session(stores, tools=False)
    await stores["messages"].append(
        session.id,
        [
            {"role": "user", "content": "สร้างเอกสาร"},
            {
                "role": "assistant",
                "content": "ผมจะตรวจข้อมูลก่อนครับ",
                "tool_calls": [{"id": "read-1", "function": {"name": "read_skill"}}],
            },
            {"role": "tool", "content": "skill content", "tool_call_id": "read-1"},
            {"role": "assistant", "content": "สร้างเอกสารเสร็จแล้ว"},
        ],
    )

    page = await _page(stores, user, session.id)
    assert [m["content"] for m in page["messages"]] == ["สร้างเอกสาร", "สร้างเอกสารเสร็จแล้ว"]

    # The display filter must not alter the durable model history: the next
    # turn still receives the assistant tool call and its matching result.
    history = await stores["messages"].recent(session.id)
    assert history[1]["content"] == "ผมจะตรวจข้อมูลก่อนครับ"
    assert history[1]["tool_calls"]


@pytest.mark.asyncio
async def test_another_users_session_is_not_readable(stores):
    _, session = await _session(stores, email="owner@sbot.ai", turns=1)
    intruder = await stores["users"].get_or_create_by_email("intruder@sbot.ai")

    with pytest.raises(Exception) as exc:
        await _page(stores, intruder, session.id)
    assert getattr(exc.value, "status_code", None) == 404
