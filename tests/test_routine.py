"""Daily routines: sleeping members don't answer, busy ones answer less, waking up and going to bed."""

from __future__ import annotations

import asyncio
from datetime import datetime

from conftest import FakeSink

from roundtable.moderator import RuleModerator
from roundtable.room import ChatRoom
from roundtable.routine import (
    ASLEEP, BUSY, ONLINE, Routines, describe_routine, fell_asleep, parse_range, status_at, valid_range,
)
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore, normalize_settings
from roundtable.transcript import Message

NIGHT = datetime(2026, 10, 27, 3, 0)  # a Tuesday
WORKDAY = datetime(2026, 10, 27, 14, 0)
SATURDAY = datetime(2026, 10, 31, 14, 0)


class Cfg:
    def __init__(self, sleep="", busy="", busy_doing=""):
        self.sleep, self.busy, self.busy_doing = sleep, busy, busy_doing


def test_ranges_and_states():
    assert parse_range("00:30-10:30") == (30, 630)
    assert parse_range("23:00~07:00") == (1380, 420)
    assert parse_range("10-23") is None and parse_range("10:00-10:00") is None
    assert valid_range("") and not valid_range("晚上")
    owl = Cfg(sleep="03:30-11:00")
    worker = Cfg(sleep="23:30-07:30", busy="09:30-18:00", busy_doing="在上班")
    assert status_at(owl, NIGHT) == ONLINE and status_at(worker, NIGHT) == ASLEEP  # wraps past midnight
    assert status_at(worker, WORKDAY) == BUSY and status_at(worker, SATURDAY) == ONLINE  # weekdays only
    assert describe_routine(worker) == "你的作息：一般 23:30 睡，07:30 起；工作日 09:30 到 18:00 在上班。"
    assert describe_routine(Cfg()) == ""


def test_settings_check_the_format(tmp_path):
    member = {"id": "a", "name": "A", "kind": "api", "provider": "deepseek", "model": "m", "sleep": "晚上睡"}
    try:
        normalize_settings({"participants": [member]})
    except Exception as e:
        assert "睡觉时间" in str(e)
    else:
        raise AssertionError("bad range accepted")
    member["sleep"] = "00:30-10:30"
    assert normalize_settings({"participants": [member]})["participants"][0]["sleep"] == "00:30-10:30"


class Dice:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value

    def choice(self, items):
        return items[0]

    def choices(self, items, weights=None):
        return [items[0]]


def room_with(make, store, round_cfg, routines, *members, rng=None):
    return ChatRoom("-1", members, FakeSink(), round_cfg, store, routines=routines,
                    moderator=RuleModerator(rng) if rng else None)


async def test_sleepers_stay_quiet_unless_called(make, store, round_cfg):
    a, b = make("a", ["在呢", "嗯"]), make("b", ["……谁啊，刚睡着"])
    b.cfg.sleep = "00:00-23:59"
    routines = Routines()
    room = room_with(make, store, round_cfg, routines, a, b)
    room.add_user_message("阿明", "有人吗")
    await room.wait_idle()
    assert room.sink.posts == [("a", "在呢")] and b.prompts == []
    assert "B 在睡觉（一般 23:59 起）" in a.prompts[0]

    room.add_user_message("阿明", "@B 起床了")
    await room.wait_idle()
    assert ("b", "……谁啊，刚睡着") in room.sink.posts
    assert "刚被群里吵醒" in b.prompts[0] and routines.status(b) == ONLINE


async def test_when_everyone_sleeps_one_may_wake(make, store, round_cfg):
    a, b = make("a", ["唔……几点了"]), make("b", ["zzz"])
    for p in (a, b):
        p.cfg.sleep = "00:00-23:59"
    room = room_with(make, store, round_cfg, Routines(), a, b, rng=Dice(0.0))
    room.add_user_message("阿明", "睡不着")
    await room.wait_idle()
    assert room.sink.posts == [("a", "唔……几点了")]

    c = make("c", ["zzz"])
    c.cfg.sleep = "00:00-23:59"
    quiet = room_with(make, store, round_cfg, Routines(), c, rng=Dice(0.9))
    quiet.add_user_message("阿明", "睡不着")
    await quiet.wait_idle()
    assert quiet.sink.posts == []  # nobody up: no answer until morning


async def test_busy_members_answer_after_free_ones(make, store, round_cfg):
    a, b = make("a", ["我在上班，晚点说"]), make("b", ["来了"])
    a.cfg.busy, a.cfg.busy_doing = "00:00-23:59", "在上班"
    routines = Routines(now=lambda: WORKDAY)
    room = room_with(make, store, round_cfg, routines, a, b, rng=Dice(0.0))
    room.add_user_message("阿明", "聊天")
    await room.wait_idle()
    assert room.sink.posts[0] == ("b", "来了")
    assert "你现在在上班" in a.prompts[0] and "A 在上班（23:59 以后有空）" in b.prompts[0]


class FakeRoom:
    def __init__(self, unseen):
        self.key, self.busy, self.unseen, self.prompts = "-1", False, unseen, []
        self.messages = list(unseen) or [Message("a", "A", "旧消息", (), 1.0)]
        self.last_activity = 0.0

    def _unseen(self, p):
        return self.unseen

    def start_proactive(self, speaker, make_prompt, reply_turns):
        self.prompts.append((speaker.id, make_prompt(self.unseen), reply_turns))
        future = asyncio.get_event_loop().create_future()
        future.set_result("早")
        return future


async def test_waking_up_answers_what_was_missed(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.refresh_prompts = lambda: None
    cfg = type("C", (), {"kind": "api", "sleep": "01:00-09:00", "busy": "", "busy_doing": ""})()
    member = type("P", (), {"id": "ds", "name": "DeepSeek", "cfg": cfg})()
    rt.participants = [member]
    before_bed = datetime(2026, 10, 27, 0, 30).timestamp()  # talked about before it slept: not missed
    room = FakeRoom([Message("user", "阿明", "辞职算了", (), before_bed),
                     Message("user", "阿明", "你们睡了吗", (), datetime(2026, 10, 27, 3, 0).timestamp())])
    rt.room = lambda chat: room
    rt.proactive_chat = lambda: "-1"
    clock = [datetime(2026, 10, 27, 8, 59)]
    rt.routines.now = lambda: clock[0]
    await rt._routine_tick()  # first look: nothing to compare with
    assert room.prompts == [] and rt._asleep(member)
    clock[0] = datetime(2026, 10, 27, 9, 1)
    await rt._routine_tick()
    assert room.prompts[0][0] == "ds" and "你睡着时群里的消息" in room.prompts[0][1] and "你们睡了吗" in room.prompts[0][1]
    assert "辞职算了" not in room.prompts[0][1]


def test_fell_asleep_is_the_latest_bedtime():
    assert fell_asleep(Cfg(sleep="23:30-07:30"), datetime(2026, 10, 27, 7, 0)) == datetime(2026, 10, 26, 23, 30)
    assert fell_asleep(Cfg(sleep="01:00-10:30"), datetime(2026, 10, 27, 11, 0)) == datetime(2026, 10, 27, 1, 0)
    assert fell_asleep(Cfg(), datetime(2026, 10, 27, 11, 0)) is None


async def test_sleeping_members_skip_their_own_plans(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    cfg = type("C", (), {"kind": "api", "sleep": "00:00-23:59", "busy": "", "busy_doing": ""})()
    member = type("P", (), {"id": "ds", "name": "DeepSeek", "cfg": cfg})()
    rt.participants = [member]
    rt.followups.now = lambda: datetime(2026, 10, 27, 12, 0)
    item = rt.followups.add("问问面试", datetime(2026, 10, 27, 10, 0), "-1", "ds")
    assert rt._followup_asker(item) is None  # nobody awake to ask
    await rt._followup_tick()
    assert item.status == "pending"  # asked later, not dropped
