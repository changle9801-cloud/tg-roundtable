"""Remembering what the user has coming up, and asking about it later."""

from __future__ import annotations

import asyncio
from datetime import datetime

from conftest import FakeSink

from roundtable.followups import FollowUpStore, build_followup_prompt, extract_followups, parse_when
from roundtable.room import ChatRoom
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore
from roundtable.transcript import Message

NOW = datetime(2026, 10, 26, 14, 0)


def test_parse_when():
    assert parse_when("2026-10-27 18:00", NOW) == datetime(2026, 10, 27, 18, 0)
    assert parse_when("10-27", NOW) == datetime(2026, 10, 27, 20, 0)  # no time: in the evening
    assert parse_when("01-03 09:30", datetime(2026, 12, 20)) == datetime(2027, 1, 3, 9, 30)  # next year
    assert parse_when("2026-10-20 10:00", NOW) is None  # the past
    assert parse_when("2027-06-01", NOW) is None  # too far ahead
    assert parse_when("明天", NOW) is None


def test_tags_are_hidden_and_collected():
    text, found = extract_followups("加油！[跟进:2026-10-27 18:00|阿明今天面试，问问结果]", NOW)
    assert text == "加油！" and found == [(datetime(2026, 10, 27, 18, 0), "阿明今天面试，问问结果")]
    text, found = extract_followups("[跟进：昨天|不对]好", NOW)
    assert text == "好" and found == []


def test_store_dedupes_moves_to_daytime_and_caps(tmp_path):
    clock = [NOW]
    store = FollowUpStore(tmp_path, lambda: clock[0])
    a = store.add("阿明面试，问问结果", datetime(2026, 10, 27, 3, 0), "-1", "grok")
    assert datetime.fromtimestamp(a.due) == datetime(2026, 10, 27, 9, 0)  # not at 3am
    assert store.add("阿明面试问问结果", datetime(2026, 10, 27, 18, 0), "-1", "claude") is None
    late = store.add("看病结果", datetime(2026, 10, 26, 23, 30), "-1")
    assert datetime.fromtimestamp(late.due) == datetime(2026, 10, 27, 9, 0)
    for i in range(3):
        store.add(f"第{i}件事要问", datetime(2026, 10, 26, 15, 0), "-1")
    clock[0] = datetime(2026, 10, 26, 16, 0)
    due = store.due()
    assert [f.text for f in due] == ["第0件事要问", "第1件事要问", "第2件事要问"]
    for f in due:
        store.finish(f, asked=True)
    clock[0] = datetime(2026, 10, 27, 10, 0)
    assert len(store.due()) == 2  # a new day; and only in the daytime
    clock[0] = datetime(2026, 10, 27, 23, 30)
    assert store.due() == []
    assert "已经记下、到时候要问的事" in store.prompt_section() and "看病结果" in store.prompt_section()
    assert FollowUpStore(tmp_path).pending()[0].text == "阿明面试，问问结果"  # saved


def test_asking_prompt():
    store_item = type("F", (), {"text": "阿明今天面试，问问结果"})()
    prompt = build_followup_prompt(store_item, [Message("user", "阿明", "我回来了")], "（现在是……）")
    assert "阿明今天面试" in prompt and "我回来了" in prompt and "[PASS]" in prompt and "提醒一下" in prompt


async def test_room_collects_tags_and_nudges(make, store, round_cfg):
    a = make("a", ["加油 [跟进:2026-10-27 18:00|阿明面试，问问结果]"])
    noted = []
    sink = FakeSink()
    room = ChatRoom("-1", [a], sink, round_cfg, store, on_followup=lambda chat, p, items: noted.append((chat, p.id, items)))
    room.add_user_message("阿明", "明天要去面试了")
    await room.wait_idle()
    assert sink.posts == [("a", "加油")]
    assert noted[0][0] == "-1" and noted[0][1] == "a" and noted[0][2][0][1] == "阿明面试，问问结果"
    assert "刚才有人提到了以后的安排" in a.prompts[0]


class FakeRoom:
    def __init__(self, busy=False, reply="面试怎么样？"):
        self.busy, self.reply, self.key, self.prompts = busy, reply, "-1", []

    def start_proactive(self, speaker, make_prompt, reply_turns):
        self.prompts.append((speaker.id, make_prompt([])))
        future = asyncio.get_event_loop().create_future()
        future.set_result(self.reply)
        return future


async def test_runtime_asks_when_due(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    asker = type("P", (), {"id": "grok", "name": "Grok", "cfg": type("C", (), {"kind": "grok_cli"})()})()
    rt.participants = [asker]
    rt.refresh_prompts = lambda: None
    room = FakeRoom()
    rt.room = lambda chat: room
    rt.followups.now = lambda: datetime(2026, 10, 26, 16, 0)
    item = rt.followups.add("阿明面试，问问结果", datetime(2026, 10, 26, 15, 0), "-1", "grok")
    await rt._followup_tick()
    assert room.prompts[0][0] == "grok" and "阿明面试" in room.prompts[0][1]
    assert item.status == "done" and item.asked_at

    busy = FakeRoom(busy=True)
    rt.room = lambda chat: busy
    later = rt.followups.add("出差回来没", datetime(2026, 10, 26, 15, 0), "-1", "nobody")
    await rt._followup_tick()
    assert later.status == "pending" and busy.prompts == []  # waits a bit

    passed = FakeRoom(reply=None)
    rt.room = lambda chat: passed
    later.due = 0
    await rt._followup_tick()
    assert later.status == "skipped"  # it had nothing to ask after all
