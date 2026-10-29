"""Game memories: facts from the action log, feelings that fade, opinions that stay."""

from __future__ import annotations

import asyncio
import json

from roundtable.game_memories import (
    GameMemoryStore, decision_prompt, fallback_decision, parse_decision, parse_memories, template_memories,
)
from roundtable.games.story import signals, worth_remembering
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore

DAY = 86400.0


def card(cid, name=None, kind="ATTACK"):
    return {"id": cid, "name": name or cid, "type": kind}


BASE_DECK = [card("Strike_R", "Strike"), card("Defend_R", "Defend", "SKILL"), card("Bash")]
STRENGTH = BASE_DECK + [card("Inflame", kind="POWER"), card("Heavy Blade"), card("Twin Strike")]


def game(floor, hp, deck, phase="COMPLETE", screen="MAP", monsters=(), hand=(), **extra):
    state = {"floor": floor, "act": 1, "current_hp": hp, "max_hp": 80, "class": "IRONCLAD", "deck": list(deck),
             "room_phase": phase, "screen_type": screen, **extra}
    if phase == "COMBAT":
        state["combat_state"] = {"monsters": [dict(m) for m in monsters], "hand": list(hand)}
    return {"in_game": True, "game": state}


def event(before, after, command="proceed", reason=""):
    return {"command": command, "reason": reason, "before": before, "after": after, "error": None}


def nob(hp):
    return {"name": "Gremlin Nob", "current_hp": hp, "max_hp": 85, "is_gone": hp <= 0, "half_dead": False}


def climb_events():
    louse = {"name": "Louse", "current_hp": 30, "max_hp": 30, "is_gone": False, "half_dead": False}
    dead_louse = {**louse, "current_hp": 0, "is_gone": True}
    return [
        # a reward skipped while hoping for Demon Form
        event(game(2, 70, BASE_DECK, screen="CARD_REWARD", screen_state={"cards": [card("Cleave")]}),
              game(2, 70, BASE_DECK), "skip", "想要 Demon Form，先不拿"),
        # the strength build comes together on floor 4
        event(game(3, 70, BASE_DECK), game(4, 70, STRENGTH), "choose 1", "Demon Form 还没来，先拿 Inflame"),
        # a close call: one Heavy Blade kills a 30 HP louse, leaving 9 HP
        event(game(5, 12, STRENGTH, "COMBAT", monsters=[louse], hand=[card("Heavy Blade")]),
              game(5, 9, STRENGTH, "COMBAT", monsters=[dead_louse]), "play 1 0", "Demon Form 估计这局等不到了"),
        event(game(5, 9, STRENGTH, "COMBAT", monsters=[dead_louse]), game(5, 9, STRENGTH), "proceed"),
        # killed by the Gremlin Nob on floor 7
        event(game(7, 9, STRENGTH, "COMBAT", monsters=[nob(60)], hand=[card("Strike_R", "Strike")]),
              game(7, 0, STRENGTH, "COMBAT", screen="GAME_OVER", monsters=[nob(54)]), "end"),
    ]


def test_signals_read_the_story_from_the_actions():
    sig = signals(climb_events())
    assert sig["build"] == {"name": "力量", "formed": True, "formed_floor": 4,
                            "cards": ["Heavy Blade", "Inflame", "Twin Strike"]}
    assert sig["died"] and sig["killer"] == ["Gremlin Nob"] and sig["death_floor"] == 7
    assert sig["wanted"] == ["Demon Form"]
    assert sig["close_calls"] == [{"floor": 5, "hp": 9, "max_hp": 80, "monsters": ["Louse"]}]
    assert sig["key_plays"] == [{"floor": 5, "card": "Heavy Blade", "monster": "Louse"}]
    assert sig["skipped_rewards"] == 1
    assert "Gremlin Nob" in sig["fact_names"] and "Strike" not in sig["fact_names"]
    assert worth_remembering(sig)
    assert not worth_remembering(signals(climb_events()[:1]))  # floor 2, nothing going on


def test_made_up_names_and_numbers_are_dropped():
    sig = signals(climb_events())
    raw = json.dumps([
        {"kind": "遭遇", "subject": "Gremlin Nob", "feeling": "不服", "intensity": 0.6,
         "story": "第 7 层被 Gremlin Nob 打死了", "tags": ["Gremlin Nob"]},
        {"kind": "时刻", "subject": "Strike", "feeling": "得意", "intensity": 0.5,
         "story": "一张 Strike 打出 40 伤害", "tags": []},  # Strike didn't do anything special; 40 never happened
        {"kind": "成绩", "subject": "纪录", "feeling": "得意", "intensity": 0.5, "story": "打到第 12 层"},
        {"kind": "瞎编", "subject": "x", "feeling": "开心", "intensity": 0.5, "story": "随便"},
    ], ensure_ascii=False)
    kept = parse_memories("好的：\n" + raw, sig, "grok", "c1")
    assert [m["story"] for m in kept] == ["第 7 层被 Gremlin Nob 打死了"]
    assert kept[0]["facts"]["killer"] == ["Gremlin Nob"]
    assert parse_memories("不是 JSON", sig, "grok", "c1") == []
    fallback = template_memories(sig, "grok", "c1")
    assert [m["kind"] for m in fallback] == ["遭遇", "流派", "手气"]
    assert all(parse_memories(json.dumps([m], ensure_ascii=False), sig, "grok", "c1") for m in fallback)


def test_feelings_fade_and_set_the_mood(tmp_path):
    clock = [1000 * DAY]
    store = GameMemoryStore(tmp_path, lambda: clock[0])
    sig = signals(climb_events())
    [mem, *_] = store.add(template_memories(sig, "grok", "c1"))
    assert "你最近的游戏心情：第 7 层死在 Gremlin Nob 手里，不服" in store.prompt_section("grok")
    clock[0] += 3 * DAY
    assert abs(mem.strength(clock[0]) - mem.intensity / 2) < 1e-9  # half-life of three days
    assert store.mood("grok") == ""
    store.set_status(mem.id, "pinned")
    assert "Gremlin Nob" in store.mood("grok")  # pinned memories don't fade
    store.set_status(mem.id, "hidden")
    assert "Gremlin Nob" not in store.mood("grok")
    assert GameMemoryStore(tmp_path).items("grok")[0].status == "hidden"  # saved


def test_what_keeps_happening_becomes_an_opinion(tmp_path):
    store = GameMemoryStore(tmp_path)
    sig = signals(climb_events())
    unformed = {**sig, "build": {**sig["build"], "formed": False}, "floor": 7}
    outcomes = [store.record_climb("grok", unformed) for _ in range(3)]
    assert outcomes[1]["nemesis"] == "Gremlin Nob"
    lines = store.opinion_lines("grok")
    assert "觉得力量流太看脸（试了 3 次只成过 0 次）" in lines
    assert "跟 Gremlin Nob 有仇（被它打死过 3 次）" in lines
    assert "每次都等不到 Demon Form" in lines
    assert store.profile("grok").startswith("你以前玩过 3 局：")
    better = store.record_climb("grok", {**sig, "floor": 20, "died": False, "killer": []})
    assert better["new_record"]
    store.clear_opinion("grok", "Gremlin Nob")
    assert not any("Gremlin Nob" in line for line in store.opinion_lines("grok", 10))


def test_chat_brings_back_the_matching_memory(tmp_path):
    store = GameMemoryStore(tmp_path)
    store.add(template_memories(signals(climb_events()), "grok", "c1"))
    hit = store.recall("grok", "今天又遇到 Gremlin Nob 了，好难打")
    assert "Gremlin Nob" in hit and hit.startswith("[你的游戏记忆]")
    assert "力量流" in store.recall("grok", "力量好玩吗")
    assert store.recall("grok", "晚饭吃什么") == ""
    assert store.recall("grok", "最近玩了什么游戏") != ""  # games come up: the latest ones
    assert store.recall("deepseek", "Gremlin Nob") == ""  # only its own memories


def test_climbs_span_play_sessions(tmp_path):
    store = GameMemoryStore(tmp_path)
    first = {"id": "r1", "member": "grok", "first": {"floor": 0}, "started_at": 1.0, "status": "paused"}
    later = {"id": "r2", "member": "grok", "first": {"floor": 3}, "started_at": 2.0, "status": "paused"}
    over = {"id": "r3", "member": "grok", "first": {"floor": 6}, "started_at": 3.0, "status": "finished",
            "stopped_because": "game_over", "game_over": {"victory": False}}
    assert store.attach(first) == ("c_r1", False)
    assert store.attach(later) == ("c_r1", False)
    assert store.attach(later) == ("c_r1", False)  # the same session twice is not a new one
    assert store.attach(over) == ("c_r1", True)
    assert store.climbs["c_r1"]["receipts"] == ["r1", "r2", "r3"]
    assert store.attach({**first, "id": "r4"})[0] == "c_r4"  # floor 0 again: a new climb


def test_repeated_sour_memories_are_suggested_as_a_joke(tmp_path):
    store = GameMemoryStore(tmp_path)
    sig = signals(climb_events())
    sig = {**sig, "build": {**sig["build"], "formed": False}}
    for i in range(3):
        store.add([m for m in template_memories(sig, "grok", f"c{i}") if m["kind"] == "流派"])
    assert store.nominations("grok", "Grok") == ["Grok的力量流从来没成过"]
    assert store.nominations("grok", "Grok") == []  # only once


class FakeGames:
    def __init__(self, receipts, events):
        self._receipts, self._events = receipts, events

    def receipts(self, member=None):
        return [r for r in self._receipts if member is None or r["member"] == member]

    def events(self, receipt):
        return self._events[receipt["id"]]


async def test_a_finished_climb_is_remembered(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    member = type("P", (), {"id": "grok", "name": "Grok",
                            "cfg": type("C", (), {"kind": "grok_cli", "persona": "好胜"})()})()
    rt.participants = [member]
    rt.refresh_prompts = lambda: None
    rt._chronicle_writer = lambda: None  # no cheap model around: plain wording from the facts
    receipt = {"id": "r1", "member": "grok", "first": {"floor": 0}, "started_at": 1.0, "companions": [],
               "stopped_because": "game_over", "status": "finished", "game_over": {}}
    rt.games = FakeGames([receipt], {"r1": climb_events()})
    cid, ended = rt.game_memories.attach(receipt)
    added = await rt._remember_climb(cid)
    assert ended and [m.kind for m in added] == ["遭遇", "流派", "手气"]
    assert await rt._remember_climb(cid) == []  # only once
    assert "力量流" in rt.game_memories.prompt_section("grok")
    assert rt.game_memories.opinions["grok"]["climbs"] == 1


def test_local_previews_leave_no_memory(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt._record_game({"id": "r1", "member": "grok", "publication_target": "local_preview", "actions": 5})
    assert rt.game_memories.climbs == {}


async def test_the_cheap_model_writes_them_in_the_members_voice(tmp_path, monkeypatch):
    import roundtable.runtime as runtime_module

    asked = []

    class Writer:
        def __init__(self, *args):
            pass

        def set_system_prompt(self, text):
            pass

        async def reply(self, key, prompt):
            asked.append(prompt)
            return json.dumps([{"kind": "遭遇", "subject": "Gremlin Nob", "feeling": "记仇", "intensity": 0.6,
                                "story": "又是 Gremlin Nob，第 7 层送走我", "tags": ["Gremlin Nob"]}], ensure_ascii=False)

        def reset(self, key):
            pass

    monkeypatch.setattr(runtime_module, "participant_class", lambda cfg: Writer)
    rt = Runtime(SettingsStore(tmp_path))
    rt.refresh_prompts = lambda: None
    deepseek = type("P", (), {"id": "deepseek", "name": "DeepSeek", "cfg": type("C", (), {"kind": "api", "persona": "软萌"})()})()
    grok = type("P", (), {"id": "grok", "name": "Grok", "cfg": type("C", (), {"kind": "grok_cli", "persona": "好胜"})()})()
    rt.participants = [grok, deepseek]
    rt._chronicle_writer = lambda: deepseek
    added = await rt._write_game_memories(grok, "c1", signals(climb_events()))
    assert [m.story for m in added] == ["又是 Gremlin Nob，第 7 层送走我"]
    assert "你是 Grok" in asked[0] and "好胜" in asked[0]


async def test_admin_page_manages_game_memories(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    from test_web import free_port, login
    from roundtable.web.server import make_app

    store = SettingsStore(tmp_path)
    rt = Runtime(store)
    port = free_port()
    client = TestClient(TestServer(make_app(rt, store.web_token(), port), host="127.0.0.1", port=port))
    await client.start_server()
    client.token = store.web_token()
    try:
        await login(client)
        headers = {"Origin": f"http://127.0.0.1:{port}"}
        [mem, *_] = rt.game_memories.add(template_memories(signals(climb_events()), "grok", "c1"))
        rt.game_memories.record_climb("grok", signals(climb_events()))
        state = await (await client.get("/api/state")).json()
        assert state["game_memories"]["items"][0]["icon"] and "grok" in state["game_memories"]["opinions"]
        r = await client.post(f"/api/game-memories/{mem.id}/pinned", headers=headers)
        assert [m["status"] for m in (await r.json())["game_memories"]["items"] if m["id"] == mem.id] == ["pinned"]
        r = await client.put(f"/api/game-memories/{mem.id}", json={"story": "Gremlin Nob 太凶了"}, headers=headers)
        assert rt.game_memories.get(mem.id).story == "Gremlin Nob 太凶了"
        assert (await client.post(f"/api/game-memories/{mem.id}/weird", headers=headers)).status in (404, 405)
        await client.delete(f"/api/game-opinions/grok/Gremlin Nob", headers=headers)
        assert "Gremlin Nob" not in rt.game_memories.opinions["grok"]["monsters"]
        await client.delete(f"/api/game-memories/{mem.id}", headers=headers)
        assert all(m.id != mem.id for m in rt.game_memories.memories)
    finally:
        await client.close()


def test_after_a_climb_it_decides_for_itself(tmp_path):
    sig = signals(climb_events())
    sig["stood_out"] = {"new_record": False, "nemesis": "Gremlin Nob"}
    prompt = decision_prompt("DeepSeek", "容易泄气", sig, "（你最近的游戏心情：……）", ["跟 Gremlin Nob 有仇"])
    assert "容易泄气" in prompt and "rest=这游戏先放几天" in prompt and "跟 Gremlin Nob 有仇" in prompt
    raw = '{"choice":"rest","days":10,"why":"又被 Gremlin Nob 打死，烦","say":"Gremlin Nob 到底怎么打"}'
    assert parse_decision(raw, sig) == {"choice": "rest", "days": 7, "why": "又被 Gremlin Nob 打死，烦",
                                        "say": "Gremlin Nob 到底怎么打"}
    made_up = parse_decision('{"choice":"help","why":"想问问","say":"我一张 Strike 打了 40 怎么还输"}', sig)
    assert made_up["choice"] == "help" and made_up["say"] == ""  # no such hit in this climb
    assert parse_decision('{"choice":"quit_forever","why":"x"}', sig) is None
    assert fallback_decision(sig)["choice"] == "rest"
    assert fallback_decision({**sig, "stood_out": {}, "close_calls": [1]})["choice"] == "again"


def test_a_game_put_aside_stays_aside_until_the_time_is_up(tmp_path):
    clock = [1000 * DAY]
    store = GameMemoryStore(tmp_path, lambda: clock[0])
    store.record_climb("deepseek", signals(climb_events()))
    store.decide("deepseek", {"choice": "rest", "days": 2, "why": "连着输，烦", "say": ""})
    assert store.resting("deepseek")
    assert "这游戏先放几天（连着输，烦）" in store.play_note("deepseek")
    assert "这游戏先放几天" in store.prompt_section("deepseek")
    assert "这次是阿明让你开的" in store.profile("deepseek")  # the owner may still start it
    assert "这游戏先放几天" in store.opinions_view("deepseek")["plan"]
    clock[0] += 2 * DAY + 1
    assert not store.resting("deepseek") and store.play_note("deepseek") == ""
    store.decide("deepseek", {"choice": "later", "days": None, "why": "过两天再说", "say": ""})
    store.done("deepseek")
    assert store.intention("deepseek") is None
    assert GameMemoryStore(tmp_path).intentions["deepseek"]["status"] == "done"  # saved


async def test_wanting_another_round_starts_one_when_allowed(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.refresh_prompts = lambda: None
    rt._chronicle_writer = lambda: None
    member = type("P", (), {"id": "grok", "name": "Grok", "cfg": type("C", (), {"kind": "grok_cli", "persona": "好胜"})()})()
    rt.participants = [member]
    started = []
    rt._play_again = lambda pid: started.append(pid) or _done()
    sig = {**signals(climb_events()), "stood_out": {}, "close_calls": [1]}
    intent = await rt._decide_next(member, sig)
    await asyncio.sleep(0)
    assert intent["choice"] == "again" and started == ["grok"]
    assert rt.game_memories.intention("grok")["why"] == "差一点，想再来"


async def _done():
    return None


def test_daily_life_does_not_offer_a_game_put_aside(tmp_path):
    from roundtable.life import LifeEngine, LifeStore

    seen = []

    async def ask(member, prompt):
        seen.append(prompt)
        return '{"action":"idle","reason":"歇会儿"}'

    async def share(*args, **kwargs):
        return None

    engine = LifeEngine(LifeStore(tmp_path), ask, share, lambda pid: "", {"a": "A"},
                        play=lambda member, peer=None: None, can_play=lambda member: True,
                        play_note=lambda member: "\n你上一局打完时的打算：过阵子再玩（累了）")
    asyncio.run(engine.step("a", [], False))
    assert "这次还可以选择 play" in seen[0] and "过阵子再玩（累了）" in seen[0]
