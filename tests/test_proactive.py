from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta

import pytest
from conftest import FakeSink

from nookling.config import ParticipantConfig, RoundConfig
from nookling.proactive import ProactiveScheduler, find_urls, seconds_quiet
from nookling.prompts import build_proactive_prompt
from nookling.conversation import Conversation
from nookling.settings import parse_hours
from nookling.transcript import Message


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


def member(**kw) -> ParticipantConfig:
    return ParticipantConfig(id="grok", name="Grok", kind="grok_cli", proactive=True, **kw)


@pytest.mark.parametrize("text,expected", [
    ("10-23", (10, 23)), ("22-2", (22, 2)), ("0-24", (0, 24)), (" 9 ~ 21 ", (9, 21)),
    ("10-10", None), ("25-3", None), ("abc", None), ("", None),
])
def test_parse_hours(text, expected):
    assert parse_hours(text) == expected


def test_hours_window_including_past_midnight(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 23, 30))
    s = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    assert s.in_hours(member(proactive_hours="22-2"))
    assert not s.in_hours(member(proactive_hours="10-23"))
    clock.at = datetime(2026, 10, 26, 1, 0)
    assert s.in_hours(member(proactive_hours="22-2"))


def test_first_check_waits_then_spreads_posts_over_the_day(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 10, 0))
    s = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    pc = member(proactive_per_day=3, proactive_hours="10-22")

    assert not s.due(pc)  # 10 to 30 minutes after switching on
    clock.advance(minutes=31)
    assert s.due(pc)

    s.record(pc, "看这个 https://x.com/a/1")
    assert not s.due(pc)
    gap = s.plan(pc).next_at - clock().timestamp()
    assert 0.7 * 4 * 3600 <= gap <= 1.3 * 4 * 3600  # 12 hours / 3 posts, with jitter


def test_daily_cap_and_new_day(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 12, 0))
    s = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    pc = member(proactive_per_day=1)
    s.plan(pc).next_at = 0
    s.record(pc, "posted")
    s.plan(pc).next_at = 0
    assert not s.due(pc)  # one post a day

    clock.at = datetime(2026, 10, 26, 12, 0)
    s.plan(pc).next_at = 0
    assert s.due(pc)
    assert s.plan(pc).posts == 0


def test_passes_are_capped_too(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 12, 0))
    s = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    pc = member(proactive_per_day=2)
    for _ in range(4):
        s.plan(pc).next_at = 0
        s.record(pc, None)
    s.plan(pc).next_at = 0
    assert not s.due(pc)  # at most twice the daily posts in attempts


def test_shared_links_are_remembered_across_restarts(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 12, 0))
    pc = member()
    ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock).record(pc, "a https://x.com/a/1。 b https://x.com/b/2")
    again = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    assert again.recent_urls(pc) == ["https://x.com/a/1", "https://x.com/b/2"]


def test_postpone_and_disabled(tmp_path):
    clock = Clock(datetime(2026, 10, 25, 12, 0))
    s = ProactiveScheduler(tmp_path / "p.json", random.Random(0), clock)
    pc = member()
    s.plan(pc).next_at = 0
    s.postpone(pc)
    assert not s.due(pc)
    clock.advance(minutes=6)
    assert s.due(pc)
    pc.proactive = False
    assert not s.due(pc)


def test_helpers():
    assert find_urls("看（https://x.com/a/1）和 https://example.com/p?q=1。") == ["https://x.com/a/1", "https://example.com/p?q=1"]
    assert seconds_quiet(0) == float("inf")
    assert seconds_quiet(100.0, now=160.0) == 60.0


def test_proactive_prompt():
    with_search = build_proactive_prompt([], "宇宙", True, True, ["https://x.com/old"])
    assert "X（推特）" in with_search and "宇宙" in with_search and "https://x.com/old" in with_search
    assert "[PASS]" in with_search
    plain = build_proactive_prompt([], "", False, False, [])
    assert "不要假装看到了什么新闻" in plain


def room_for(members, store, **round_kw):
    sink = FakeSink()
    cfg = RoundConfig(max_turns_per_model=3, call_timeout=5, follow_up_chance=1.0, follow_up_decay=1.0, **round_kw)
    return Conversation("chat", members, sink, cfg, store), sink


async def test_proactive_post_then_short_reactions(make, store):
    grok = make("grok", ["刷到这个笑死 https://x.com/a/1", "对吧", "还有", "更多"])
    a = make("a", [f"a{i}" for i in range(5)])
    b = make("b", [f"b{i}" for i in range(5)])
    room, sink = room_for([grok, a, b], store)

    future = room.start_proactive(grok, lambda unseen: "PROACTIVE", reply_turns=1)
    assert await future == "刷到这个笑死 https://x.com/a/1"
    await room.wait_idle()

    assert grok.prompts[0] == "PROACTIVE"
    counts = Counter(pid for pid, _ in sink.posts)
    assert counts["a"] <= 1 and counts["b"] <= 1  # one reaction each
    assert counts["grok"] <= 2  # its post, plus at most one answer
    assert sink.posts[0] == ("grok", "刷到这个笑死 https://x.com/a/1")


async def test_proactive_pass_posts_nothing(make, store):
    grok = make("grok", ["[PASS]"])
    a = make("a", ["a0"])
    room, sink = room_for([grok, a], store)
    assert await room.start_proactive(grok, lambda unseen: "P", reply_turns=1) is None
    await room.wait_idle()
    assert sink.posts == []


async def test_no_proactive_while_a_round_runs(make, store):
    slow = make("slow", ["x"], delay=0.2)
    room, sink = room_for([slow], store)
    room.add_user_message("me", "hi")
    assert room.start_proactive(slow, lambda unseen: "P", reply_turns=1) is None
    await room.wait_idle()


async def test_proactive_sees_what_it_missed(make, store):
    grok = make("grok", ["ok"])
    room, sink = room_for([grok], store)
    room.messages.append(Message("user", "me", "早上好", (), 1.0))
    seen = []
    await room.start_proactive(grok, lambda unseen: seen.extend(m.text for m in unseen) or "P", reply_turns=0)
    assert seen == ["早上好"]
