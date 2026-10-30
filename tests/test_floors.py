import pytest

from nookling.furniture import PlacementError
from nookling.home import Home, bedroom
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex", "grok": "Grok"}
STUDY_STAIRS = {"type": "base/stairs", "room": "study", "at": [6, 4]}  # house cells x 6..8, z 17..22


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def upstairs(home):
    home.add_floor()
    return home.add_room("", [18, 12], at=[0, 13], kind="hall", level=1)  # right above the study


def test_floors_are_added_on_top_and_taken_away_from_the_top(tmp_path):
    home = house(tmp_path)
    assert home.add_floor() == {"level": 1, "name": "二楼"}
    assert home.add_floor("阁楼")["name"] == "阁楼"
    home.rename_floor(1, "卧室层")
    with pytest.raises(PlacementError, match="最上面"):
        home.delete_floor(1)
    home.delete_floor(2)
    for _ in range(3):
        home.add_floor()
    with pytest.raises(PlacementError, match="最多 5 层"):
        home.add_floor()
    assert [f["name"] for f in Home(tmp_path).floors][:2] == ["一楼", "卧室层"]


def test_stairs_need_a_floor_and_a_room_above(tmp_path):
    home = house(tmp_path)
    with pytest.raises(PlacementError, match="先加一层"):
        home.place(dict(STUDY_STAIRS))
    home.add_floor()
    with pytest.raises(PlacementError, match="要有房间或走廊"):
        home.place(dict(STUDY_STAIRS))
    home.add_room("", [18, 12], at=[0, 13], kind="hall", level=1)
    stairs = home.place(dict(STUDY_STAIRS))
    assert stairs["type"] == "base/stairs" and home.stairwells(1) == {(x, z) for x in range(6, 9) for z in range(17, 23)}


def test_nothing_stands_over_a_stairwell_and_the_room_above_cannot_go(tmp_path):
    home = house(tmp_path)
    hall = upstairs(home)
    home.place(dict(STUDY_STAIRS))
    with pytest.raises(PlacementError, match="楼梯口"):
        home.place({"type": "base/beanbag", "room": hall["id"], "at": [6, 5]})
    home.place({"type": "base/beanbag", "room": hall["id"], "at": [12, 5]})  # beside it is fine
    with pytest.raises(PlacementError, match="楼梯上面"):
        home.reshape_room(hall["id"], at=[20, 13])
    with pytest.raises(PlacementError):
        home.delete_room(hall["id"])  # furniture in it, and the stairs need it


def test_stairs_cannot_come_up_under_furniture(tmp_path):
    home = house(tmp_path)
    hall = upstairs(home)
    home.place({"type": "base/beanbag", "room": hall["id"], "at": [6, 5]})
    with pytest.raises(PlacementError, match="摆着东西"):
        home.place(dict(STUDY_STAIRS))


def test_she_uses_things_on_her_own_floor_first(tmp_path):
    home = house(tmp_path)
    hall = upstairs(home)
    assert hall["id"] in home.shut_rooms()  # upstairs with no stairs yet: nobody can get there
    home.place(dict(STUDY_STAIRS))
    room = home.add_room("影音室", [10, 10], at=[18, 13], level=1)  # beside the hallway upstairs
    assert home.shut_rooms() == set()  # up the stairs, through the door the new room got
    tv = home.place({"type": "base/tv", "room": room["id"], "at": [2, 0]})
    home.where["grok"].room = hall["id"]
    home.begin("grok", "watch")
    assert home.where["grok"].piece == tv["id"]  # the one upstairs, not the games room's
    home.where["codex"].room = "living"
    home.begin("codex", "watch")
    assert home.where["codex"].room == "fun"
    bed_room = home.add_room("Grok的新卧室", [10, 10], at=[28, 13], level=1)
    assert bed_room["level"] == 1


async def test_the_page_can_add_floors_and_draw_upstairs(admin):  # noqa: F811
    await login(admin)
    h = {"Origin": admin.origin}
    floor = await (await admin.post("/api/home/floors", json={}, headers=h)).json()
    assert floor == {"level": 1, "name": "二楼"}
    room = await admin.post("/api/home/rooms", json={"name": "", "kind": "hall", "size": [18, 12], "at": [0, 13], "level": 1}, headers=h)
    assert room.status == 200 and (await room.json())["level"] == 1
    renamed = await (await admin.put("/api/home/floors/1", json={"name": "楼上"}, headers=h)).json()
    assert renamed["name"] == "楼上"
    view = await (await admin.get("/api/home")).json()
    assert [f["level"] for f in view["floors"]] == [0, 1]
    refused = await admin.delete("/api/home/floors/1", headers=h)
    assert refused.status == 400  # it has a room


def test_a_new_piece_never_lands_on_a_stairwell(tmp_path):
    home = house(tmp_path)
    home.add_floor()
    landing = home.add_room("楼梯间", [3, 6], at=[6, 17], level=1)  # exactly over the stairs, all stairwell
    home.place(dict(STUDY_STAIRS))
    with pytest.raises(PlacementError, match="放不下了"):
        home.put("base/beanbag", landing["id"])
    assert home.put("base/rug-small", landing["id"])["at"] == [0, 0]  # a rug does not block: it may lie there
