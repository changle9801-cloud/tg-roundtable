import json
import struct

import pytest

from nookling import models
from nookling.furniture import PlacementError
from nookling.home import Home, bedroom
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex", "xiaolin": "小林"}


def house(tmp_path):
    home = Home(tmp_path)
    home.ensure(MEMBERS)
    return home


def glb(gltf: dict) -> bytes:
    body = json.dumps(gltf).encode()
    body += b" " * (-len(body) % 4)
    return b"glTF" + struct.pack("<II", 2, 20 + len(body)) + struct.pack("<II", len(body), 0x4E4F534A) + body


GOOD = {"asset": {"version": "2.0"}, "accessors": [{"count": 36}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}], "animations": [{"name": "Idle"}]}


# ---- kinds of furniture the owner makes ----

def test_a_new_kind_gets_its_seats_from_its_template(tmp_path):
    home = house(tmp_path)
    sofa = home.save_type({"template": "sofa", "name": "长沙发", "size": [8, 2]})
    assert sofa["id"].startswith("mine/") and len(sofa["uses"]) == 4 and sofa["blocks"]
    rug = home.save_type({"template": "rug", "size": [4, 4]})
    assert rug["uses"] == [] and not rug["blocks"]
    assert home.save_type({"template": "tv"})["screen"] == {"face": 0}
    with pytest.raises(PlacementError, match="没有这个样子"):
        home.save_type({"template": "rocket"})
    with pytest.raises(PlacementError, match="尺寸"):
        home.save_type({"template": "sofa", "size": [30, 2]})


def test_built_in_kinds_are_copied_not_changed(tmp_path):
    home = house(tmp_path)
    with pytest.raises(PlacementError, match="先复制"):
        home.save_type({"color": "#ff0000"}, "base/sofa-3")
    copy = home.save_type({"copy_of": "base/sofa-3"})
    assert copy["name"] == "三人沙发（复制）" and copy["uses"] == home.all_types()["base/sofa-3"]["uses"]
    red = home.save_type({"color": "#FF0000"}, copy["id"])
    assert red["color"] == "#ff0000" and red["uses"] == copy["uses"]  # same shape: hand-tuned seats kept
    wider = home.save_type({"size": [8, 2]}, copy["id"])
    assert len(wider["uses"]) == 4  # a new shape: seats worked out again
    assert Home(tmp_path).types[copy["id"]]["size"] == [8, 2]  # kept across restarts


def test_a_kind_cannot_grow_out_of_where_its_pieces_stand_or_go_while_in_use(tmp_path):
    home = house(tmp_path)
    stool = home.save_type({"template": "chair", "size": [1, 1]})
    piece = home.place({"type": stool["id"], "room": bedroom("codex"), "at": [8, 3]})
    with pytest.raises(PlacementError, match="放不下"):
        home.save_type({"size": [4, 4]}, stool["id"])  # would cross the wall
    with pytest.raises(PlacementError, match="还摆着"):
        home.delete_type(stool["id"])
    home.remove(piece["id"])
    home.delete_type(stool["id"])
    assert stool["id"] not in home.types


# ---- pieces ----

def test_pieces_go_where_they_fit_and_only_plain_ones_can_be_deleted(tmp_path):
    home = house(tmp_path)
    chair = home.put("base/beanbag", "kitchen")
    assert chair["room"] == "kitchen"
    edited = home.edit_piece(chair["id"], {"color": "#123ABC", "name": "我的豆袋", "turn": 90, "functions": ["music.play"]})
    assert (edited["color"], edited["name"], edited["turn"], edited["functions"]) == ("#123abc", "我的豆袋", 90, ["music.play"])
    with pytest.raises(PlacementError, match="挂着功能"):
        home.remove(chair["id"])
    home.edit_piece(chair["id"], {"functions": []})
    home.remove(chair["id"])
    with pytest.raises(PlacementError, match="功能名"):
        home.edit_piece(home.pieces[0]["id"], {"functions": ["不是功能名"]})
    home.sync_parcels({"music": "听歌"})
    box = next(p for p in home.pieces if p["type"] == "base/parcel")
    with pytest.raises(PlacementError, match="纸箱"):
        home.remove(box["id"])
    with pytest.raises(PlacementError, match="纸箱"):
        home.edit_piece(box["id"], {"functions": ["video.watch"]})


# ---- rooms ----

def test_rooms_can_be_added_renamed_and_deleted_when_empty(tmp_path):
    home = house(tmp_path)
    room = home.add_room("阳光房", "small")
    assert room["size"] == [10, 10] and room["shared"] and room["doors"] == [{"wall": "back"}]
    assert home.rename_room(room["id"], "一个特别长的房间名字")["name"] == "一个特别长的房间"
    with pytest.raises(PlacementError, match="大小"):
        home.add_room("x", "huge")
    with pytest.raises(PlacementError, match="卧室"):
        home.delete_room(bedroom("codex"))
    with pytest.raises(PlacementError, match="大门"):
        home.delete_room("living")
    with pytest.raises(PlacementError, match="还有家具"):
        home.delete_room("storage")
    home.put("base/plant", room["id"])
    with pytest.raises(PlacementError, match="还有家具"):
        home.delete_room(room["id"])
    home.remove(next(p for p in home.pieces if p["room"] == room["id"])["id"])
    home.where["codex"].room = room["id"]
    with pytest.raises(PlacementError, match="有人"):
        home.delete_room(room["id"])
    home.where["codex"].room = "living"
    home.delete_room(room["id"])
    assert all(r["id"] != room["id"] for r in Home(tmp_path).rooms)


def test_with_no_study_left_reading_falls_back_to_the_living_room(tmp_path):
    home = house(tmp_path)
    for p in [p for p in home.pieces if p["room"] == "study"]:
        home.edit_piece(p["id"], {"functions": []})
        home.remove(p["id"])
    for p in [p for p in home.pieces if "web.read" in p["functions"]]:
        home.edit_piece(p["id"], {"functions": []})
    home.delete_room("study")
    home.begin("codex", "read")
    assert home.where["codex"].room == "living"


# ---- models ----

def test_models_are_checked_before_they_are_kept(tmp_path):
    info = models.store(tmp_path, glb(GOOD))
    assert info["file"].endswith(".glb") and info["triangles"] == 12 and info["animations"] == ["Idle"]
    assert models.store(tmp_path, glb(GOOD))["file"] == info["file"]  # the same file once
    assert models.path_of(tmp_path, info["file"]).is_file()
    assert models.path_of(tmp_path, "../settings.json") is None
    for bad, why in ((b"not a model", "不是 .glb"),
                     (glb({**GOOD, "images": [{"uri": "https://example.com/t.png"}]}), "外部"),
                     (glb({**GOOD, "extensionsRequired": ["KHR_draco_mesh_compression"]}), "Draco")):
        with pytest.raises(models.ModelError, match=why):
            models.inspect(bad)


# ---- over HTTP, as the page does it ----

async def test_the_page_can_arrange_the_house(admin):  # noqa: F811
    await login(admin)
    post = lambda path, body: admin.post(path, json=body, headers={"Origin": admin.origin})  # noqa: E731
    put = lambda path, body: admin.put(path, json=body, headers={"Origin": admin.origin})  # noqa: E731
    kind = await (await post("/api/home/types", {"template": "armchair", "name": "摇椅"})).json()
    piece = await (await post("/api/home/pieces", {"type": kind["id"], "room": "living"})).json()
    assert piece["room"] == "living"
    resp = await put(f"/api/home/pieces/{piece['id']}", {"room": "living", "at": [6, 1]})  # onto the sofa
    assert resp.status == 400 and "叠在一起" in (await resp.json())["error"]
    room = await (await post("/api/home/rooms", {"name": "书房二", "size": "medium"})).json()
    assert room["size"] == [14, 12]
    upload = await admin.post("/api/home/models", data=glb(GOOD), headers={"Origin": admin.origin})
    model = await upload.json()
    assert upload.status == 200 and model["triangles"] == 12
    got = await admin.get(f"/api/home/models/{model['file']}")
    assert got.status == 200 and (await got.read())[:4] == b"glTF"
    resp = await put(f"/api/home/types/{kind['id']}", {"model": {"file": model["file"], "turn": 90, "scale": 1.2}})
    assert (await resp.json())["model"] == {"file": model["file"], "turn": 90, "scale": 1.2}
    view = await (await admin.get("/api/home")).json()
    assert kind["id"] in view["types"] and "armchair" in view["templates"] and "parcel" not in view["templates"]


def test_the_page_can_draw_every_template():
    import re
    from pathlib import Path

    from nookling.furniture import TEMPLATES
    js = (Path(__file__).parents[1] / "src" / "nookling" / "web" / "static" / "home.js").read_text(encoding="utf-8")
    block = js[js.index("const TEMPLATES = {"):js.index("const turnedSize")]
    drawn = set(re.findall(r'^\s{2}"?([a-z-]+)"?: \(', block, re.M))
    assert drawn == set(TEMPLATES)
