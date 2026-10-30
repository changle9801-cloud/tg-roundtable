from __future__ import annotations

import asyncio
import random
import time
from collections import Counter

from conftest import FakeSink

from nookling.moderator import RuleModerator
from nookling.participants import ParticipantError
from nookling.conversation import Conversation
from nookling.transcript import Message


def room_with(participants, round_cfg, store, seed=0):
    sink = FakeSink()
    room = Conversation("chat", participants, sink, round_cfg, store, RuleModerator(random.Random(seed)))
    return room, sink


async def test_each_model_speaks_at_most_max_turns(make, round_cfg, store):
    a = make("a", [f"a{i}" for i in range(10)])
    b = make("b", [f"b{i}" for i in range(10)])
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    counts = Counter(pid for pid, _ in sink.posts)
    assert counts == {"a": 3, "b": 3}


async def test_no_one_speaks_twice_in_a_row_with_two_models(make, round_cfg, store):
    a = make("a", [f"a{i}" for i in range(10)])
    b = make("b", [f"b{i}" for i in range(10)])
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    speakers = [pid for pid, _ in sink.posts]
    assert all(x != y for x, y in zip(speakers, speakers[1:]))


async def test_round_ends_when_everyone_passes(make, round_cfg, store):
    a = make("a", ["hello", "[PASS]"])
    b = make("b", ["PASS"])
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert sink.posts == [("a", "hello")]
    assert len(a.prompts) + len(b.prompts) <= 3


async def test_models_see_each_other_but_not_themselves(make, round_cfg, store):
    a = make("a", ["from a", "[PASS]"])
    b = make("b", ["from b", "[PASS]"])
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("Ming", "hello all")
    await room.wait_idle()

    first, second = sink.posts[0][0], sink.posts[1][0]
    second_model = a if second == "a" else b
    prompt = second_model.prompts[0]
    assert "Ming（用户）: hello all" in prompt
    assert f"from {first}" in prompt
    assert f"from {second}" not in prompt


async def test_mentioned_model_goes_first(make, round_cfg, store):
    for seed in range(5):
        a = make("a", ["a says"])
        b = make("b", ["b says"], aliases=["小B"])
        room, sink = room_with([a, b], round_cfg, store, seed=seed)
        room.add_user_message("me", "小B 你怎么看")
        await room.wait_idle()
        assert sink.posts[0][0] == "b"


async def test_reply_to_model_goes_first(make, round_cfg, store):
    for seed in range(5):
        a = make("a", ["a says"])
        b = make("b", ["b says"])
        room, sink = room_with([a, b], round_cfg, store, seed=seed)
        room.add_user_message("me", "really?", reply_to="a")
        await room.wait_idle()
        assert sink.posts[0][0] == "a"


async def test_failing_model_is_reported_and_skipped(make, round_cfg, store):
    a = make("a", [ParticipantError("boom")])
    b = make("b", ["fine", "[PASS]"])
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert ("b", "fine") in sink.posts
    assert any("boom" in n for n in sink.notices)
    assert len(a.prompts) == 1


async def test_new_user_message_restarts_quotas(make, round_cfg, store):
    a = make("a", [f"a{i}" for i in range(20)], delay=0.01)
    b = make("b", [f"b{i}" for i in range(20)], delay=0.01)
    room, sink = room_with([a, b], round_cfg, store)

    room.add_user_message("me", "first")
    await asyncio.sleep(0.025)  # a couple of turns in
    room.add_user_message("me", "second")
    await room.wait_idle()

    counts = Counter(pid for pid, _ in sink.posts)
    assert counts["a"] > 3 or counts["b"] > 3
    # The new message reaches both models.
    assert any("me（用户）: second" in p for p in a.prompts)
    assert any("me（用户）: second" in p for p in b.prompts)


async def test_stop_cancels_round(make, round_cfg, store):
    a = make("a", ["slow"], delay=10)
    room, sink = room_with([a], round_cfg, store)

    room.add_user_message("me", "hi")
    await asyncio.sleep(0.01)
    assert await room.stop()
    assert not room.busy
    assert sink.posts == []


async def test_single_model_does_not_talk_to_itself(make, round_cfg, store):
    a = make("a", ["one", "two", "three"])
    room, sink = room_with([a], round_cfg, store)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    assert sink.posts == [("a", "one")]


async def test_after_a_reset_a_member_sees_its_own_lines(make, round_cfg, store):
    a = make("a", ["a again"])
    b = make("b", ["[PASS]"])
    room, sink = room_with([a, b], round_cfg, store)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    said = sink.posts[0][1]
    room.reset_member(a)
    room.add_user_message("me", "@a still there?", reply_to="a")
    await room.wait_idle()
    assert f"A（你自己）: {said}" in a.prompts[-1]


async def test_the_member_just_spoken_to_answers_first(make, round_cfg, store):
    for seed in range(5):
        a = make("a", ["a says"])
        b = make("b", ["b says"])
        room, sink = room_with([a, b], round_cfg, store, seed=seed)
        room.moderator.rng.random = lambda: 0.0  # always take the hint
        room._append(Message("b", "B", "b said something", (), time.time()))
        room.add_user_message("me", "you sure?")  # no @, no reply: still talking to b
        await room.wait_idle()
        assert sink.posts[0][0] == "b"


async def test_no_one_keeps_the_first_answer_forever(make, round_cfg, store):
    a, b = make("a", []), make("b", [])
    room, _ = room_with([a, b], round_cfg, store)
    room.moderator.rng.random = lambda: 0.0
    now = time.time()
    for k, (who, text) in enumerate([("user", "hi"), ("a", "yo"), ("user", "and?"), ("b", "eh")]):
        room._append(Message(who, who, text, (), now + k))
    latest = Message("user", "me", "really?", (), now + 5)
    room._append(latest)
    assert room._answering(latest) == "b"  # answered first once so far
    room._append(Message("b", "b", "yes", (), now + 6))
    latest = Message("user", "me", "sure?", (), now + 7)
    room._append(latest)
    assert room._answering(latest) is None  # twice running: no one is favoured


async def test_talking_to_everyone_brings_more_replies(make, store):
    from nookling.config import RoundConfig

    def second_replies(text):
        a, b = make("a", ["嗯"]), make("b", ["嗯"])
        cfg = RoundConfig(max_turns_per_model=1, call_timeout=5, follow_up_chance=0.5, follow_up_decay=0.4)
        room = Conversation("chat", [a, b], FakeSink(), cfg, store, RuleModerator(random.Random(0)))
        room.moderator.rng.random = lambda: 0.4  # between a plain second reply (0.25) and one to everyone (0.55)
        room.add_user_message("me", text)
        return room

    for text, both in (("今天好冷", False), ("你们觉得冷吗", True)):
        room = second_replies(text)
        await room.wait_idle()
        assert len(room.sink.posts) == (2 if both else 1)


async def test_a_cut_off_round_picks_up_again(make, round_cfg, store):
    # Settings saved while a reply was on its way: the round stops, then resumes after the rebuild.
    a = make("a", ["嗯，在呢"], delay=0.3)
    room, sink = room_with([a], round_cfg, store)
    room.add_user_message("me", "有人吗")
    await asyncio.sleep(0.05)
    assert await room.stop() and sink.posts == []
    room.resume()
    await room.wait_idle()
    assert sink.posts == [("a", "嗯，在呢")]
