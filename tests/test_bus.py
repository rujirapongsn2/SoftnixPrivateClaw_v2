import asyncio

from claw.core.bus import EventBus
from claw.core.events import TextDeltaEvent


async def test_publish_reaches_all_subscribers():
    bus = EventBus()
    async with bus.subscribe("s1") as q1, bus.subscribe("s1") as q2:
        bus.publish("s1", TextDeltaEvent(turn_id="t", text="hello"))
        assert (await asyncio.wait_for(q1.get(), 1)).text == "hello"
        assert (await asyncio.wait_for(q2.get(), 1)).text == "hello"


async def test_sessions_are_isolated():
    bus = EventBus()
    async with bus.subscribe("s1") as q1, bus.subscribe("s2") as q2:
        bus.publish("s1", TextDeltaEvent(turn_id="t", text="only-s1"))
        assert (await asyncio.wait_for(q1.get(), 1)).text == "only-s1"
        assert q2.empty()


async def test_unsubscribe_cleans_up():
    bus = EventBus()
    async with bus.subscribe("s1"):
        assert bus.subscriber_count("s1") == 1
    assert bus.subscriber_count("s1") == 0
