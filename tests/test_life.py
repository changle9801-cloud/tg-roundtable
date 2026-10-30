from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from conftest import FakeSink
from test_web import admin, login  # noqa: F401

from nookling.config import ConfigError
from nookling.life import LifeEngine, LifeStore, MAX_PRIVATE_MESSAGES
from nookling.conversation import Conversation
from nookling.runtime import Runtime
from nookling.settings import SettingsStore, normalize_settings
from nookling.transcript import Message

NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}
MEMBERS = [{"id": "claude", "name": "Claude", "kind": "claude_cli", "command": ["python"]},
           {"id": "codex", "name": "Codex", "kind": "codex_cli", "command": ["python"]}]
IDEA = {"title": "修复频道的拉链系列", "why": "上次只看懂了拉头", "next_step": "继续看更换止码的方法"}


def observation(**overrides):
    return {"outcome": "progress", "progress": "链齿完好时可能只需更换拉头，型号还没确认", "next_step": "明天继续查型号",
            "sources": [{"url": "https://example.com/zipper", "title": "拉链修复", "read_level": "看了字幕和四张画面"}],
            "follow_up": "keep", **overrides}


class Script:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.public = []

    async def ask(self, pid, prompt):
        self.calls.append((pid, prompt))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)

    async def share(self, pid, text, sources=()):
        self.public.append((pid, text))
        return text

    def engine(self, store):
        return LifeEngine(store, self.ask, self.share, lambda pid: "最近公开消息：在聊修东西", NAMES)


def test_cross_day_reload_preserves_progress_and_limits_private_audience(tmp_path):
    clock = [time.time()]
    store = LifeStore(tmp_path, lambda: clock[0])
    item = store.choose_interest("codex", IDEA)
    store.observe(item, observation(progress="只有 Codex 看过的详细型号"))
    thread = store.new_thread("codex", "claude", "拉链")
    store.add_message(thread, "codex", "我发现可以单独换拉头")
    store.add_message(thread, "claude", "我想知道怎么分型号")
    clock[0] += 2 * 86400
    loaded = LifeStore(tmp_path, lambda: clock[0])
    assert loaded.of("codex")[0].id == item.id
    assert "明天继续查型号" in loaded.prompt_section("codex", NAMES)
    assert "详细型号" not in loaded.prompt_section("claude", NAMES)
    assert "我发现可以单独换拉头" in loaded.prompt_section("claude", NAMES)
    assert loaded.prompt_section("grok", NAMES) == ""


async def test_exploration_can_end_privately_and_continue_next_day(tmp_path):
    store = LifeStore(tmp_path)
    first = Script({"action": "explore", "interest": IDEA}, observation())
    await first.engine(store).step("codex", "claude", True)
    assert first.public == [] and store.threads == []
    item = store.of("codex")[0]
    next_day = LifeStore(tmp_path, lambda: time.time() + 86400)
    second = Script({"action": "explore", "interest": {"id": item.id}}, observation(progress="第二天找到了型号标记"))
    await second.engine(next_day).step("codex", "claude", True)
    assert "型号还没确认" in second.calls[0][1]
    assert len(next_day.interests) == 1 and next_day.of("codex")[0].progress == "第二天找到了型号标记"
    assert second.public == []


async def test_idle_is_a_complete_action_with_no_messages(tmp_path):
    script = Script({"action": "idle", "reason": "今天没有特别想继续的"})
    store = LifeStore(tmp_path)
    assert await script.engine(store).step("claude", "codex", True) == "idle"
    assert script.public == [] and not store.threads
    assert LifeStore(tmp_path).events[0]["action"] == "idle"


async def test_unsupported_findings_do_not_become_progress_or_a_public_report(tmp_path):
    script = Script({"action": "explore", "interest": IDEA}, observation(sources=[], follow_up="share", message="我看完了"))
    store = LifeStore(tmp_path)
    await script.engine(store).step("codex", "claude", True)
    assert store.of("codex")[0].progress == "尚未开始了解"
    assert script.public == []


async def test_private_dialogue_is_bounded_and_never_auto_published(tmp_path):
    script = Script({"action": "private", "message": "想听听你怎么看", "reason": "对方也在意这个"},
                    {"message": "我想先看来源", "done": False},
                    {"message": "给你链接 [画:图|偷偷触发]", "done": False},
                    {"message": "好，下次接着聊", "done": False})
    store = LifeStore(tmp_path)
    await script.engine(store).step("codex", "claude", True)
    assert len(store.threads[0]["messages"]) == MAX_PRIVATE_MESSAGES
    assert [p for p, _ in script.calls] == ["codex", "claude", "codex", "claude"]
    assert "[画:" not in store.threads[0]["messages"][2]["text"]
    assert script.public == [] and store.prompt_section("grok", NAMES) == ""


async def test_private_recipient_can_decline_and_sender_can_share_later(tmp_path):
    store = LifeStore(tmp_path)
    private = Script({"action": "private", "message": "你现在想聊吗"}, {"message": "[PASS]。", "done": True})
    await private.engine(store).step("claude", "codex", True)
    assert len(store.threads[0]["messages"]) == 1 and not private.public
    share = Script({"action": "share", "message": "我找到了昨天说的那段视频"})
    await share.engine(LifeStore(tmp_path)).step("claude", "codex", True)
    assert share.public == [("claude", "我找到了昨天说的那段视频")]
    assert "你现在想聊吗" in share.calls[0][1]


async def test_deferred_share_is_not_automatically_retried(tmp_path):
    store = LifeStore(tmp_path)
    script = Script({"action": "share", "message": "暂时想发"}, {"action": "idle"})
    engine = script.engine(store)
    async def defer(pid, text):
        return None
    engine.share = defer
    await engine.step("claude", "codex", True)
    engine.share = script.share
    await engine.step("claude", "codex", True)
    assert script.public == []
    assert [e["action"] for e in store.events] == ["deferred", "idle"]


def test_another_members_interest_cannot_be_modified(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("codex", IDEA)
    with pytest.raises(ConfigError, match="自己"):
        store.choose_interest("claude", {"id": item.id, "status": "done"})
    assert item.status == "active"


def test_a_third_interest_puts_the_oldest_on_hold_and_active_items_stay_in_context(tmp_path):
    clock = [1000.0]
    store = LifeStore(tmp_path, lambda: clock[0])
    first = store.choose_interest("codex", IDEA)
    clock[0] += 1
    second = store.choose_interest("codex", {**IDEA, "title": "另一个系列"})
    clock[0] += 1
    third = store.choose_interest("codex", {**IDEA, "title": "第三件"})
    assert (first.status, second.status, third.status) == ("paused", "active", "active")
    assert store.events[-1]["action"] == "paused" and first.title in store.events[-1]["note"]
    clock[0] += 1
    store.choose_interest("codex", {"id": first.id, "status": "active"})  # back on: the oldest active waits
    assert (first.status, second.status) == ("active", "paused")
    for i in range(8):
        item = store.choose_interest("codex", {**IDEA, "title": f"过去的事项{i}"})
        store.choose_interest("codex", {"id": item.id, "status": "done"})
    assert first.title in store.prompt_section("codex")


@pytest.mark.parametrize("replies", [["not json", "still not"], ['{"action":"delete"}'], ["[]", "[]"]])
async def test_invalid_decisions_end_as_a_quiet_rest(tmp_path, replies):
    script = Script(*replies)
    store = LifeStore(tmp_path)
    assert await script.engine(store).step("claude", "codex", True) == "blocked"
    assert not script.public and not store.threads and not store.interests
    assert store.events[-1]["action"] == "blocked" and "没做成" in store.events[-1]["note"]


async def test_bad_json_gets_one_more_try(tmp_path):
    script = Script("嗯……我想想", {"action": "idle", "reason": "先歇会儿"})
    store = LifeStore(tmp_path)
    assert await script.engine(store).step("claude", "codex", True) == "idle"
    assert "不是合法的 JSON" in script.calls[1][1] and store.events[-1]["note"] == "先歇会儿"


async def test_friends_take_turns_for_private_chats(tmp_path):
    store = LifeStore(tmp_path)
    charged = []
    first = Script({"action": "private", "message": "在吗"}, {"message": "", "done": True})
    engine = first.engine(store)
    engine.charge = charged.append
    await engine.step("codex", ["claude", "grok"], True)
    assert "你可以私聊的朋友：Claude（ID claude，还没私聊过）、Grok（ID grok，还没私聊过）" in first.calls[0][1]
    second = Script({"action": "private", "message": "又来了"}, {"message": "", "done": True})
    engine = second.engine(store)
    engine.charge = charged.append
    await engine.step("codex", ["claude", "grok"], True)
    assert [t["members"] for t in store.threads] == [["codex", "claude"], ["codex", "grok"]]  # the one not talked to yet
    assert charged == ["claude", "grok"]  # being messaged costs the friend an activity
    third = Script({"action": "private", "message": "找你", "peer": "claude"}, {"message": "", "done": True})
    await third.engine(store).step("codex", ["claude", "grok"], True)
    assert store.threads[-1]["members"] == ["codex", "claude"]  # its own choice wins


async def test_no_friend_free_means_no_private_chat(tmp_path):
    script = Script({"action": "private", "message": "在吗"})
    store = LifeStore(tmp_path)
    assert await script.engine(store).step("codex", [], True) == "blocked"
    assert "这次不能私聊" in script.calls[0][1] and not store.threads


async def test_looking_around_stays_light(tmp_path):
    from nookling.prompts import LIGHT_READING, build_browse_prompt

    script = Script({"action": "explore", "interest": IDEA}, observation())
    await script.engine(LifeStore(tmp_path)).step("codex", ["claude"], True)
    assert LIGHT_READING in script.calls[1][1] and "学术论文" in LIGHT_READING
    assert LIGHT_READING in build_browse_prompt("宇宙", True, [])


async def test_progress_is_reported_for_the_journal(tmp_path):
    store = LifeStore(tmp_path)
    found = []
    script = Script({"action": "explore", "interest": IDEA}, observation(title="拉链只换拉头", take="零件坏了不等于整件坏了"))
    engine = script.engine(store)
    engine.found = lambda pid, item, result: found.append((pid, item.title, result["title"], result["take"]))
    await engine.step("codex", ["claude"], True)
    assert found == [("codex", IDEA["title"], "拉链只换拉头", "零件坏了不等于整件坏了")]


def runtime(tmp_path):
    store = SettingsStore(tmp_path)
    store.save({"participants": MEMBERS, "life": {"enabled": True, "members": ["claude", "codex"],
                "hours": "0-24", "per_day": 2, "quiet_minutes": 0, "member_days": {"claude": 2, "codex": 2}},
                "proactive": {"chat_id": -100}})
    r = Runtime(store)
    r.sink = FakeSink()
    return r


async def test_daily_quota_survives_restart_and_resets_without_losing_interests(tmp_path):
    r = runtime(tmp_path)
    day = [datetime(2026, 10, 26, 12)]
    r.life_schedule.now = lambda: day[0]
    item = r.life.choose_interest("claude", IDEA)
    async def idle(pid, prompt): return '{"action":"idle"}'
    r._life_ask = idle
    r.start_life("claude"); await r._life_task
    restarted = Runtime(r.store)
    restarted.life_schedule.now = lambda: day[0]
    restarted._life_ask = idle
    restarted.start_life("claude"); await restarted._life_task
    with pytest.raises(ConfigError, match="用完"):
        restarted.start_life("claude")
    day[0] += timedelta(days=1)
    restarted.start_life("claude"); await restarted._life_task
    assert restarted.life_schedule.plan(restarted._life_policy(restarted._member("claude"))).posts == 1
    assert restarted.life.of("claude")[0].id == item.id


async def test_only_one_activity_runs_and_disabling_preserves_completed_progress(tmp_path):
    r = runtime(tmp_path)
    entered = asyncio.Event()
    call = 0
    async def ask(pid, prompt):
        nonlocal call
        call += 1
        if call == 1: return json.dumps({"action": "work", "interest": {**IDEA, "goal": "写一份自己的拉链笔记"},
                                        "work": {"operation": "write", "kind": "note"}})
        entered.set(); await asyncio.Event().wait()
    r._member("claude").cfg.web_search = True
    r._life_ask = ask
    r.start_life("claude")
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(ConfigError, match="忙"):
        r.start_life("codex")
    settings = dict(r.settings, life={**r.settings["life"], "enabled": False})
    await r.apply(settings=settings)
    assert r._life_task is None and LifeStore(tmp_path).of("claude")[0].title == IDEA["title"]
    assert r.life.events[-1]["action"] == "interrupted"


async def test_selected_members_skip_old_browse_and_forced_proactive_paths(tmp_path):
    r = runtime(tmp_path)
    r.life_schedule.due = lambda pc: False
    r.browsing.due = lambda pc: True
    r.proactive.due = lambda pc: True
    r.telegram.controller = object()
    async def unexpected(*args): pytest.fail("selected member entered legacy workflow")
    r._browse = unexpected
    r._proactive_attempt = unexpected
    await r._proactive_tick()
    assert not r._browsing_now


async def test_share_gate_and_actor_authored_message_reaches_existing_group_sink(tmp_path):
    r = runtime(tmp_path)
    r.telegram.controller = object()
    room = r.conversation(-100)
    async def no_extra_replies(*args, **kwargs): pass
    room._round_loop = no_extra_replies
    r.config.life.quiet_minutes = 30
    room.messages.append(Message("user", "User", "刚说完", (), time.time()))
    assert await r._life_share("claude", "不该现在发") is None
    assert not r.sink.posts
    r.config.life.quiet_minutes = 0
    asked = []
    async def public_reply(chat, prompt, *args, **kwargs):
        asked.append((chat, prompt))
        return "用群聊口吻说这句"
    r._member("claude").reply = public_reply
    assert await r._life_share("claude", "我想说这句") == "用群聊口吻说这句"
    await room.wait_idle()
    assert r.sink.posts == [("claude", "用群聊口吻说这句")]
    assert room.messages[-1].text == "用群聊口吻说这句"
    assert asked[0][0] == room.key and "我想说这句" in asked[0][1]
    assert not room.messages[-1].external_to_session
    r.life_posts.plan(r._life_policy(r._member("claude"))).posts = 2
    assert await r._life_share("claude", "超上限") is None


async def test_prepared_public_message_does_not_generate_an_extra_report(make, store, round_cfg):
    member = make("a", ["不应被调用"])
    sink = FakeSink()
    room = Conversation("x", [member], sink, round_cfg, store)
    future = room.start_proactive(member, lambda unseen: "", 0, prepared_text="顺手发个有意思的")
    assert await future == "顺手发个有意思的"
    await room.wait_idle()
    assert member.prompts == [] and sink.posts == [("a", "顺手发个有意思的")]


def test_settings_require_two_enabled_members():
    with pytest.raises(ConfigError, match="两位"):
        normalize_settings({"participants": MEMBERS, "life": {"enabled": True, "members": ["claude"]}})
    with pytest.raises(ConfigError, match="两位"):
        normalize_settings({"participants": MEMBERS, "life": {"enabled": True, "members": ["claude", "grok"]}})
    assert not normalize_settings({})["life"]["enabled"]


def test_everyone_may_join_and_claude_does_one_a_day():
    members = MEMBERS + [{"id": "grok", "name": "Grok", "kind": "grok_cli"},
                         {"id": "ds", "name": "DeepSeek", "kind": "api", "provider": "deepseek"}]
    life = normalize_settings({"participants": members, "life": {
        "enabled": True, "members": ["claude", "codex", "grok", "ds"], "per_day": 3, "member_days": {"grok": 5}}})["life"]
    assert life["member_days"] == {"claude": 1, "codex": 3, "grok": 5, "ds": 3}
    with pytest.raises(ConfigError, match="0 到 8"):
        normalize_settings({"participants": members, "life": {"members": ["claude", "codex"], "member_days": {"codex": 9}}})


async def test_friends_out_of_personal_activities_can_still_reply_privately(tmp_path):
    r = runtime(tmp_path)
    r.config.life.member_days = {"claude": 1, "codex": 2}
    asked = []

    async def ask(pid, prompt):
        asked.append(prompt)
        return '{"action":"idle"}'

    r._life_ask = ask
    r.start_life("claude")
    await r._life_task
    r.start_life("codex")
    await r._life_task
    assert "Claude（ID claude" in asked[-1]  # personal cap does not use passive replies
    with pytest.raises(ConfigError, match="用完"):
        r.start_life("claude")


async def test_findings_land_in_the_journal(tmp_path):
    r = runtime(tmp_path)
    item = r.life.choose_interest("codex", IDEA)
    r._life_found("codex", item, observation(title="拉链只换拉头", take="零件坏了不等于整件坏了"))
    entry = r.journal.of("codex")[0]
    assert (entry.title, entry.take, entry.url) == ("拉链只换拉头", "零件坏了不等于整件坏了", "https://example.com/zipper")
    assert "链齿完好" in entry.summary


async def test_private_state_is_scoped_in_actual_system_prompts(tmp_path):
    r = runtime(tmp_path)
    thread = r.life.new_thread("claude", "codex", "独有的话题")
    r.life.add_message(thread, "claude", "只有我们知道的进展")
    r.refresh_prompts()
    assert "只有我们知道的进展" in r._member("codex").with_turn_context("刚才私下聊了什么")
    from nookling.config import ParticipantConfig
    third = ParticipantConfig(id="grok", name="Grok", kind="grok_cli")
    assert "只有我们知道的进展" not in r.system_prompt_for(third)
    assert "只有我们知道的进展" not in r._chat_context(third, "刚才私下聊了什么")


async def test_background_actor_keeps_assigned_tools_and_separate_sessions(tmp_path, monkeypatch):
    r = runtime(tmp_path)
    from nookling.tools import ToolServer
    tool = ToolServer("video", "看视频", command="python", allowed=["watch_video"], members=["codex"])
    r.config.tools = [tool]
    seen = {}
    class Actor:
        def __init__(self, cfg, sessions, work, timeout):
            seen.update(sessions=sessions, work=work)
        def set_system_prompt(self, prompt): pass
        async def reply(self, key, prompt, on_activity=None):
            seen.update(key=key, tools=self.tools); return "{}"
        def reset(self, key): seen["reset"] = key
    monkeypatch.setattr("nookling.runtime.participant_class", lambda cfg: Actor)
    await r._life_ask("codex", "决定下一步")
    assert seen["tools"] == [tool] and seen["sessions"] is not r.sessions
    assert seen["work"] == tmp_path / "work" / "life" / "codex"
    assert seen["reset"] == seen["key"]


async def test_life_api_requires_auth_and_origin_and_runs_asynchronously(admin):
    assert (await admin.get("/api/life")).status == 401
    await login(admin)
    assert (await admin.get("/api/life")).status == 200
    assert (await admin.post("/api/life/claude/step")).status == 403
    headers = {"Origin": admin.origin}
    assert (await admin.post("/api/life/claude/step", headers=headers)).status == 400
    settings = {"participants": MEMBERS, "life": {"enabled": True, "members": ["claude", "codex"]}}
    assert (await admin.put("/api/settings", json=settings, headers=headers)).status == 200
    waiting = asyncio.Event()
    async def delayed(pid, prompt):
        await waiting.wait()
        return '{"action":"idle","reason":"先歇着"}'
    admin.runtime._life_ask = delayed
    response = await admin.post("/api/life/claude/step", headers=headers)
    assert response.status == 200 and (await response.json())["running"] == "claude"
    task = admin.runtime._life_task
    assert (await admin.post("/api/life/codex/step", headers=headers)).status == 400
    waiting.set()
    await task
    state = await (await admin.get("/api/life")).json()
    assert state["running"] == "" and state["events"][-1]["action"] == "idle"
    assert state["plans"]["claude"]["posts_today"] == 1
