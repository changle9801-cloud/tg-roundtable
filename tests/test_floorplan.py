import json

from nookling import floorplan
from nookling.home import Home, bedroom

MEMBERS = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "deepseek": "DeepSeek"}


def house(tmp_path, members=MEMBERS):
    home = Home(tmp_path)
    home.ensure(members)
    return home


def at(home, rid):
    return tuple(home.room(rid)["at"])


def test_the_old_automatic_layout_becomes_positions_that_look_the_same(tmp_path):
    home = house(tmp_path)
    # Bedrooms in a row, a hallway, study | hall | games room, a hallway, kitchen | living | storage.
    assert [at(home, bedroom(m)) for m in MEMBERS] == [(0, 0), (10, 0), (20, 0), (30, 0)]
    assert (at(home, "study"), at(home, "fun")) == ((0, 13), (21, 13))
    assert (at(home, "kitchen"), at(home, "living"), at(home, "storage")) == ((0, 28), (12, 28), (30, 28))
    halls = sorted((tuple(r["at"]), tuple(r["size"])) for r in home.rooms if r.get("kind") == "hall")
    assert halls == [((0, 10), (40, 3)), ((0, 25), (40, 3)), ((18, 13), (3, 12))]
    assert all(r["level"] == 0 for r in home.rooms) and home.floors == [{"level": 0, "name": "一楼"}]
    for door in home.doors:
        assert floorplan.door_ok(door, home.rooms), door
    front, = [d for d in home.doors if d.get("outside")]
    assert floorplan.door_sides(front, home.rooms) == ["living"]
    assert floorplan.unreachable(home.doors, home.rooms) == []
    assert all("doors" not in r for r in home.rooms)


def test_an_existing_house_is_converted_once_and_keeps_everything_else(tmp_path):
    home = house(tmp_path)
    raw = json.loads((tmp_path / "home.json").read_text(encoding="utf-8"))
    # Turn it back into a house from before positions (rooms with their own doors, no hallways).
    old = Home(tmp_path)
    for r in raw["rooms"]:
        r.pop("at"), r.pop("level"), r.pop("kind")
    raw["rooms"] = [r for r in raw["rooms"] if not r["id"].startswith("hall-")]
    from nookling.furniture import BEDROOM_DOORS, SHARED_ROOMS
    doors = {rid: d for rid, _, _, _, d in SHARED_ROOMS}
    for r in raw["rooms"]:
        r["doors"] = [dict(x) for x in (BEDROOM_DOORS if r["id"].startswith("bed-") else doors[r["id"]])]
    raw["rooms"][0]["name"] = "改过名的卧室"
    raw["rooms"][1]["floor"] = {"color": "#123456"}  # a floor look (2e) must survive: storeys are "level"
    raw.pop("doors"), raw.pop("floors")
    (tmp_path / "home.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    pieces_before = raw["pieces"]
    again = house(tmp_path)
    assert again.rooms[0]["name"] == "改过名的卧室" and again.pieces == pieces_before
    assert again.rooms[1]["floor"] == {"color": "#123456"} and again.rooms[1]["level"] == 0
    assert len([r for r in again.rooms if r.get("kind") == "hall"]) == 3
    assert floorplan.unreachable(again.doors, again.rooms) == []
    third = house(tmp_path)
    assert len(third.rooms) == len(again.rooms) and len(third.doors) == len(again.doors)  # not twice
    assert old is not None


def test_a_new_member_gets_a_bedroom_at_the_end_of_the_row_with_a_door(tmp_path):
    home = house(tmp_path)
    home.ensure({**MEMBERS, "xiaolin": "小林"})
    room = home.room(bedroom("xiaolin"))
    assert room["at"] == [40, 0]
    hall = next(r for r in home.rooms if r.get("kind") == "hall" and r["at"] == [0, 10])
    assert hall["size"] == [50, 3]  # the hallway now reaches her door
    assert floorplan.unreachable(home.doors, home.rooms) == []
    assert any(bedroom("xiaolin") in floorplan.door_sides(d, home.rooms) for d in home.doors)


def test_a_new_room_goes_next_to_a_hallway_and_never_walls_up_the_front_door(tmp_path):
    home = house(tmp_path)
    for name in ("阳光房", "书房二", "画室"):
        room = home.add_room(name, "small")
        others = [r for r in home.rooms if r["id"] != room["id"]]
        assert not any(floorplan.overlaps(room, r) for r in others)
    front, = [d for d in home.doors if d.get("outside")]
    assert floorplan.door_sides(front, home.rooms) == ["living"]  # still opens to the outside
    assert floorplan.unreachable(home.doors, home.rooms) == []


def test_deleting_a_room_takes_its_doors_with_it(tmp_path):
    home = house(tmp_path)
    room = home.add_room("阳光房", "small")
    before = len(home.doors)
    home.delete_room(room["id"])
    assert len(home.doors) == before - 1
    assert all(floorplan.door_ok(d, home.rooms) for d in home.doors)


def test_furniture_still_keeps_clear_of_doors(tmp_path):
    home = house(tmp_path)
    doors = home.checked_room(bedroom("codex"))["doors"]
    assert doors == [{"wall": "front", "at": 5.5}]  # squares 4–7: doors sit on whole squares now
    import pytest
    from nookling.furniture import PlacementError
    with pytest.raises(PlacementError, match="挡住门"):
        home.place({"type": "base/beanbag", "room": bedroom("codex"), "at": [4, 8]})


def test_a_room_with_no_way_in_is_left_alone(tmp_path):
    home = house(tmp_path)
    study_doors = [d for d in home.doors if "study" in floorplan.door_sides(d, home.rooms)]
    home.doors = [d for d in home.doors if d not in study_doors]  # the study's only door is gone
    assert home.shut_rooms() == {"study"}
    assert "study" in home.view(MEMBERS)["unreachable"]
    home.begin("codex", "read", "磁吸手柄")  # the computers are all in the study
    assert (home.where["codex"].room, home.where["codex"].piece) == ("living", "")
    home.where["grok"].room = "study"
    home.begin("claude", "private", "聊聊", peer="grok", peer_home=True)
    assert home.where["claude"].verb == "phone"  # she can't get to Grok: a phone call
    home.begin("claude", "idle", "想去书房坐会儿，看看书")
    assert home.where["claude"].room != "study"
    home.doors.extend(study_doors)
    assert home.shut_rooms() == set()
