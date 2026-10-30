"""The urge to speak: named reasons, explained (docs/urge-to-speak.md)."""

from __future__ import annotations

import random

from conftest import FakeSink

from roundtable.moderator import RuleModerator
from roundtable.room import ChatRoom
from roundtable.urge import Urge, join_in, pick_weight, talk_back


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


def test_todays_numbers():
    grok, claude = Member("Grok", "high"), Member("Claude", "low")
    assert join_in(grok, 0, to_all=False, busy=False, chance=0.5, decay=0.4) is None  # the first reply always comes
    assert join_in(claude, 1, to_all=False, busy=False, chance=0.5, decay=0.4).value == 0.25
    assert round(join_in(grok, 2, to_all=True, busy=True, chance=0.5, decay=0.4).value, 3) == round(0.9 * 0.55 * 1.5 * 0.4, 3)
    assert talk_back(grok, named=False, busy=False).value == 0.6 * 1.5
    assert "被朋友点名" in talk_back(grok, named=True, busy=False).explain()
    assert pick_weight(Member("D", "normal", "宇宙，游戏"), "聊聊游戏").value == 3.0
    assert pick_weight(Member("D", "low"), "聊聊游戏").value == 0.5


async def test_the_room_keeps_why_it_rolled(make, store):
    from roundtable.config import RoundConfig

    a, b = make("a", ["嗯"]), make("b", ["好"])
    cfg = RoundConfig(max_turns_per_model=2, call_timeout=5, follow_up_chance=0.5, follow_up_decay=0.4)
    room = ChatRoom("chat", [a, b], FakeSink(), cfg, store, RuleModerator(random.Random(1)))
    room.add_user_message("me", "hi")
    await room.wait_idle()
    [roll, *_] = room.decisions
    assert roll["what"] == "接话" and "第 2 个接话 0.5" in roll["why"] and isinstance(roll["hit"], bool)
