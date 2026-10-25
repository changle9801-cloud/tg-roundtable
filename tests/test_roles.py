"""Role cards, the no-coding policy and activity levels."""

from __future__ import annotations

import random
from collections import Counter

import pytest
from conftest import FakeSink

from roundtable.config import ConfigError, ParticipantConfig
from roundtable.moderator import RuleModerator
from roundtable.prompts import NO_CODING_POLICY, build_system_prompt, looks_like_code, render_role
from roundtable.room import ChatRoom
from roundtable.settings import normalize_participant
from roundtable.transcript import Message


def role(**kw):
    return ParticipantConfig(id="a", name="阿杰", kind="claude_cli", **kw)


def test_role_card_comes_first_then_rules_scene_policy():
    cfg = role(tagline="吐槽担当", persona="嘴毒心软", style="短句", examples="就这？\n好家伙", activity="high")
    prompt = build_system_prompt("阿杰", ["小雅"], render_role(cfg), scene="你们是邻居", no_coding=True, extra="表情包：…")
    order = [prompt.index(s) for s in ("你在这个群里的角色：阿杰", "群聊规则", "群的背景设定", "话题范围", "表情包")]
    assert order == sorted(order)
    assert "- 就这？" in prompt and "话痨" in prompt
    assert "你知道自己是 AI" in prompt


def test_policy_and_scene_are_optional():
    prompt = build_system_prompt("阿杰", [], render_role(role()))
    assert NO_CODING_POLICY not in prompt and "背景设定" not in prompt


@pytest.mark.parametrize("text", [
    "```python\nprint(1)\n```",
    "def add(a, b):\n    return a + b\n\nprint(add(1, 2))",
    "<div class='x'>\n  <span>hi</span>\n</div>",
    "for (let i = 0; i < 3; i++) {\n  console.log(i);\n}",
])
def test_code_is_detected(text):
    assert looks_like_code(text)


@pytest.mark.parametrize("text", [
    "我今天写了一天代码，累死了",
    "for 循环到底是啥意思啊",
    "周末去吃火锅吗？\n我想吃毛肚\n还有鸭肠",
    "好家伙 => 这也太离谱了",
])
def test_everyday_talk_is_not_code(text):
    assert not looks_like_code(text)


async def test_room_drops_replies_with_code(make, round_cfg, store):
    a = make("a", ["好的，代码如下：\n```\nprint(1)\n```"])
    sink = FakeSink()
    room = ChatRoom("chat", [a], sink, round_cfg, store, block_code=True)

    room.add_user_message("me", "帮我写个脚本")
    await room.wait_idle()

    assert sink.posts == []
    assert all(m.speaker_id == "user" for m in room.messages)


async def test_room_allows_code_when_policy_is_off(make, round_cfg, store):
    a = make("a", ["```\nprint(1)\n```"])
    sink = FakeSink()
    room = ChatRoom("chat", [a], sink, round_cfg, store, block_code=False)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert len(sink.posts) == 1


def test_chatty_members_are_picked_more_often(make):
    chatty = make("chatty", [])
    chatty.cfg.activity = "high"
    quiet = make("quiet", [])
    quiet.cfg.activity = "low"
    moderator = RuleModerator(random.Random(1))
    last = Message("user", "me", "hi")
    picks = Counter(moderator.pick([chatty, quiet], last, None).id for _ in range(2000))
    assert picks["chatty"] > 3 * picks["quiet"]


async def test_member_turn_limit_overrides_group_default(make, round_cfg, store):
    a = make("a", [f"a{i}" for i in range(10)])
    a.cfg.max_turns = 1
    b = make("b", [f"b{i}" for i in range(10)])
    c = make("c", [f"c{i}" for i in range(10)])
    sink = FakeSink()
    room = ChatRoom("chat", [a, b, c], sink, round_cfg, store)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    counts = Counter(pid for pid, _ in sink.posts)
    assert counts["a"] == 1
    # The others keep the group default (3). One of them may stop at 2 when it spoke last
    # and nobody new is left to answer.
    assert max(counts["b"], counts["c"]) == 3 and min(counts["b"], counts["c"]) >= 2


def test_role_fields_are_validated():
    base = {"id": "a", "name": "阿杰", "kind": "claude_cli"}
    assert normalize_participant({**base, "activity": "weird"})["activity"] == "normal"
    with pytest.raises(ConfigError, match="120"):
        normalize_participant({**base, "tagline": "长" * 121})
    with pytest.raises(ConfigError, match="0 到 10"):
        normalize_participant({**base, "max_turns": 11})
