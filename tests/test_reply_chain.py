"""Most messages get one or two replies, not one from everyone."""

from __future__ import annotations

import random
from statistics import mean

from conftest import FakeSink

from roundtable.config import RoundConfig
from roundtable.moderator import RuleModerator
from roundtable.prompts import DEFAULT_SYSTEM_PROMPT, FRIEND_RULES, build_turn_prompt
from roundtable.room import ChatRoom
from roundtable.telegram_bot import bubbles, typing_pause
from roundtable.transcript import Message


def room_for(members, store, chance, decay, seed=0):
    sink = FakeSink()
    cfg = RoundConfig(max_turns_per_model=3, call_timeout=5, follow_up_chance=chance, follow_up_decay=decay)
    return ChatRoom("chat", members, sink, cfg, store, RuleModerator(random.Random(seed))), sink


def four(make):
    return [make(pid, [f"{pid}{i}" for i in range(6)]) for pid in ("a", "b", "c", "d")]


async def test_zero_chance_means_exactly_one_reply(make, store):
    room, sink = room_for(four(make), store, chance=0.0, decay=0.0)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert len(sink.posts) == 1


async def test_an_at_mention_always_gets_an_answer(make, store):
    a = make("a", ["@B 你说呢"])
    b = make("b", ["我觉得行"])
    c = make("c", ["c0"])
    room, sink = room_for([a, b, c], store, chance=0.0, decay=0.0)
    room.add_user_message("me", "A 你好")  # a answers first
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b"]


async def test_full_chance_lets_everyone_speak(make, store):
    room, sink = room_for(four(make), store, chance=1.0, decay=1.0)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert {pid for pid, _ in sink.posts} == {"a", "b", "c", "d"}


async def test_default_chances_keep_it_short(make, store):
    counts = []
    for seed in range(60):
        room, sink = room_for(four(make), store, chance=0.5, decay=0.4, seed=seed)
        room.add_user_message("me", "hi")
        await room.wait_idle()
        counts.append(len(sink.posts))
    assert 1.2 < mean(counts) < 2.0  # about 1 + 0.5 + 0.1
    assert max(counts) <= 4


async def test_a_new_user_message_starts_a_fresh_chain(make, store):
    members = four(make)
    room, sink = room_for(members, store, chance=0.0, decay=0.0)
    room.add_user_message("me", "one")
    await room.wait_idle()
    room.add_user_message("me", "two")
    await room.wait_idle()
    assert len(sink.posts) == 2


def test_turn_prompt_has_no_assignment_or_quota():
    prompt = build_turn_prompt([Message("user", "阿明", "hi")], 3)
    assert "轮到你" not in prompt and "还可以发言" not in prompt
    assert "[PASS]" in prompt


def test_default_rules_include_friend_rules():
    assert FRIEND_RULES in DEFAULT_SYSTEM_PROMPT and "{friend_rules}" not in DEFAULT_SYSTEM_PROMPT
    assert "打圆场" in FRIEND_RULES and "我收手" in FRIEND_RULES


def test_bubbles_and_pauses():
    assert bubbles("哈哈\n\n你这也太离谱了\n\n\n不过行吧") == ["哈哈", "你这也太离谱了", "不过行吧"]
    assert bubbles("一行\n两行") == ["一行\n两行"]
    assert len(bubbles("\n\n".join(str(i) for i in range(7)))) == 4
    assert typing_pause("短", first=True) < typing_pause("长" * 60, first=False) <= 5.0
