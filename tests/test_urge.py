"""The urge to speak: named reasons, explained (docs/urge-to-speak.md)."""

from __future__ import annotations

import random

from conftest import FakeSink

from roundtable.moderator import RuleModerator
from roundtable.room import ChatRoom
from roundtable.urge import Urge, pick_weight, wants_to_speak


class Member:
    def __init__(self, name, activity="normal", browse_topics=""):
        self.name = name
        self.cfg = type("Cfg", (), {"activity": activity, "browse_topics": browse_topics, "proactive_topics": ""})()


def test_parts_add_up_and_factors_multiply():
    u = Urge("Grok", [("接她的话", 0.4), ("说的是她", 0.4)], [("活跃度高", 1.3), ("疲劳", 0.5)])
    assert round(u.value, 2) == 0.52
    assert u.explain() == "Grok 0.52（接她的话 0.4 + 说的是她 0.4；×活跃度高 1.3 ×疲劳 0.5）"
    assert Urge("x", [("a", 0.9), ("b", 0.9)]).value == 1.0  # a chance stops at 1
    assert Urge("x", [("a", 2.0)], [("对上兴趣", 3)], cap=None).value == 6.0  # a pick weight does not


def wants(p, **kw):
    args = dict(pending=False, replies=0, answering=False, to_all=False, named=False, follows=False,
                interested=False, said=0, busy=False, liveliness=1.0, crowding=0.4)
    return wants_to_speak(p, **{**args, **kw})


def test_what_makes_her_want_to_speak():
    grok, claude = Member("Grok", "high"), Member("Claude", "low")
    first = wants(claude, pending=True, replies=0)
    third = wants(claude, pending=True, replies=2)
    assert round(first.value, 3) == round((0.2 + 0.05) * 0.6, 3) and third.value < first.value  # crowded
    assert wants(claude, pending=True, to_all=True).value > first.value
    comeback = wants(grok, follows=True, said=1)
    assert round(comeback.value, 3) == round(0.45 * 1.3 * 0.6, 3) and "接她的话" in comeback.explain()
    assert "被朋友喊名字" in wants(grok, named=True, follows=True).explain()  # named counts, not both
    assert wants(grok, pending=True, liveliness=0).value == 0  # only owed answers
    assert wants(claude, liveliness=2).value == 1 and "拉满" in wants(claude, liveliness=2).explain()
    assert wants(grok, pending=True, busy=True).value < wants(grok, pending=True).value
    assert pick_weight(Member("D", "normal", "宇宙，游戏"), "聊聊游戏").value == 3.0
    assert pick_weight(Member("D", "low"), "聊聊游戏").value == 0.5


async def test_the_room_keeps_why_it_rolled(make, store):
    from roundtable.config import RoundConfig

    a, b = make("a", ["嗯"]), make("b", ["好"])
    cfg = RoundConfig(max_turns_per_model=2, call_timeout=5, follow_up_chance=0.5, follow_up_decay=0.4)
    room = ChatRoom("chat", [a, b], FakeSink(), cfg, store, RuleModerator(random.Random(1)))
    room.add_user_message("me", "hi")
    await room.wait_idle()
    rolls = list(room.decisions)
    assert {r["what"] for r in rolls} == {"想说话"} and len(rolls) >= 2  # both rolled for the user's message
    assert "还没回这条 0.2" in rolls[0]["why"] and isinstance(rolls[0]["hit"], bool)
