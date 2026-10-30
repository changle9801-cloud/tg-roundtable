import pytest
from test_life import Script

from nookling import redecorate
from nookling.furniture import PlacementError
from nookling.home import Home
from nookling.life import LifeEngine, LifeStore
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex", "grok": "Grok"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def in_room(home, rid="bed-codex"):
    return [p for p in home.pieces if p["room"] == rid]


def test_she_sees_her_room_in_words_and_what_she_may_add(tmp_path):
    home = house(tmp_path)
    menu = redecorate.menu(home, "codex")
    bed = next(p for p in in_room(home) if p["type"] == "base/bed")
    assert f"- {bed['id']}：床（床）靠后墙" in menu and "这周还能改 3 次" in menu
    assert "- base/floor-lamp：落地灯" in menu and "base/stairs" not in menu
    assert "门在前墙" in menu


def test_she_adds_things_said_in_words_and_the_others_hear_of_it_once(tmp_path):
    home = house(tmp_path)
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "add", "kind": "base/beanbag", "name": "懒骨头", "color": "#AA3366", "where": {"against": "left"}},
        {"do": "add", "kind": "base/frame", "where": {"against": "right"}, "hang": "high"},
    ], "why": "想舒服点"})
    assert len(result["done"]) == 2 and not result["refused"]
    bag = next(p for p in in_room(home) if p["type"] == "base/beanbag")
    assert bag["at"][0] == 0 and bag["turn"] == 270 and bag["name"] == "懒骨头" and bag["color"] == "#aa3366"
    assert bag["functions"] == [] and bag["added_by"] == "codex"
    frame = next(p for p in in_room(home) if p["type"] == "base/frame")
    assert frame["wall"] == "right" and frame["y"] == 1.7
    assert "在自己卧室加了懒骨头" in home.news("grok", MEMBERS) and home.news("grok", MEMBERS) == ""
    assert home.news("codex", MEMBERS) == ""  # she knows what she did
    assert len(home.changes) == 2 and home.changes[0]["undo"]["added"] == bag["id"]


def test_moving_turning_colouring_naming_and_throwing_out(tmp_path):
    home = house(tmp_path)
    desk = next(p for p in in_room(home) if p["type"] == "base/writing-desk")
    rug = next(p for p in in_room(home) if p["type"] == "base/rug-small")
    lamp = next(p for p in in_room(home) if p["type"] == "base/floor-lamp")
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "move", "piece": desk["id"], "where": {"against": "right"}},
        {"do": "color", "piece": rug["id"], "color": "#123456"},
        {"do": "name", "piece": lamp["id"], "name": "小太阳"},
    ]})
    assert not result["refused"], result
    w = 1 if desk["turn"] in (90, 270) else 3
    assert desk["at"][0] + w == 10 and desk["turn"] == 90  # against the right wall, facing into the room
    assert rug["color"] == "#123456" and lamp["name"] == "小太阳"
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "remove", "piece": rug["id"]}, {"do": "room_name", "name": "代码洞"},
        {"do": "room_color", "part": "walls", "color": "#ddeeff"}]})
    assert not result["refused"], result
    assert rug["id"] not in {p["id"] for p in home.pieces}
    assert home.room("bed-codex")["name"] == "代码洞" and home.room("bed-codex")["walls"] == {"color": "#ddeeff"}


def test_what_she_may_not_do(tmp_path):
    home = house(tmp_path)
    bed = next(p for p in in_room(home) if p["type"] == "base/bed")
    sofa = next(p for p in home.pieces if p["room"] == "living" and p["type"] == "base/sofa-3")
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "remove", "piece": bed["id"]}, {"do": "move", "piece": sofa["id"], "where": {"against": "left"}},
        {"do": "name", "piece": bed["id"], "name": "一张非常非常大的床"}, {"do": "add", "kind": "base/beanbag"}]})
    assert result["done"] == []
    assert any("床不能扔" in r for r in result["refused"]) and any("不在你的卧室里" in r for r in result["refused"])
    assert any("最长 8 个字" in r for r in result["refused"]) and any("一次最多改 3 件" in r for r in result["refused"])
    assert redecorate.left_this_week(home, "codex") == 3  # nothing done: not counted
    with pytest.raises(PlacementError, match="没有这种东西"):
        redecorate._one(home, "codex", {"do": "add", "kind": "base/stairs"})


def test_three_times_a_week_and_sixteen_things_in_a_bedroom(tmp_path):
    home = house(tmp_path)
    for i in range(3):
        redecorate.apply(home, "codex", {"changes": [{"do": "add", "kind": "base/plant"}]})
    assert "这次只能许愿" in redecorate.menu(home, "codex")  # no more changes this week, but she may still wish
    late = redecorate.apply(home, "codex", {"changes": [{"do": "add", "kind": "base/plant"}]})
    assert late["done"] == [] and "这周已经改过 3 次" in late["refused"][0]
    home.redecorations["codex"] = []
    while len(in_room(home)) < redecorate.BEDROOM_MAX:
        home.pieces.append({**in_room(home)[-1], "id": f"p-{len(home.pieces)}"})
    with pytest.raises(PlacementError, match="已经有 16 件"):
        redecorate._one(home, "codex", {"do": "add", "kind": "base/plant"})


async def test_a_day_of_hers_can_be_spent_changing_her_room(tmp_path):
    home = house(tmp_path)
    store = LifeStore(tmp_path)
    script = Script({"action": "home", "reason": "房间太空了"},
                    {"changes": [{"do": "add", "kind": "base/floor-lamp", "where": {"against": "back"}}], "why": "晚上看书"})
    engine = LifeEngine(store, script.ask, script.share, lambda pid: "", {"codex": "Codex"},
                        home_menu=lambda pid: redecorate.menu(home, pid),
                        home_apply=lambda pid, plan: "；".join(redecorate.apply(home, pid, plan)["done"]))
    assert await engine.step("codex", [], False) == "home"
    assert "home：动动自己的卧室" in script.calls[0][1] and "[改改家里]" in script.calls[1][1]
    assert sum(1 for p in in_room(home) if p["type"] == "base/floor-lamp") == 2
    assert store.events[-1]["action"] == "home" and "落地灯" in store.events[-1]["note"]


def test_in_a_shared_room_she_may_move_turn_and_rename_a_few_times_a_week(tmp_path):
    home = house(tmp_path)
    sofa = next(p for p in home.pieces if p["room"] == "fun" and p["type"] == "base/sofa-3")
    table = next(p for p in home.pieces if p["room"] == "living" and p["type"] == "base/coffee-table")
    result = redecorate.apply(home, "codex", {"changes": [
        {"do": "move", "room": "fun", "piece": sofa["id"], "where": {"against": "front"}},
        {"do": "room_name", "room": "fun", "name": "牌桌"},
        {"do": "color", "room": "fun", "piece": sofa["id"], "color": "#000000"}]})
    assert len(result["done"]) == 2 and "要许愿" in result["refused"][0]
    assert home.room("fun")["name"] == "牌桌" and sofa["turn"] == 180  # against the front wall, facing into the room
    assert sofa["at"][1] + 2 == home.room("fun")["size"][1]
    assert "Codex把娱乐室改名叫「牌桌」" in home.news("grok", MEMBERS)
    redecorate.apply(home, "grok", {"changes": [{"do": "turn", "room": "fun", "piece": sofa["id"], "face": "back"}]})
    later = redecorate.apply(home, "grok", {"changes": [
        {"do": "move", "room": "fun", "piece": sofa["id"], "where": {"against": "back"}},
        {"do": "room_name", "room": "fun", "name": "影院"},
        {"do": "move", "room": "living", "piece": table["id"], "where": {"against": "front"}}]})
    assert "挪过 2 次" in later["refused"][0] and "改过名" in later["refused"][1] and len(later["done"]) == 1
    with pytest.raises(PlacementError, match="别人的卧室"):
        redecorate._one(home, "codex", {"do": "room_name", "room": "bed-grok", "name": "抢来的"})
    assert "公共房间" in redecorate.menu(home, "codex") and "「牌桌」（ID fun）：这周还能挪 0 次、改名 0 次" in redecorate.menu(home, "codex")


def test_the_owner_undoes_a_change_and_whoever_made_it_hears(tmp_path):
    home = house(tmp_path)
    desk = next(p for p in in_room(home) if p["type"] == "base/writing-desk")
    where = (list(desk["at"]), desk["turn"])
    redecorate.apply(home, "codex", {"changes": [
        {"do": "move", "piece": desk["id"], "where": {"against": "right"}},
        {"do": "add", "kind": "base/plant"},
        {"do": "room_name", "name": "代码洞"}]})
    redecorate.apply(home, "codex", {"changes": [{"do": "color", "piece": desk["id"], "color": "#101010"}]})
    move, plant, rename, colour = home.changes
    assert "后来又被改过" in redecorate.undoable(home, move)  # the colour came after: that first
    redecorate.undo(home, colour["id"], MEMBERS)
    redecorate.undo(home, move["id"], MEMBERS)
    assert (desk["at"], desk["turn"], desk["color"]) == (where[0], where[1], None)
    redecorate.undo(home, plant["id"], MEMBERS)
    assert not any(p["type"] == "base/plant" for p in in_room(home))
    redecorate.undo(home, rename["id"], MEMBERS)
    assert home.room("bed-codex")["name"] == "Codex的卧室"
    with pytest.raises(PlacementError, match="撤回过"):
        redecorate.undo(home, rename["id"], MEMBERS)
    news = home.news("codex", MEMBERS)
    assert news.count("主人撤回了Codex的一处改动") == 4
    assert [c["why_not"] for c in redecorate.view(home)][:1] == ["这条不能撤回"]  # an undo is not undone


async def test_the_page_lists_changes_and_undoes_one(admin):  # noqa: F811
    await login(admin)
    runtime = admin.runtime
    runtime.home.ensure({"codex": "Codex"})
    redecorate.apply(runtime.home, "codex", {"changes": [{"do": "add", "kind": "base/plant"}]})
    view = await (await admin.get("/api/home")).json()
    entry = view["changes"][0]
    assert "加了盆栽" in entry["text"] and entry["why_not"] == ""
    undone = await admin.post(f"/api/home/changes/{entry['id']}/undo", headers={"Origin": admin.origin})
    assert undone.status == 200 and (await undone.json())["changes"][1]["undone"]
    again = await admin.post(f"/api/home/changes/{entry['id']}/undo", headers={"Origin": admin.origin})
    assert again.status == 400
