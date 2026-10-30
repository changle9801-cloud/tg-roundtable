"""Moments from the real group where who speaks next decided whether it felt like friends.

Each test replays one situation with stand-in members and checks how much someone wants to
speak (docs/urge-to-speak.md, "怎么检验"). The dice are loaded high, so only owed answers come
and the chances computed along the way can be read from ``room.decisions``.
"""

from __future__ import annotations

import random
import time
from statistics import mean

from conftest import FakeSink

from roundtable.config import RoundConfig
from roundtable.moderator import RuleModerator
from roundtable.room import ChatRoom
from roundtable.routine import Routines
from roundtable.transcript import Message

CFG = RoundConfig(max_turns_per_model=2, call_timeout=5, follow_up_chance=0.5, follow_up_decay=0.4)


def room_with(members, store, *, seed=0, quiet=True, routines=None):
    room = ChatRoom("chat", members, FakeSink(), CFG, store, RuleModerator(random.Random(seed)), routines=routines)
    if quiet:
        room.moderator.rng.random = lambda: 0.99  # nobody speaks unless owed: read the chances
    return room


def chance(room, who, what="想说话"):
    """The last chance computed for ``who``."""
    return next(d for d in reversed(room.decisions) if d["who"] == who and d["what"] == what)


def member(make, pid, replies=(), activity="normal", aliases=()):
    p = make(pid, list(replies), aliases=aliases)
    p.cfg.activity = activity
    return p


async def test_a_picture_for_one_draws_the_other_over(make, store, tmp_path):
    # 阿明 sends Claude her ID card; Codex is awake and should want to look too.
    claude = member(make, "claude", ["出生日期倒是认真填了"], "low")
    codex = member(make, "codex")
    room = room_with([claude, codex], store)
    room.add_user_message("阿明", "[发送了一张图片] @Claude 看你的身份卡", images=[str(tmp_path / "card.jpg")])
    await room.wait_idle()
    assert chance(room, "Codex")["chance"] >= 0.6


async def test_overridden_she_talks_back(make, store):
    # Grok says don't add anything; Codex redraws her card anyway.
    grok = member(make, "grok", ["少写是我故意的，别往上塞生日地址"], "high")
    codex = member(make, "codex", ["嗯，这次照整套的版式补齐 [画:Grok新版卡|金发双马尾的 Grok，黑金身份卡]"])
    room = room_with([grok, codex], store)
    room.add_user_message("阿明", "@Grok 你看看，@Codex 给她重做一张")
    await room.wait_idle()
    assert [pid for pid, _ in room.sink.posts] == ["grok", "codex"]
    back = chance(room, "Grok")
    assert back["chance"] >= 0.7 and "说的是她" in back["why"] and "接她的话" in back["why"]


async def test_a_friend_asks_her_by_name(make, store):
    # DeepSeek wakes up and asks Codex what he was turned into.
    deepseek = member(make, "deepseek", aliases=["大肥鱼"])
    codex = member(make, "codex", aliases=["干面包"])
    claude = member(make, "claude", activity="low")
    room = room_with([deepseek, codex, claude], store)
    await room.start_proactive(deepseek, lambda unseen: "", 1, prepared_text="我醒了，Codex 你这是被换成什么了呀")
    await room.wait_idle()
    assert chance(room, "Codex")["chance"] >= 0.6
    assert chance(room, "Claude")["chance"] < 0.3  # not her question


async def test_one_answer_to_a_plain_acknowledgement(make, store):
    counts = []
    for seed in range(200):
        people = [member(make, pid, ["嗯嗯"] * 3) for pid in ("a", "b", "c", "d")]
        room = room_with(people, store, seed=seed, quiet=False)
        room._append(Message("user", "阿明", "今天把柜子收拾完了", (), 1_800_000_000))
        room._append(Message("a", "A", "收拾得怎么样", (), 1_800_000_010))
        room.cursors = {p.id: len(room.messages) for p in people}
        room._append(Message("user", "阿明", "嗯", (), 1_800_000_020))
        room._task = None
        await room._run_round()
        counts.append(len(room.sink.posts))
    assert mean(counts) <= 1.4


async def test_others_mostly_leave_a_two_person_chat_alone(make, store):
    a, b = member(make, "a"), member(make, "b")
    room = room_with([a, b], store)
    for k, (who, text) in enumerate([("user", "我在看房子"), ("a", "看哪边的"), ("user", "东边那片"), ("a", "贵吗")]):
        room._append(Message(who, who, text, (), 1_800_000_000 + k))
    room.cursors = {"a": len(room.messages), "b": len(room.messages)}
    room._append(Message("user", "阿明", "还行，比去年便宜", (), 1_800_000_010))
    await room._run_round()
    assert chance(room, "B")["chance"] <= 0.3


async def test_mentioned_in_passing_while_asleep(make, store):
    codex = member(make, "codex")
    deepseek = member(make, "deepseek", ["……谁叫我"], aliases=["大肥鱼"])
    deepseek.cfg.sleep = "00:00-23:59"
    room = room_with([codex, deepseek], store, routines=Routines())
    await room.start_proactive(codex, lambda unseen: "", 1, prepared_text="早，大肥鱼估计还在睡")
    await room.wait_idle()
    assert chance(room, "Deepseek", "吵醒")["chance"] <= 0.4


async def test_two_friends_bicker_a_little_then_stop(make, store):
    longest = []
    for seed in range(200):
        a = member(make, "a", ["才不是", "就不是"], "high")
        b = member(make, "b", ["就是", "就是就是"], "high")
        room = room_with([a, b], store, seed=seed, quiet=False)
        room.add_user_message("阿明", "@A 你是不是又熬夜了")
        await room.wait_idle()
        longest.append(len(room.sink.posts))
    assert max(longest) <= 4 and mean(longest) < 2.6


async def test_twenty_minutes_later_you_still_means_her(make, store):
    # Codex answered 阿明 about magnetic controllers; 20 quiet minutes later 阿明 asks "你有想玩的游戏吗".
    codex, deepseek = member(make, "codex"), member(make, "deepseek")
    room = room_with([codex, deepseek], store)
    t = time.time()
    room._append(Message("user", "阿明", "磁吸手柄真的靠谱吗", (), t - 1230))
    room._append(Message("codex", "Codex", "我看悬", (), t - 1200))
    room.cursors = {p.id: len(room.messages) for p in (codex, deepseek)}
    room._append(Message("user", "阿明", "你有想玩的游戏吗", (), t))
    await room._run_round()
    assert "刚和阿明说话的是她" in chance(room, "Codex")["why"] and chance(room, "Codex")["chance"] >= 0.8

    # A post nobody asked for, 20 minutes ago, is not a conversation still going on.
    room2 = room_with([member(make, "codex"), member(make, "deepseek")], store)
    room2._append(Message("codex", "Codex", "刚看了个访谈", (), t - 1200))
    room2.cursors = {"codex": 1, "deepseek": 1}
    room2._append(Message("user", "阿明", "你有想玩的游戏吗", (), t))
    await room2._run_round()
    assert "刚和" not in chance(room2, "Codex")["why"]


async def test_wanting_to_answer_you_survives_someone_going_first(make, store):
    # "你们都各自想玩什么游戏": three want to answer; after the first, the others still get their say.
    people = [member(make, pid, ["种田", "拆迁", "上天"]) for pid in ("a", "b", "c")]
    room = room_with(people, store, quiet=False)
    room.moderator.rng.random = lambda: 0.3  # under answering everyone (0.6), over a later afterthought
    room.add_user_message("阿明", "你们都各自想玩什么游戏")
    await room.wait_idle()
    assert {pid for pid, _ in room.sink.posts} == {"a", "b", "c"}  # everyone who wanted to got their say
    assert any(d["what"] == "还想回" for d in room.decisions)
