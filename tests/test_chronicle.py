"""The group's own history: daily highlights and memes made in the group."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import pytest

from nookling.chronicle import Chronicle, build_prompt, parse_reply
from nookling.config import ConfigError
from nookling.runtime import Runtime
from nookling.settings import SettingsStore
from nookling.transcript import Message

REPLY = json.dumps({"moments": [{"title": "洗衣液咖啡", "what": "主人把洗衣液倒进咖啡机，还说喝起来还行", "people": ["主人", "Grok"]}],
                    "memes": ["「战略性失误」是 Grok 翻车时的说法"]}, ensure_ascii=False)


def test_parse_and_prompt():
    moments, memes = parse_reply("好的```json\n" + REPLY + "\n```")
    assert moments[0]["title"] == "洗衣液咖啡" and memes == ["「战略性失误」是 Grok 翻车时的说法"]
    with pytest.raises(ConfigError):
        parse_reply("没什么好记的")
    prompt = build_prompt("2026-10-25", ["10:00 主人: 早"], ["旧事"], ["旧梗"])
    assert "2026-10-25" in prompt and "旧事" in prompt and "旧梗" in prompt and "10:00 主人: 早" in prompt


def test_which_day_and_recording(tmp_path):
    c = Chronicle(tmp_path)
    days = ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert c.day_to_write(days, datetime(2026, 10, 26, 12, 0)) == "2026-10-24"
    added = c.record("2026-10-24", json.loads(REPLY)["moments"])
    assert [m.title for m in added] == ["洗衣液咖啡"]
    c.record("2026-10-25", [])  # a day with nothing worth it is done too
    assert c.day_to_write(days, datetime(2026, 10, 26, 12, 0)) is None  # today is not over yet
    assert c.day_to_write(days, datetime(2026, 10, 26, 23, 10)) == "2026-10-26"
    assert c.record("2026-10-26", json.loads(REPLY)["moments"]) == []  # same moment, not twice
    assert "10月24日 洗衣液咖啡" in Chronicle(tmp_path).prompt_section()
    c.delete(added[0].id)
    assert c.prompt_section() == ""


async def test_runtime_writes_the_day_up(tmp_path, monkeypatch):
    rt = Runtime(SettingsStore(tmp_path))
    writer = type("P", (), {"id": "ds", "name": "DeepSeek", "cfg": type("C", (), {"kind": "api"})()})()
    rt.participants = [writer]
    seen = {}

    class Actor:
        def __init__(self, cfg, sessions, work, timeout):
            seen["work"] = work

        def set_system_prompt(self, prompt):
            seen["system"] = prompt

        async def reply(self, key, prompt):
            seen["prompt"] = prompt
            return REPLY

        def reset(self, key):
            seen["reset"] = key

    monkeypatch.setattr("nookling.runtime.participant_class", lambda cfg: Actor)
    room = rt.conversation("-1")
    ts = datetime(2026, 10, 25, 21, 0).timestamp()
    room.messages = [Message("user", "主人", f"消息{i}", (), ts + i) for i in range(8)]
    rt.proactive_chat = lambda: "-1"
    rt._chronicle_tick(datetime(2026, 10, 26, 9, 0))
    for _ in range(50):
        if not rt._chronicle_running:
            break
        await asyncio.sleep(0.01)
    assert [m.title for m in rt.chronicle.moments] == ["洗衣液咖啡"]
    assert rt.memes.items[-1].added_by == "群里"
    assert "洗衣液咖啡" in rt.chronicle.prompt_section()
    assert "消息7" in seen["prompt"] and seen["work"].name == "ds" and "记事本" in seen["system"]
    rt._chronicle_tick(datetime(2026, 10, 26, 9, 5))
    assert not rt._chronicle_running  # that day is done


async def test_a_failed_write_up_is_not_retried_every_tick(tmp_path, monkeypatch):
    rt = Runtime(SettingsStore(tmp_path))
    rt.participants = [type("P", (), {"id": "ds", "name": "DeepSeek", "cfg": type("C", (), {"kind": "api"})()})()]

    class Broken:
        def __init__(self, *a):
            pass

        def set_system_prompt(self, prompt):
            pass

        async def reply(self, key, prompt):
            return "没法整理"

        def reset(self, key):
            pass

    monkeypatch.setattr("nookling.runtime.participant_class", lambda cfg: Broken)
    room = rt.conversation("-1")
    room.messages = [Message("user", "主人", f"m{i}", (), time.time() - 86400 + i) for i in range(8)]
    rt._chronicle_running = True
    assert await rt._write_chronicle(room, datetime.fromtimestamp(time.time() - 86400).date().isoformat()) == []
    assert rt.chronicle.done_days and not rt._chronicle_running
