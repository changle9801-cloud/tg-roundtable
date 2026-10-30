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

    a, b, c = make("a", ["嗯"]), make("b", ["好"]), make("c", [])
    cfg = RoundConfig(max_turns_per_model=2, call_timeout=5, follow_up_chance=0.5, follow_up_decay=0.4)
    room = ChatRoom("chat", [a, b, c], FakeSink(), cfg, store, RuleModerator(random.Random(1)))
    room.moderator.rng.random = lambda: 0.99  # nobody wants to: the most eager answers anyway
    room.add_user_message("阿明", "@C 你在吗")
    await room.wait_idle()
    room.add_user_message("阿明", "今天好冷")
    await room.wait_idle()
    report = room.decision_report()
    assert [g["ts"] for g in report] == sorted((g["ts"] for g in report), reverse=True)  # newest first
    by_message = {g["for"]: g for g in report}
    newest, older = by_message["阿明：今天好冷"], by_message["阿明：@C 你在吗"]
    owed = [(s["what"], s["who"], s["why"]) for s in older["steps"]]
    assert owed[:2] == [("欠的回答", "C", "阿明点了她"), ("没说", "C", "看了，觉得不用回（PASS）")]
    steps = newest["steps"]
    rolled = [s for s in steps if s["what"] == "想说话"]
    assert len(rolled) == 3 and not any(s["hit"] for s in rolled) and "还没回这条 0.2" in rolled[0]["why"]
    assert any(s["what"] == "保底" for s in steps) and any(s["what"] == "说了" for s in steps)


def test_who_likes_answering_whom():
    grok = Member("Grok", "high")
    fond = wants(grok, follows=True, fondness=0.2, of="Codex")
    cool = wants(grok, pending=True, fondness=-0.2, of="阿明")
    assert "爱接Codex的话 1.2" in fond.explain() and round(fond.value, 3) == round(0.45 * 1.3 * 1.2, 3)
    assert "懒得接阿明的话" in cool.explain() and round(cool.value, 3) == round(0.25 * 1.3 * 0.8, 3)
    assert wants(grok, fondness=0.3).value < 0.1  # no reason to speak stays no reason: it only strengthens one


def test_the_table_in_settings():
    import pytest

    from roundtable.config import ConfigError
    from roundtable.settings import default_settings, normalize_settings

    s = default_settings()
    s["participants"] = [{"id": i, "name": i.upper(), "kind": "api", "provider": "deepseek", "model": "m"} for i in "ab"]
    s["round"]["affinity"] = {"a": {"b": 0.2, "user": -0.1, "gone": 0.1, "a": 0.3}, "gone": {"a": 0.1}, "b": {"a": 0}}
    assert normalize_settings(s)["round"]["affinity"] == {"a": {"b": 0.2, "user": -0.1}}
    s["round"]["affinity"] = {"a": {"b": 0.5}}
    with pytest.raises(ConfigError):
        normalize_settings(s)


async def test_a_friend_she_likes_answering(make, store):
    from roundtable.config import RoundConfig

    grok, codex = make("grok", ["少写是我故意的"]), make("codex", ["嗯，照整套补齐 [画:Grok新版卡|金发双马尾的 Grok]"])
    grok.cfg.activity = "high"
    cfg = RoundConfig(max_turns_per_model=2, call_timeout=5, affinity={"grok": {"codex": 0.2}})
    room = ChatRoom("chat", [grok, codex], FakeSink(), cfg, store, RuleModerator(random.Random(0)))
    room.moderator.rng.random = lambda: 0.99
    room.add_user_message("阿明", "@Grok 你看看，@Codex 给她重做一张")
    await room.wait_idle()
    back = next(d for d in reversed(room.decisions) if d["who"] == "Grok")
    assert "爱接Codex的话 1.2" in back["why"] and back["chance"] >= 0.85  # the doc's worked example: about 0.9
