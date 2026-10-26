"""The user's 👍 / 👎 on members' messages."""

from __future__ import annotations

from conftest import FakeSink

from roundtable.feedback import FeedbackStore, plain_text
from roundtable.room import ChatRoom
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore
from roundtable.transcript import Message


def test_store(tmp_path):
    fb = FeedbackStore(tmp_path)
    assert fb.react("grok", "就这？", "🤔") is None  # says nothing either way
    liked = fb.react("grok", "就这？", "😂")
    assert liked.kind == "liked" and fb.prompt_section("grok") == ""
    assert fb.react("grok", "就这？", "👎").id == liked.id and liked.kind == "disliked"  # changed its mind
    assert "「就这？」" in fb.prompt_section("grok") and fb.prompt_section("ds") == ""
    assert fb.unreact("grok", "就这？") and fb.items == []
    kept = fb.react("ds", "我不是肥鱼！", "❤")
    fb.mark(kept.id, "used")
    assert not fb.unreact("ds", "我不是肥鱼！")  # already a sample line
    assert FeedbackStore(tmp_path).items[0].status == "used"
    assert plain_text("哈哈 [表情:狗头] https://x.com/a [给 A 的「x」点了 👍]") == "哈哈"


async def test_telegram_ids_survive_a_restart(make, store, round_cfg, tmp_path):
    a = make("a", ["好耶"])
    log = tmp_path / "chat.jsonl"
    room = ChatRoom("-1", [a], FakeSink(), round_cfg, store, log_path=log)
    room.add_user_message("阿明", "来", tg_id=7)
    await room.wait_idle()
    again = ChatRoom("-1", [a], FakeSink(), round_cfg, store, log_path=log)
    assert again.message_for(1001).text == "好耶" and again.message_for(7).from_user


async def test_reactions_reach_the_member(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    member = {"id": "grok", "name": "Grok", "kind": "api", "provider": "deepseek", "model": "m", "examples": "就这？"}
    await rt.apply({**rt.settings, "participants": [member]})
    room = rt.room("-1")
    said = Message("grok", "Grok", "笑死，这也行 [表情:狗头]", (), 5.0)
    room._remember_tg(said, 55)
    room._append(said)
    rt.sink.parts[("-1", 54)] = 55  # the first bubble of the same reply

    rt.on_reaction("-1", 54, "阿明", ["👎"], [])
    assert rt.feedback.items[0].text == "笑死，这也行"
    assert room.messages[-1].from_user and "点了 👎" in room.messages[-1].text
    assert "「笑死，这也行」" in rt.system_prompt_for(rt.config.participants[0])

    rt.on_reaction("-1", 55, "阿明", ["😂"], ["👎"])
    item = rt.feedback.items[0]
    assert item.kind == "liked" and "笑死" not in rt.system_prompt_for(rt.config.participants[0])
    await rt.use_feedback(item.id)
    assert rt.settings["participants"][0]["examples"] == "就这？\n笑死，这也行"
    assert item.status == "used"

    rt.on_reaction("-1", 999, "阿明", ["👍"], [])  # unknown message: ignored
    assert len(rt.feedback.items) == 1
