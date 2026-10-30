"""People the owner brings into the group: the members can tell them apart from him."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace

from conftest import FakeSink

from roundtable.config import RoundConfig
from roundtable.life import LifeStore
from roundtable.prompts import build_turn_prompt
from roundtable.room import ChatRoom
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore
from roundtable.transcript import GUEST_NOTE, USER, Message, label


def test_a_guest_is_labelled_and_kept_across_restarts():
    owner = Message(USER, "Orion", "我掏钱养你们", (), 1.0)
    guest = Message(USER, "小林", "大阪", (), 2.0, guest=True)
    assert owner.from_owner and guest.from_user and not guest.from_owner
    assert label(owner) == "Orion（用户）" and label(guest) == "小林（群友）"
    assert Message.from_json(json.loads(json.dumps(guest.to_json()))).guest
    assert "guest" not in owner.to_json()
    prompt = build_turn_prompt([owner, guest], 2)
    assert "小林（群友）: 大阪" in prompt and GUEST_NOTE in prompt
    assert GUEST_NOTE not in build_turn_prompt([owner], 2)


async def test_the_members_see_who_is_a_guest(make, store):
    codex = make("codex", ["你在哪个城市？"])
    room = ChatRoom("chat", [codex], FakeSink(), RoundConfig(call_timeout=5), store)
    room.add_user_message("小林", "@Codex 介绍一下今天天气", guest=True)
    await room.wait_idle()
    assert "小林（群友）" in codex.prompts[0] and "不是阿明本人" in codex.prompts[0]
    assert room.messages[0].guest


def test_the_first_allowed_id_is_the_owner(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.config.telegram.allowed_user_ids = [111, 222]
    assert not rt.is_guest(111) and rt.is_guest(222)
    rt.config.telegram.allowed_user_ids = []
    assert not rt.is_guest(222)


async def test_what_a_guest_says_is_not_remembered_about_the_owner(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.refresh_prompts = lambda: None
    room = rt.room("-100")
    day = datetime(2026, 10, 27, 8, 0).timestamp()
    for i, text in enumerate(["今天又加班到九点", "下个月想去成都玩", "成都那边吃得好"]):
        room.messages.append(Message(USER, "Orion", text, (), day + i * 60))
    room.messages.append(Message(USER, "小林", "我住在大阪", (), day + 600, guest=True))
    asked = []

    async def cheap(prompt, system, purpose="game_memory"):
        asked.append(prompt)
        return json.dumps([{"about": "阿明", "text": "阿明住在大阪", "quote": "我住在大阪", "replaces": ""}],
                          ensure_ascii=False)

    rt._cheap_ask = cheap
    rt._memory_running = True
    assert await rt._consolidate_memory(room, "2026-10-27") == []  # a guest's words are not his
    assert "小林（群友）: 我住在大阪" in asked[0] and "（群友）的是群主拉进群的朋友" in asked[0]


def test_a_guest_reaction_teaches_nothing(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    room = rt.room("-100")
    target = Message("codex", "Codex", "嗯，这句爱听", (), time.time())
    room._append(target)
    room.message_for = lambda tg_id: target
    rt.sink = SimpleNamespace(parts={})
    rt.on_reaction("-100", 1, "小林", ["👍"], [], guest=True)
    assert rt.feedback.items == [] and room.messages[-1].guest  # seen in the group, not learned


def test_everyone_s_game_progress_side_by_side(tmp_path):
    store = LifeStore(tmp_path)
    now = time.time()
    store.artifacts = [
        {"kind": "game", "owner": "deepseek", "created_at": now, "game_started_at": now - 60,
         "title": "《杀戮尖塔》游玩回执", "game_facts": "最后记录在第 12 层，生命 77/87。"},
        {"kind": "game", "owner": "grok", "created_at": now, "game_started_at": now - 120,
         "title": "《杀戮尖塔》游玩回执", "game_facts": "最后记录在第 1 层，生命 80/80。"},
    ]
    board = store.game_board("deepseek", "12层了？我才刚开局", {"deepseek": "DeepSeek", "grok": "Grok"})
    first, second = board.splitlines()[1:3]
    assert first.startswith("- 你自己：《杀戮尖塔》，第 12 层") and second.startswith("- Grok：《杀戮尖塔》，第 1 层")
    assert store.game_board("deepseek", "今天天气怎么样", {}) == ""
