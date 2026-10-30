"""Most messages get one or two replies, not one from everyone."""

from __future__ import annotations

import random
from statistics import mean

from conftest import FakeSink

from nookling.config import RoundConfig
from nookling.moderator import RuleModerator
from nookling.prompts import DEFAULT_SYSTEM_PROMPT, FRIEND_RULES, build_turn_prompt
from nookling.conversation import Conversation
from nookling.apps.telegram import bubbles, typing_pause
from nookling.transcript import Message


def room_for(members, store, chance, decay, seed=0):
    sink = FakeSink()
    cfg = RoundConfig(max_turns_per_model=3, call_timeout=5, follow_up_chance=chance, follow_up_decay=decay)
    return Conversation("chat", members, sink, cfg, store, RuleModerator(random.Random(seed))), sink


def four(make):
    return [make(pid, [f"{pid}{i}" for i in range(6)]) for pid in ("a", "b", "c", "d")]


async def test_zero_chance_means_exactly_one_reply(make, store):
    room, sink = room_for(four(make), store, chance=0.0, decay=0.0)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert len(sink.posts) == 1


async def test_an_at_mention_always_gets_an_answer(make, store):
    a = make("a", ["@B 你说呢"])
    b = make("b", ["我觉得行"])
    c = make("c", ["c0"])
    room, sink = room_for([a, b, c], store, chance=0.0, decay=0.0)
    room.add_user_message("me", "A 你好")  # a answers first
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b"]


async def test_an_earlier_mention_is_still_answered(make, store):
    # The user asks A and B; A then hands over to C. B was asked first and still answers.
    a = make("a", ["@C 你来"])
    b = make("b", ["我也觉得"])
    c = make("c", ["好"])
    d = make("d", ["d0"])
    room, sink = room_for([a, b, c, d], store, chance=0.0, decay=0.0)
    room.add_user_message("me", "@A @B 你们说说")
    await room.wait_idle()
    assert {pid for pid, _ in sink.posts} == {"a", "b", "c"}  # d was not asked and nobody chimes in


async def test_full_chance_lets_everyone_speak(make, store):
    room, sink = room_for(four(make), store, chance=1.0, decay=1.0)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert {pid for pid, _ in sink.posts} == {"a", "b", "c", "d"}


async def test_default_chances_keep_it_short(make, store):
    counts = []
    for seed in range(60):
        room, sink = room_for(four(make), store, chance=0.5, decay=0.4, seed=seed)
        room.add_user_message("me", "hi")
        await room.wait_idle()
        counts.append(len(sink.posts))
    # About 1 + 0.5 + 0.1, plus a comeback each now and then when two get talking.
    assert 1.5 < mean(counts) < 2.5
    assert max(counts) <= 6


async def test_a_friend_talks_back(make, store):
    # The user asks Codex to redo Grok's card; one says no, the other does it anyway: each answers back once.
    grok = make("grok", ["别加，就这样", "我说了不要"])
    codex = make("codex", ["嗯，这次补齐", "行吧"])
    room, sink = room_for([grok, codex], store, chance=0.5, decay=0.4)
    room.moderator.rng.random = lambda: 0.2  # under a comeback after one line (0.27), over nothing to say (0.05)
    room.add_user_message("me", "@Codex 给 @Grok 重做一张")
    await room.wait_idle()
    order = [pid for pid, _ in sink.posts]
    assert len(order) == 4 and order[0] != order[1] and order[:2] == order[2:]  # one comeback each, then done


async def test_naming_a_friend_draws_her_in(make, store):
    a, b, c = make("a", ["哈哈"]), make("b", ["在这"]), make("c", ["不该接"])
    room, sink = room_for([a, b, c], store, chance=0.5, decay=0.4)
    room.moderator.rng.random = lambda: 0.6  # under being named (1.0), over a plain reply to the post (0.55)
    await room.start_proactive(a, lambda unseen: "", 1, prepared_text="早，B 你那张卡呢")
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b"]  # B answers; C needn't


async def test_a_new_user_message_starts_a_fresh_chain(make, store):
    members = four(make)
    room, sink = room_for(members, store, chance=0.0, decay=0.0)
    room.add_user_message("me", "one")
    await room.wait_idle()
    room.add_user_message("me", "two")
    await room.wait_idle()
    assert len(sink.posts) == 2


def test_turn_prompt_has_no_assignment_or_quota():
    prompt = build_turn_prompt([Message("user", "主人", "hi")], 3)
    assert "轮到你" not in prompt and "还可以发言" not in prompt
    assert "[PASS]" in prompt


def test_every_turn_says_what_time_it_is():
    from datetime import datetime

    from nookling.prompts import build_proactive_prompt, now_line

    assert now_line(datetime(2026, 10, 25, 21, 5)) == "（现在是 2026年10月25日 星期日 晚上 21:05）"
    assert "星期一 凌晨 3:00" in now_line(datetime(2026, 10, 26, 3, 0))
    assert "中午 12:30" in now_line(datetime(2026, 10, 26, 12, 30))
    prompt = build_turn_prompt([Message("user", "主人", "hi")], 3, now=datetime(2026, 10, 25, 9, 0))
    assert prompt.startswith("（现在是 2026年10月25日 星期日 上午 9:00）\n[群聊新消息]")
    assert build_proactive_prompt([], "", False, False, []).startswith("（现在是 ")


def test_default_rules_include_friend_rules():
    assert FRIEND_RULES in DEFAULT_SYSTEM_PROMPT and "{friend_rules}" not in DEFAULT_SYSTEM_PROMPT
    assert "打圆场" in FRIEND_RULES and "行动兑现" in FRIEND_RULES
    assert "说话用口语" in FRIEND_RULES and "半句话" in FRIEND_RULES
    assert "喂喂" not in FRIEND_RULES  # no single opener for everyone to copy


def test_bubbles_and_pauses():
    assert bubbles("哈哈\n\n你这也太离谱了\n\n\n不过行吧") == ["哈哈", "你这也太离谱了", "不过行吧"]
    assert bubbles("一行\n两行") == ["一行\n两行"]
    assert len(bubbles("\n\n".join(str(i) for i in range(7)))) == 4
    assert typing_pause("短", first=True) < typing_pause("长" * 60, first=False) <= 5.0


def test_a_shared_topic_favors_an_interested_member_but_mentions_still_win(make):
    a, b = make("a", []), make("b", [])
    a.cfg.browse_topics = "休闲游戏、动漫、可爱动物"
    b.cfg.browse_topics = "神话、科幻小说"
    moderator = RuleModerator(random.Random(7))
    post = Message("c", "C", "这个你们看看", sources=({"url": "https://example.com/post",
                   "title": "休闲游戏的新玩法", "excerpt": "玩家可以一起照顾可爱动物。"},))
    picks = [moderator.pick([a, b], post, "c").id for _ in range(120)]
    assert picks.count("a") >= 80
    addressed = Message("c", "C", "@B 这个休闲游戏你玩过吗", mentions=("b",))
    assert moderator.pick([a, b], addressed, "c") is b


async def test_a_shared_topic_gets_two_bounded_replies_between_the_pair(make, store):
    a = make("a", ["透明壳也是我喜欢的", "不应该无限接"])
    b = make("b", ["我想要那个透明壳", "那下次找个实测看看", "不应该无限接"])
    room, sink = room_for([a, b], store, chance=0.0, decay=0.0)
    await room.start_proactive(a, lambda unseen: "", 1, prepared_text="这个掌机改装挺好玩", discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b", "a", "b"]
    assert len(a.prompts) == 1 and len(b.prompts) == 2


async def test_discussion_pass_ends_the_extra_exchange(make, store):
    a, b = make("a", ["[PASS]"]), make("b", ["这个我也喜欢", "不该再问一次"])
    room, sink = room_for([a, b], store, chance=0.0, decay=0.0)
    await room.start_proactive(a, lambda unseen: "", 1, prepared_text="分享一个视频", discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b"]
    assert len(a.prompts) == 1 and len(b.prompts) == 1


async def test_discussion_respects_a_members_single_turn_limit(make, store):
    a, b = make("a", ["我再说一句"]), make("b", ["好玩", "还想接"])
    b.cfg.max_turns = 1
    room, sink = room_for([a, b], store, chance=0.0, decay=0.0)
    await room.start_proactive(a, lambda unseen: "", 1, prepared_text="一个新视频", discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a", "b", "a"]


async def test_discussion_does_not_wake_a_sleeping_friend(make, store):
    from datetime import datetime
    from nookling.routine import Routines

    a, b = make("a", []), make("b", ["不该被叫醒"])
    b.cfg.sleep = "00:00-12:00"
    room, sink = room_for([a, b], store, chance=0.0, decay=0.0)
    room.routines = Routines(now=lambda: datetime(2026, 10, 28, 8))
    await room.start_proactive(a, lambda unseen: "", 1, prepared_text="@B 一个新视频", discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["a"] and not b.prompts


async def test_zero_reply_budget_still_disables_discussion(make, store):
    a, b = make("a", []), make("b", ["不该发"])
    room, sink = room_for([a, b], store, chance=0.0, decay=0.0)
    await room.start_proactive(a, lambda unseen: "", 0, prepared_text="一个新视频", discussion=True)
    await room.wait_idle()
    assert len(sink.posts) == 1 and not b.prompts
