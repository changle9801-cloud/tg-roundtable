import random
import time
from dataclasses import replace

from nookling.apps.house import HouseSink
from nookling.conversation import Conversation
from nookling.home import Home
from nookling.moderator import RuleModerator
from nookling.runtime import SAID_WINDOW, TALK_LINGER
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex", "grok": "Grok"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def test_she_comes_to_the_living_room_to_talk_and_goes_back(tmp_path):
    home = house(tmp_path)
    home.begin("codex", "read", "一篇文章")
    before = home.where["codex"]
    home.talk("codex")
    assert (home.where["codex"].room, home.where["codex"].verb) == ("living", "talk")
    home.talk("codex")  # speaking again changes nothing
    home.end_talk("codex")
    back = home.where["codex"]
    assert (back.room, back.verb, back.piece) == (before.room, before.verb, before.piece) and not back.active


def test_after_a_restart_she_still_goes_back_to_her_room(tmp_path):
    home = house(tmp_path)
    home.talk("grok")
    again = Home(tmp_path)  # what she was doing is not saved, where she came from is
    again.end_talk("grok")
    assert (again.where["grok"].room, again.where["grok"].verb) == ("bed-grok", "rest")


def test_someone_out_does_not_come_to_talk(tmp_path):
    home = house(tmp_path)
    home.settle("grok", "busy")
    home.talk("grok")
    assert home.where["grok"].verb == "out"


async def test_the_family_talks_at_home_and_whoever_speaks_comes_over(make, round_cfg, store, tmp_path):
    spoke = []
    sink = HouseSink(on_speak=spoke.append)
    a = make("a", ["来啦"])
    talk = Conversation("__home", [a], sink, round_cfg, store, RuleModerator(random.Random(0)),
                        log_path=tmp_path / "__home.jsonl", scene=lambda: "（这次是在家里当面说话。）")
    talk.add_user_message("主人", "谁想喝奶茶")
    await talk.wait_idle()
    assert [m.text for m in talk.messages] == ["谁想喝奶茶", "来啦"]
    assert spoke == ["a"] and not sink.thinking
    assert "（这次是在家里当面说话。）" in a.prompts[0]
    assert (tmp_path / "__home.jsonl").exists()  # kept like a group's transcript


async def test_the_owner_speaks_from_the_house_page(admin):  # noqa: F811
    await login(admin)
    h = {"Origin": admin.origin}
    said = await admin.post("/api/home/talk", json={"text": "大家晚上好"}, headers=h)
    assert said.status == 200
    line = (await said.json())["messages"][-1]
    assert line["text"] == "大家晚上好" and line["owner"]
    view = await (await admin.get("/api/home")).json()
    assert view["talk"]["messages"][-1]["text"] == "大家晚上好"
    empty = await admin.post("/api/home/talk", json={"text": "  "}, headers=h)
    assert empty.status == 400


async def test_after_a_quiet_while_they_go_back(admin):  # noqa: F811
    runtime = admin.runtime
    runtime.home.ensure({"codex": "Codex"})
    runtime.home.talk("codex")
    runtime.say_at_home("在吗")  # the talk has just begun: she stays
    await runtime.house_talk().wait_idle()
    runtime._talk_tick()
    assert runtime.home.where["codex"].verb == "talk"
    runtime.house_talk().messages[-1] = replace(runtime.house_talk().messages[-1], ts=time.time() - TALK_LINGER - 1)
    runtime._talk_tick()
    assert runtime.home.where["codex"].verb != "talk"


async def test_the_page_is_told_what_was_just_said(admin):  # noqa: F811
    await login(admin)
    runtime = admin.runtime
    runtime.home.ensure({"codex": "Codex", "grok": "Grok"})
    runtime.say_at_home("开饭啦")
    old = runtime.life.new_thread("codex", "grok", "旧事")
    runtime.life.add_message(old, "codex", "上周那个")
    old["messages"][-1]["ts"] = time.time() - SAID_WINDOW - 5  # long said: no bubble for it
    thread = runtime.life.new_thread("codex", "grok", "看电影")
    runtime.life.add_message(thread, "codex", "今晚看什么")
    said = (await (await admin.get("/api/home")).json())["said"]
    assert [(s["kind"], s["who"], s["text"]) for s in said] == [("home", "user", "开饭啦"), ("private", "codex", "今晚看什么")]
    assert said[0]["owner"] and said[1]["peer"] == "grok"
