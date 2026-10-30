"""Nightly consolidation: what the user said about himself becomes long-term memory, with proof."""

from __future__ import annotations

import json
from datetime import datetime

from nookling.memory import CONSOLIDATED, MemoryStore, consolidation_prompt, parse_consolidation
from nookling.runtime import Runtime
from nookling.settings import SettingsStore
from nookling.transcript import USER, Message

SAID = ["我想辞职去开咖啡店了，感觉上班好无聊", "1w5 左右", "嘿嘿，嘴上辞了，明年开春再提离职吧"]


def answer(*items):
    return "好的\n" + json.dumps(list(items), ensure_ascii=False)


def test_only_facts_backed_by_the_users_own_words_are_kept(tmp_path):
    store = MemoryStore(tmp_path)
    old = store.add("主人想马上辞职")
    raw = answer(
        {"about": "主人", "text": "主人月薪一万五左右", "quote": "1w5 左右", "replaces": ""},
        {"about": "主人", "text": "主人打算明年开春提离职", "quote": "明年开春再提离职", "replaces": old.id},
        {"about": "主人", "text": "主人养了一只猫", "quote": "我家猫今天", "replaces": ""},  # never said
        {"about": "主人", "text": "主人喜欢肉鸽", "quote": "无聊", "replaces": "nope"},  # quote too short
        {"about": "主人", "text": "主人想开咖啡店", "quote": "去开咖啡店", "replaces": "no-such-id"},
    )
    found = parse_consolidation(raw, SAID, store.items)
    assert [f["text"] for f in found] == ["主人月薪一万五左右", "主人打算明年开春提离职", "主人想开咖啡店"]
    assert found[1]["replaces"] == old.id and found[2]["replaces"] == ""
    assert parse_consolidation("没有 JSON", SAID, store.items) == []


def test_review_waits_and_approval_retires_the_old_fact(tmp_path):
    store = MemoryStore(tmp_path)
    old = store.add("主人想马上辞职")
    said_at = datetime(2026, 10, 27, 8, 46).timestamp()
    found = [{"about": "主人", "text": "主人打算明年开春提离职", "quote": "明年开春再提离职", "replaces": old.id}]
    [new] = store.record_consolidation("2026-10-27", found, "review", said_at)
    assert new.status == "pending" and new.source == CONSOLIDATED and store.get(old.id).enabled
    store.update(new.id, {"status": "active"})
    assert not store.get(old.id).enabled  # the update replaced it
    section = store.prompt_section("claude", "review")
    assert "主人打算明年开春提离职（10-27 说的）" in section and "主人想马上辞职" not in section
    again = MemoryStore(tmp_path)
    assert again.done_days == ["2026-10-27"] and again.get(new.id).quote == "明年开春再提离职"


def test_auto_keeps_right_away_and_off_keeps_nothing(tmp_path):
    store = MemoryStore(tmp_path)
    found = [{"about": "主人", "text": "主人月薪一万五左右", "quote": "1w5 左右", "replaces": ""}]
    assert store.record_consolidation("2026-10-26", [], "off", 0.0) == []
    [fact] = store.record_consolidation("2026-10-27", found, "auto", 1.0)
    assert fact.status == "active"
    assert store.record_consolidation("2026-10-28", found, "auto", 2.0) == []  # already known
    assert store.done_days == ["2026-10-26", "2026-10-27", "2026-10-28"]


def test_which_day_to_read(tmp_path):
    store = MemoryStore(tmp_path)
    noon, night = datetime(2026, 10, 29, 12, 0), datetime(2026, 10, 29, 23, 10)
    days = ["2026-10-20", "2026-10-27", "2026-10-29"]
    assert store.day_to_consolidate(days, noon) == "2026-10-27"  # 10-20 is too long ago
    store.done_days = ["2026-10-27"]
    assert store.day_to_consolidate(days, noon) is None  # today isn't over yet
    assert store.day_to_consolidate(days, night) == "2026-10-29"


def test_the_prompt_shows_known_facts_and_who_the_user_is(tmp_path):
    store = MemoryStore(tmp_path)
    old = store.add("主人在杭州工作")
    prompt = consolidation_prompt("2026-10-27", ["08:46 Orion（用户）: 1w5 左右"], store.items, ["Orion"])
    assert "群主 Orion" in prompt and old.id in prompt and "1w5 左右" in prompt and "replaces" in prompt


async def test_runtime_reads_a_finished_day(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.refresh_prompts = lambda: None
    rt.proactive_chat = lambda: "-100"
    room = rt.conversation("-100")
    day = datetime(2026, 10, 27, 8, 0)
    for i, text in enumerate(SAID):
        room.messages.append(Message(USER, "Orion", text, (), day.timestamp() + i * 60))
    room.messages.append(Message("codex", "Codex", "盲猜月薪一万", (), day.timestamp() + 30))
    asked = []

    async def cheap(prompt, system, purpose="game_memory"):
        asked.append((prompt, purpose))
        return answer({"about": "主人", "text": "主人月薪一万五左右", "quote": "1w5 左右", "replaces": ""},
                      {"about": "主人", "text": "主人月薪一万", "quote": "盲猜月薪一万", "replaces": ""})  # Codex said that

    rt._cheap_ask = cheap
    rt._memory_running = True
    added = await rt._consolidate_memory(room, "2026-10-27")
    assert [f.text for f in added] == ["主人月薪一万五左右"] and added[0].status == "pending"
    assert asked[0][1] == "memory" and "Orion（用户）" in asked[0][0] and not rt._memory_running

    async def nobody(prompt, system, purpose="game_memory"):
        return None  # only Claude around: it doesn't do this

    rt._cheap_ask = nobody
    rt.memory.done_days = []
    assert await rt._consolidate_memory(room, "2026-10-27") == []
    assert rt.memory.done_days == []  # tried again later


async def test_nothing_to_read_without_a_group(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    try:
        await rt.consolidate_memory_now()
    except Exception as e:  # noqa: BLE001
        assert "群" in str(e)
    else:
        raise AssertionError("consolidated without a group")


def test_a_newer_fact_replaces_one_still_awaiting_approval(tmp_path):
    store = MemoryStore(tmp_path)
    kept = store.add("主人不想上班")
    [first] = store.record_consolidation("2026-10-26", [{"about": "主人", "text": "主人想马上辞职", "quote": "想辞职",
                                                         "replaces": kept.id}], "review", 1.0)
    prompt = consolidation_prompt("2026-10-27", [], store.items, ["Orion"])
    assert first.id in prompt and "待确认" in prompt
    found = parse_consolidation(answer({"about": "主人", "text": "主人打算明年开春提离职", "quote": "明年开春再提离职",
                                        "replaces": first.id}), SAID, store.items)
    assert found[0]["replaces"] == first.id
    [second] = store.record_consolidation("2026-10-27", found, "review", 2.0)
    assert [f.id for f in store.items if f.status == "pending"] == [second.id]  # the unapproved one is gone
    assert second.replaces == kept.id  # and what it was going to update carries over
    store.update(second.id, {"status": "active"})
    assert not store.get(kept.id).enabled


def test_past_the_budget_only_core_own_and_recent_facts_stay_in_the_prompt(tmp_path, monkeypatch):
    import nookling.memory as memory_module

    monkeypatch.setattr(memory_module, "PROMPT_BUDGET", 60)
    store = MemoryStore(tmp_path)
    own = store.add("主人不吃香菜")  # written by the owner
    salary = store.add("主人的月薪是一万五左右，在一家书店当店长", source=CONSOLIDATED)
    salary.said_at = 1.0
    store.add("主人断断续续学了两年咖啡，拉花已经能看", source=CONSOLIDATED).said_at = 3.0
    space = store.add("主人很向往去外太空玩，觉得比不上班还开心", source=CONSOLIDATED)
    space.said_at = 2.0
    shown = [f.text for f in store.in_prompt("claude")]
    assert shown == ["主人不吃香菜", "主人断断续续学了两年咖啡，拉花已经能看", "主人很向往去外太空玩，觉得比不上班还开心"]
    assert "月薪" not in store.prompt_section("claude", "off")
    hit = store.recall("claude", "你的月薪是一万五左右吗")
    assert "主人的月薪是一万五左右" in hit and hit.startswith("[想起来的长期记忆]")
    assert store.recall("claude", "主人不吃香菜吧") == ""  # already in the prompt
    store.update(salary.id, {"core": True})
    assert any("月薪" in f.text for f in store.in_prompt("claude"))  # core goes in whatever the size
    assert own in store.in_prompt("claude")
