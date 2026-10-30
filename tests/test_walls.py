import json

import pytest

from nookling.furniture import PlacementError, check_wall
from nookling.home import Home
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex", "grok": "Grok"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def hung(home, tid="base/frame"):
    return sorted((p for p in home.pieces if p["type"] == tid), key=lambda p: (p["wall"], p["along"]))


def test_a_new_house_hangs_its_pictures(tmp_path):
    home = house(tmp_path)
    assert [(p["room"], p["wall"], p["along"], p["y"]) for p in hung(home)] == [
        ("living", "back", 5, 1.0), ("living", "back", 8, 1.0), ("living", "back", 11, 1.0)]
    assert all("at" not in p and "turn" not in p for p in hung(home))


def test_pictures_that_stood_against_a_wall_are_hung_on_it_once(tmp_path):
    house(tmp_path)
    raw = json.loads((tmp_path / "home.json").read_text(encoding="utf-8"))
    frames = [p for p in raw["pieces"] if p["type"] == "base/frame"]
    for p in frames:  # as they were before: standing on the floor against the back wall
        along = p.pop("along")
        del p["wall"], p["y"]
        p.update(at=[along, 0], turn=0)
    frames[0].update(at=[17, 4], turn=90)  # this one stood against the right wall
    raw["extras"].remove("walls")
    raw["version"] = 3
    (tmp_path / "home.json").write_text(json.dumps(raw), encoding="utf-8")
    home = house(tmp_path)
    after = {p["id"]: (p["wall"], p["along"], p["y"]) for p in hung(home)}
    assert after[frames[0]["id"]] == ("right", 4, 1.0)
    assert sorted(a for w, a, _ in after.values() if w == "back") == [8, 11]
    assert {p["id"]: (p["wall"], p["along"], p["y"]) for p in hung(house(tmp_path))} == after  # only once


def test_what_hangs_keeps_clear_of_doors_and_of_each_other(tmp_path):
    home = house(tmp_path)
    room, types = home.checked_room("living"), home.all_types()
    frame = {"id": "", "type": "base/frame", "room": "living", "wall": "back", "along": 2, "y": 1.0}
    with pytest.raises(PlacementError, match="挡住门"):
        check_wall(frame, room, types, home.pieces)  # the doorway to the hallway is cells 2-4
    check_wall({**frame, "type": "base/clock", "y": 2.1}, room, types, home.pieces)  # above the doorway is fine
    with pytest.raises(PlacementError, match="叠在一起"):
        check_wall({**frame, "along": 6}, room, types, home.pieces)
    check_wall({**frame, "along": 6, "y": 1.7}, room, types, home.pieces)  # just above it is fine
    with pytest.raises(PlacementError, match="2.6 米高"):
        check_wall({**frame, "along": 15, "y": 2.3}, room, types, home.pieces)
    with pytest.raises(PlacementError, match="超出"):
        check_wall({**frame, "along": 17}, room, types, home.pieces)
    with pytest.raises(PlacementError, match="不能挂在墙上"):
        check_wall({**frame, "type": "base/sofa-3", "along": 15}, room, types, home.pieces)
    hall = next(r for r in home.rooms if r.get("kind") == "hall")
    with pytest.raises(PlacementError, match="走廊没有墙"):
        check_wall({**frame, "room": hall["id"]}, home.checked_room(hall["id"]), types, home.pieces)


def test_wall_things_go_on_a_wall_and_only_there(tmp_path):
    home = house(tmp_path)
    clock = home.put("base/clock", "study")
    assert (clock["wall"], clock["along"], clock["y"]) == ("back", 0, 1.8) and "at" not in clock
    with pytest.raises(PlacementError, match="要挂在墙上"):
        home.place({"type": "base/frame", "room": "study", "at": [0, 5]})
    with pytest.raises(PlacementError, match="不用转"):
        home.edit_piece(clock["id"], {"turn": 90})
    with pytest.raises(PlacementError, match="只能挂在墙上"):
        home.edit_piece(clock["id"], {"at": [4, 5]})
    moved = home.edit_piece(clock["id"], {"wall": "left", "along": 3, "y": 1.55})
    assert (moved["wall"], moved["along"], moved["y"]) == ("left", 3, 1.6)
    assert home.put("base/wall-shelf", "study")["wall"] == "back"


def test_a_tv_can_hang_or_stand(tmp_path):
    home = house(tmp_path)
    tv = next(p for p in home.pieces if p["type"] == "base/tv")
    up = home.edit_piece(tv["id"], {"wall": "back", "along": 6, "y": 0.6})
    assert up["wall"] == "back" and "at" not in up and "turn" not in up
    home.place({"type": "base/beanbag", "room": "fun", "at": [6, 0]})  # the floor where it stood is free
    down = home.edit_piece(tv["id"], {"at": [12, 0], "turn": 0})
    assert down["at"] == [12, 0] and "wall" not in down


def test_a_room_changing_shape_keeps_what_hangs_where_it_was(tmp_path):
    home = house(tmp_path)
    room = home.add_room("画室", [10, 8], at=[40, 25])
    clock = home.place({"type": "base/clock", "room": room["id"], "wall": "back", "along": 4, "y": 1.5})
    home.reshape_room(room["id"], at=[42, 25], size=[8, 8])  # the left edge moved in: it stays put in the house
    assert home.piece(clock["id"])["along"] == 2
    home.reshape_room(room["id"], at=[44, 25])  # the whole room moved: it goes along
    assert home.piece(clock["id"])["along"] == 2
    with pytest.raises(PlacementError, match="挂钟会放不下"):
        home.reshape_room(room["id"], size=[3, 8])  # the wall would be too short for it


async def test_the_page_can_hang_things(admin):  # noqa: F811
    await login(admin)
    h = {"Origin": admin.origin}
    ok = await admin.post("/api/home/pieces", json={"type": "base/clock", "room": "living", "wall": "back", "along": 14, "y": 1.9}, headers=h)
    assert ok.status == 200 and (await ok.json())["wall"] == "back"
    door = await admin.post("/api/home/pieces", json={"type": "base/clock", "room": "living", "wall": "back", "along": 2, "y": 1.0}, headers=h)
    assert door.status == 400 and "挡住门" in (await door.json())["error"]


def test_a_hung_tv_is_still_the_one_to_watch(tmp_path):
    home = house(tmp_path)
    tv = next(p for p in home.pieces if p["type"] == "base/tv")
    home.edit_piece(tv["id"], {"wall": "back", "along": 6, "y": 0.6})
    home.where["grok"].room = "fun"
    home.begin("grok", "watch")
    assert home.where["grok"].piece == tv["id"]
