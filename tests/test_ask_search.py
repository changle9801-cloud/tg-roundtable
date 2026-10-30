"""Members who cannot search ask a friend who can; nobody fakes knowing something new."""

from __future__ import annotations

import random

from conftest import FakeSink

from roundtable.config import RoundConfig
from roundtable.moderator import RuleModerator
from roundtable.prompts import FRIEND_RULES, ask_search_instructions, search_instructions
from roundtable.room import ChatRoom
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore, build_config, normalize_settings


def test_prompt_for_a_member_without_search():
    text = ask_search_instructions([("Codex", False), ("Grok", True)])
    assert "你不能联网搜索" in text and "@Codex、@Grok" in text
    assert "X（推特）上的事找 @Grok" in text and "道谢不用再 @对方" in text
    alone = ask_search_instructions([])
    assert "直说没听过" in alone and "@" not in alone


def test_searchers_help_friends_and_look_up_new_things():
    text = search_instructions(covers_x=False)
    assert "朋友请你帮忙查" in text and "@对方" in text
    assert "不用去查" in text and "闲聊里不贴链接" in text  # "靠谱吗" is chat, not a lookup


def test_rules_say_do_not_fake_knowing():
    assert "别把没根据的细节说成事实" in FRIEND_RULES  # guessing is fine, passing it off as fact is not
    assert "我看悬" in FRIEND_RULES and "别把猜的讲成事实" in FRIEND_RULES


def test_each_member_gets_the_right_section(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.config = build_config(normalize_settings({"participants": [
        {"id": "codex", "name": "Codex", "kind": "codex_cli", "web_search": True},
        {"id": "grok", "name": "Grok", "kind": "grok_cli", "web_search": True},
        {"id": "claude", "name": "Claude", "kind": "claude_cli", "web_search": False},
        {"id": "deepseek", "name": "DeepSeek", "kind": "api", "provider": "deepseek"},
    ]}), {}, tmp_path)
    pcs = {pc.id: pc for pc in rt.config.participants}
    deepseek = rt.system_prompt_for(pcs["deepseek"])
    assert "@Codex、@Grok" in deepseek and "@Claude" not in deepseek  # Claude has search turned off
    assert "联网搜索：" in rt.system_prompt_for(pcs["codex"])
    assert "@Codex、@Grok" in rt.system_prompt_for(pcs["claude"])


async def test_ask_and_answer_go_back_and_forth(make, store):
    # Even when nobody would chime in on their own, the @ chain carries the exchange.
    ds = make("deepseek", ["诶？没听过，我上不了网 @Codex 帮我查一下？", "原来是这样，好想玩"])
    codex = make("codex", ["@Deepseek 查到了：上周发售，开放世界。"])
    other = make("grok", ["g"])
    cfg = RoundConfig(max_turns_per_model=2, call_timeout=5, follow_up_chance=0.0, follow_up_decay=0.0)
    sink = FakeSink()
    room = ChatRoom("chat", [ds, codex, other], sink, cfg, store, RuleModerator(random.Random(0)))
    room.add_user_message("me", "@Deepseek 最近新出的那个游戏你玩了吗")
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["deepseek", "codex", "deepseek"]


def test_everyone_knows_what_friends_can_do(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.config = build_config(normalize_settings({"participants": [
        {"id": "codex", "name": "Codex", "kind": "codex_cli", "web_search": True, "can_draw": True},
        {"id": "grok", "name": "Grok", "kind": "grok_cli", "web_search": True},
        {"id": "deepseek", "name": "DeepSeek", "kind": "api", "provider": "deepseek"},
    ]}), {}, tmp_path)
    pcs = {pc.id: pc for pc in rt.config.participants}
    ds = rt.system_prompt_for(pcs["deepseek"])
    assert "- Codex：联网搜索、画表情包、看图" in ds
    assert "- Grok：联网搜索（也能搜 X/推特上的帖子）" in ds
    assert "- 你自己：只聊天" in ds and "@会的朋友帮个忙" in ds
    codex = rt.system_prompt_for(pcs["codex"])
    assert "- DeepSeek：只聊天" in codex and "- 你自己：联网搜索、画表情包、看图" in codex
    rt.config.vision_member = "off"
    assert "看图" not in rt.system_prompt_for(pcs["deepseek"]).split("朋友们各自会什么")[1].split("自己做不到")[0]
