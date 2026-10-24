from __future__ import annotations

import pytest
from conftest import FakeSink

from roundtable.config import ConfigError
from roundtable.memory import LEARN_INSTRUCTIONS, MemoryStore, extract_memories
from roundtable.room import ChatRoom


@pytest.fixture
def mem(tmp_path):
    return MemoryStore(tmp_path)


def test_extract_memories():
    text, facts = extract_memories("好的，记下了 [记住:阿明不吃香菜]")
    assert text == "好的，记下了"
    assert facts == ["阿明不吃香菜"]
    assert extract_memories("[remember: likes cats] ok")[1] == ["likes cats"]
    assert extract_memories("没有标记") == ("没有标记", [])


def test_add_dedupes_and_persists(tmp_path, mem):
    assert mem.add("阿明在杭州工作") is not None
    assert mem.add("  阿明在杭州工作 ") is None
    assert [f.text for f in MemoryStore(tmp_path).items] == ["阿明在杭州工作"]
    with pytest.raises(ConfigError):
        mem.add("   ")


def test_prompt_section_respects_scope_status_and_enabled(mem):
    mem.add("全员都知道")
    mem.add("只有 claude 知道", "claude")
    off = mem.add("停用的")
    mem.update(off.id, {"enabled": False})
    mem.suggest(["待确认的"], "codex", "review")

    claude = mem.prompt_section("claude", "off")
    codex = mem.prompt_section("codex", "off")
    assert "全员都知道" in claude and "只有 claude 知道" in claude
    assert "只有 claude 知道" not in codex
    assert "停用的" not in claude and "待确认的" not in claude
    assert LEARN_INSTRUCTIONS not in claude
    assert LEARN_INSTRUCTIONS in mem.prompt_section("codex", "review")
    assert MemoryStore(mem.path.parent / "x").prompt_section("a", "off") == ""


def test_suggest_modes(mem):
    assert mem.suggest(["a"], "ds", "off") == []
    pending = mem.suggest(["b"], "ds", "review")
    assert pending[0].status == "pending" and pending[0].source == "ds"
    assert mem.suggest(["c"], "ds", "auto")[0].status == "active"
    assert mem.pending_count() == 1


def test_drop_scope_makes_orphans_shared(mem):
    f = mem.add("x", "gone")
    mem.drop_scope({"claude"})
    assert mem.get(f.id).scope == "all"


async def test_room_hands_memories_to_hook_and_hides_tags(make, round_cfg, store):
    a = make("a", ["好的 [记住:阿明喜欢猫]"])
    sink = FakeSink()
    seen = []
    room = ChatRoom("chat", [a], sink, round_cfg, store, on_memory=lambda p, facts: seen.append((p.id, facts)))

    room.add_user_message("me", "我喜欢猫")
    await room.wait_idle()

    assert sink.posts == [("a", "好的")]
    assert seen == [("a", ["阿明喜欢猫"])]
    assert room.messages[-1].text == "好的"


async def test_transcript_survives_restart(make, round_cfg, store, tmp_path):
    log = tmp_path / "chats" / "chat.jsonl"
    a = make("a", ["hello"])
    room = ChatRoom("chat", [a], FakeSink(), round_cfg, store, log_path=log)
    room.add_user_message("me", "hi")
    await room.wait_idle()

    b = make("b", ["[PASS]"])
    again = ChatRoom("chat", [a, b], FakeSink(), round_cfg, store, log_path=log)
    assert [(m.speaker_id, m.text) for m in again.messages] == [("user", "hi"), ("a", "hello")]
    assert again.messages[0].ts > 0
    # Old messages count as seen, so nobody replays them after a restart.
    assert all(c == 2 for c in again.cursors.values())

    await again.reset()
    assert not log.exists()


async def test_reset_member_replays_recent_transcript(make, round_cfg, store, tmp_path):
    a = make("a", ["one", "two"])
    room = ChatRoom("chat", [a], FakeSink(), round_cfg, store)
    room.add_user_message("me", "first")
    await room.wait_idle()
    store.set("chat", "a", "session-1")

    room.reset_member(a)
    assert store.get("chat", "a") is None
    room.add_user_message("me", "second")
    await room.wait_idle()
    assert "me（用户）: first" in a.prompts[-1] and "me（用户）: second" in a.prompts[-1]
