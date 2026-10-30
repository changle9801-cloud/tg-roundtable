import json

import pytest

from nookling import furniture
from nookling.furniture import BUILTIN_TYPES, PlacementError, check, spot
from nookling.home import Home, bedroom

MEMBERS = {"codex": "Codex", "xiaolin": "小林"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def test_the_default_house_is_furnished_and_every_piece_fits(tmp_path):
    home = house(tmp_path)
    types = home.all_types()
    rooms = {r["id"] for r in home.rooms}
    assert {p["room"] for p in home.pieces} == rooms  # nothing left empty
    for piece in home.pieces:
        check(piece, home.room(piece["room"]), types, home.pieces)  # no overlap, no blocked door, inside
    beds = [p for p in home.pieces if p["type"] == "base/bed"]
    assert {p["owner"] for p in beds} == set(MEMBERS)
    tv = next(p for p in home.pieces if p["type"] == "base/tv")
    assert tv["room"] == "fun" and tv["functions"] == ["video.watch", "game.play"]


def test_a_house_from_before_furniture_gets_the_default_set_once(tmp_path):
    (tmp_path / "home.json").write_text(json.dumps({
        "version": 1,
        "rooms": [{"id": "living", "name": "牌桌", "icon": "🛋️", "shared": True},
                  {"id": "bed-codex", "name": "Codex的卧室", "icon": "🛏️", "shared": False, "owner": "codex"}],
        "where": {"codex": {"room": "study", "verb": "read", "since": 1.0}},
        "looks": {"codex": {"color": "#123456", "accessory": "bow"}}, "diary_reads": []}), encoding="utf-8")
    home = house(tmp_path)
    living = home.room("living")
    assert living["name"] == "牌桌" and living["size"] == [18, 12]  # renamed rooms keep their name
    assert home.where["codex"].room == "study" and home.looks["codex"]["accessory"] == "bow"
    count = len(home.pieces)
    assert count > 20
    again = house(tmp_path)
    assert len(again.pieces) == count  # not furnished twice


def test_a_new_member_gets_a_furnished_bedroom(tmp_path):
    home = house(tmp_path)
    before = len(home.pieces)
    home.ensure({**MEMBERS, "grok": "Grok"})
    mine = [p for p in home.pieces if p["room"] == bedroom("grok")]
    assert len(home.pieces) == before + len(mine) and {p["owner"] for p in mine} == {"grok"}
    assert any(p["type"] == "base/writing-desk" for p in mine)  # where her diary lies


def test_pieces_must_fit_the_room_and_leave_doors_and_others_alone(tmp_path):
    home = house(tmp_path)
    room = bedroom("codex")
    with pytest.raises(PlacementError, match="放不下"):
        home.place({"type": "base/sofa-3", "room": room, "at": [7, 4]})  # 6 wide in a 10-wide room
    with pytest.raises(PlacementError, match="叠在一起"):
        home.place({"type": "base/beanbag", "room": room, "at": [1, 1]})  # on the bed
    with pytest.raises(PlacementError, match="挡住门"):
        home.place({"type": "base/beanbag", "room": room, "at": [4, 8]})  # in front of the door
    with pytest.raises(PlacementError, match="朝向"):
        home.place({"type": "base/beanbag", "room": room, "at": [5, 4], "turn": 45})
    with pytest.raises(PlacementError, match="没有这种家具"):
        home.place({"type": "base/rocket", "room": room, "at": [5, 4]})
    rug = home.place({"type": "base/rug-small", "room": room, "at": [1, 1]})  # rugs go under things
    chair = home.place({"type": "base/beanbag", "room": room, "at": [5, 3]})
    assert chair["functions"] == [] and chair["owner"] == ""
    turned = home.move(chair["id"], turn=90, at=[6, 3])
    assert turned["turn"] == 90
    with pytest.raises(PlacementError):
        home.move(chair["id"], at=[1, 1])  # onto the bed again
    home.remove(rug["id"])
    saved = Home(tmp_path)
    assert any(p["id"] == chair["id"] and p["at"] == [6, 3] for p in saved.pieces)
    assert not any(p["id"] == rug["id"] for p in saved.pieces)


def test_a_turned_piece_turns_its_seats_with_it():
    sofa = BUILTIN_TYPES["base/sofa-3"]
    facing = {"id": "p", "type": sofa["id"], "room": "fun", "at": [6, 6], "turn": 0}
    x, z, face = spot(facing, sofa, sofa["uses"][0])
    assert (round(x, 2), round(z, 2), face) == (3.5, 3.45, 0)
    away = {**facing, "turn": 180}
    x, z, face = spot(away, sofa, sofa["uses"][0])
    assert (round(x, 2), round(z, 2), face) == (5.5, 3.55, 180)  # mirrored, looking at the back wall
    shelf = BUILTIN_TYPES["base/bookshelf"]
    assert furniture.turned_size(shelf["size"], 90) == (1, 5)
