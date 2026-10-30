"""Members get reminded of their own recent verbal tics."""

from __future__ import annotations

from conftest import FakeSink

from nookling.prompts import build_turn_prompt
from nookling.repetition import reminder, repeated_bits
from nookling.conversation import Conversation
from nookling.transcript import Message


def test_repeated_opener():
    texts = ["喂喂，这玩意能吃吗", "今天好冷", "喂喂，凌晨两点吃榴莲？"]
    assert repeated_bits(texts) == ["喂喂"]


def test_repeated_phrase_keeps_the_longest_form():
    texts = ["这个我早知道了，宇宙级常识 😏", "哈，宇宙级常识而已", "随便吧"]
    assert repeated_bits(texts) == ["宇宙级常识"]


def test_topic_words_from_the_chat_are_not_tics():
    own = ["啧，凌晨两点吃一桶泡面？", "啧，凌晨两点还加一桶泡面"]
    others = ["我准备凌晨两点吃榴莲", "再配一桶泡面"]
    assert repeated_bits(own, others) == ["啧"]  # the opener still counts
    assert "凌晨两点" in "".join(repeated_bits(own))


def test_short_and_common_words_need_more():
    assert repeated_bits(["我觉得行", "我觉得不行"]) == []  # common phrase
    assert repeated_bits(["大肥鱼你好", "大肥鱼别睡"]) == []  # three characters, only twice
    assert repeated_bits(["大肥鱼你好", "大肥鱼别睡", "又是大肥鱼"]) == ["大肥鱼"]


def test_tags_links_and_old_messages_are_ignored():
    assert repeated_bits(["哈 [表情:狗头]", "嗯 [表情:狗头]"]) == []
    assert repeated_bits(["看 https://example.com/宇宙级常识", "这个 https://example.com/宇宙级常识"]) == []
    old = ["宇宙级常识"] * 2 + ["好", "行", "嗯", "冷", "饿", "困", "累", "烦"]
    assert repeated_bits(old) == []  # only the last few messages count


def test_reminder_text():
    assert reminder([]) == ""
    assert reminder(["喂喂", "宇宙级常识"]) == "（你最近几条常说「喂喂」「宇宙级常识」，这次换个说法，别再用了。）"
    prompt = build_turn_prompt([Message("user", "主人", "hi")], 1, repeats=["喂喂"])
    assert prompt.endswith("（你最近几条常说「喂喂」，这次换个说法，别再用了。）")
    assert "最近几条常说" not in build_turn_prompt([Message("user", "主人", "hi")], 1)


async def test_room_reminds_the_member_of_its_own_repeats(make, store, round_cfg):
    a = make("a", ["喂喂，第一句", "喂喂，第二句", "第三句"])
    b = make("b", ["喂喂，b 的话"] * 3)
    room = Conversation("chat", [a], FakeSink(), round_cfg, store)
    for text in ("一", "二", "三"):
        room.add_user_message("me", text)
        await room.wait_idle()
    assert "常说" not in a.prompts[1]
    assert "（你最近几条常说「喂喂」" in a.prompts[2]
    assert b.prompts == []
