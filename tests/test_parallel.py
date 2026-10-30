"""Slow speakers: others may talk meanwhile, and see what the slow one is doing."""

from __future__ import annotations

import asyncio
import random

from conftest import FakeSink

from nookling.config import RoundConfig
from nookling.moderator import RuleModerator
from nookling.conversation import Conversation


class FirstMentioned(RuleModerator):
    """Deterministic: pick in member order."""

    def pick(self, candidates, last_message, last_speaker_id):
        return candidates[0] if candidates else None


def room_for(members, store, sink=None, **round_kw):
    cfg = RoundConfig(max_turns_per_model=1, call_timeout=5, follow_up_chance=1.0, follow_up_decay=1.0, **round_kw)
    sink = sink or FakeSink()
    return Conversation("chat", members, sink, cfg, store, FirstMentioned(random.Random(0))), sink


async def test_fast_member_speaks_while_slow_one_searches(make, store):
    slow = make("slow", ["slow answer"], delay=0.3, activity="正在 X 上搜索")
    fast = make("fast", ["fast answer"])
    room, sink = room_for([slow, fast], store, slow_after=0.05, max_parallel=2)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert [pid for pid, _ in sink.posts] == ["fast", "slow"]
    # The fast one was told the slow one is searching.
    assert "Slow 正在 X 上搜索" in fast.prompts[0]
    # The notice is shown, then removed once the answer arrives.
    assert sink.activities == [("show", "slow", "正在 X 上搜索"), ("clear", "slow", "")]


async def test_one_at_a_time_when_parallel_is_one(make, store):
    slow = make("slow", ["slow answer"], delay=0.15)
    fast = make("fast", ["fast answer"])
    room, sink = room_for([slow, fast], store, slow_after=0.01, max_parallel=1)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert [pid for pid, _ in sink.posts] == ["slow", "fast"]
    assert "slow answer" in fast.prompts[0]  # it waited and saw the slow reply


async def test_quick_replies_stay_sequential(make, store):
    a = make("a", ["a1"], delay=0.01)
    b = make("b", ["b1"], delay=0.01)
    room, sink = room_for([a, b], store, slow_after=5, max_parallel=2)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert [pid for pid, _ in sink.posts] == ["a", "b"]
    assert "A: a1" in b.prompts[0]


async def test_never_more_than_max_parallel(make, store):
    running = 0
    peak = 0

    class Counting:
        def __init__(self, member):
            self.member = member
            original = member.reply

            async def reply(chat_key, prompt, on_activity=None):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                try:
                    return await original(chat_key, prompt, on_activity)
                finally:
                    running -= 1

            member.reply = reply

    members = [make(pid, [f"{pid} says"], delay=0.1) for pid in ("a", "b", "c", "d")]
    for m in members:
        Counting(m)
    room, sink = room_for(members, store, slow_after=0.01, max_parallel=2)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert peak == 2
    assert len(sink.posts) == 4


async def test_user_message_gets_an_answer_while_a_slow_one_runs(make, store):
    slow = make("slow", ["slow answer"], delay=0.3)
    fast = make("fast", ["answer to second"])
    room, sink = room_for([slow, fast], store, slow_after=10, max_parallel=2)

    room.add_user_message("me", "first")
    await asyncio.sleep(0.05)  # slow is thinking; with slow_after=10 nobody else started
    room.add_user_message("me", "second")
    await room.wait_idle()

    assert "me（用户）: second" in fast.prompts[0]
    assert sink.posts.index(("fast", "answer to second")) < sink.posts.index(("slow", "slow answer"))


async def test_stop_cancels_everyone_and_clears_notices(make, store):
    slow = make("slow", ["x"], delay=5, activity="正在联网搜索")
    other = make("other", ["y"], delay=5)
    room, sink = room_for([slow, other], store, slow_after=0.01, max_parallel=2)

    room.add_user_message("me", "hi")
    await asyncio.sleep(0.1)
    assert await room.stop()

    assert sink.posts == []
    assert ("clear", "slow", "") in sink.activities
