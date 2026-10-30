"""Emoji reactions and quote replies."""

from __future__ import annotations

import random

from conftest import FakeSink

from nookling.moderator import RuleModerator
from nookling.reactions import extract_reaction, normalize, reaction_instructions
from nookling.conversation import Conversation


class InOrder(RuleModerator):
    def pick(self, candidates, last_message, last_speaker_id):
        mentioned = [c for c in candidates if c.id in last_message.mentions]
        return (mentioned or candidates)[0] if candidates else None


def room_for(members, store, round_cfg, reactions=True):
    sink = FakeSink()
    return Conversation("chat", members, sink, round_cfg, store, InOrder(random.Random(0)), reactions=reactions), sink


def test_normalize_and_extract():
    assert normalize("😂") == "🤣" and normalize("❤️") == "❤" and normalize("👍") == "👍"
    assert normalize("🦖") is None
    assert extract_reaction("[回应:😂]") == ("", "🤣")
    assert extract_reaction("哈哈 [回应：👍] [回应:🔥]") == ("哈哈", "👍")  # one per message
    assert extract_reaction("[回应:🦖] 好") == ("好", None)
    assert "[回应:🤣]" in reaction_instructions()


async def test_reaction_only_reply_reacts_and_posts_nothing(make, store, round_cfg):
    a = make("a", ["[回应:😂]"])
    room, sink = room_for([a], store, round_cfg)
    room.add_user_message("主人", "我把洗衣液倒进咖啡机了", tg_id=500)
    await room.wait_idle()
    assert sink.reactions == [("a", 500, "🤣")] and sink.posts == []
    assert "点了 🤣" in room.messages[-1].text and "洗衣液" in room.messages[-1].text  # others can see it


async def test_reaction_with_text(make, store, round_cfg):
    a = make("a", ["笑死 [回应:👍]"])
    room, sink = room_for([a], store, round_cfg)
    room.add_user_message("主人", "hi", tg_id=7)
    await room.wait_idle()
    assert sink.posts == [("a", "笑死")] and sink.reactions == [("a", 7, "👍")]


async def test_no_reaction_without_a_message_id_or_when_off(make, store, round_cfg):
    a = make("a", ["[回应:😂]"])
    room, sink = room_for([a], store, round_cfg)
    room.add_user_message("主人", "hi")  # console: no Telegram id
    await room.wait_idle()
    assert sink.reactions == [] and sink.posts == []

    b = make("b", ["[回应:😂]", "好"])
    room, sink = room_for([b], store, round_cfg, reactions=False)
    room.add_user_message("主人", "hi", tg_id=9)
    await room.wait_idle()
    assert sink.reactions == []  # counted as a pass


async def test_quote_only_when_the_chat_moved_on(make, store, round_cfg):
    from dataclasses import replace

    slow = make("slow", ["查到了，下周发售"], delay=0.2)
    fast = make("fast", ["我先说两句"])
    cfg = replace(round_cfg, slow_after=0.05, max_parallel=2, max_turns_per_model=1)
    room, sink = room_for([slow, fast], store, cfg)
    room.add_user_message("主人", "那个游戏啥时候出", tg_id=500)
    await room.wait_idle()
    # fast answered the latest message: no quote. slow answered after fast spoke: it quotes the question.
    assert sink.quotes == [("fast", None), ("slow", 500)]


async def test_reaction_goes_to_the_member_it_mentions(make, store, round_cfg):
    a = make("a", ["今天降温了"])
    b = make("b", ["@A 你说得轻巧 [回应:🤔]"])
    room, sink = room_for([a, b], store, round_cfg)
    room.add_user_message("主人", "@A 天气怎么样", tg_id=500)
    await room.wait_idle()
    a_id = 1001  # FakeSink's first id
    assert ("b", a_id, "🤔") in sink.reactions
    assert sink.quotes[-1] == ("b", None)  # it answered the latest message: no quote
