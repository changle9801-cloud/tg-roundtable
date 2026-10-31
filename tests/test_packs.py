import io
import json
import zipfile

import pytest
from test_furniture_manager import GOOD, glb

from nookling import models, packs
from nookling.furniture import PlacementError
from nookling.home import Home
from test_web import admin, login  # noqa: F401  (the admin fixture)

MEMBERS = {"codex": "Codex"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def house(path):
    home = Home(path)
    home.ensure(MEMBERS)
    return home


def made_kinds(home, data_dir):
    model = models.store(data_dir, glb(GOOD))
    lamp = home.save_type({"template": "floor-lamp", "name": "月亮灯", "color": "#ffeeaa", "functions": ["video.watch"],
                           "model": {"file": model["file"], "turn": 90, "scale": 1.5}})
    shelf = home.save_type({"template": "wall-shelf", "name": "小搁板", "mount": "wall"})
    return lamp, shelf


def zipped(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, body in files.items():
            z.writestr(name, body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False))
    return out.getvalue()


def test_furniture_goes_out_in_a_pack_and_comes_back_as_copies(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mine = house(a)
    lamp, shelf = made_kinds(mine, a)
    data = packs.export_furniture(mine, a, [lamp["id"], shelf["id"]],
                                  {"name": "月亮灯一套", "author": "小林", "license": "CC-BY-4.0", "description": "晚上看书用"}, PNG)
    manifest, files = packs.open_pack(data)
    assert manifest["name"] == "月亮灯一套" and manifest["needs"] == ["video.watch"] and manifest["preview"] == "preview.png"
    assert [k["name"] for k in manifest["furniture"]] == ["月亮灯", "小搁板"]
    theirs = house(b)
    entry = packs.install(theirs, b, manifest, files)
    got = [theirs.types[t] for t in entry["types"]]
    assert [g["name"] for g in got] == ["月亮灯", "小搁板"] and got[1]["mount"] == "wall"
    assert got[0]["model"]["turn"] == 90 and models.path_of(b, got[0]["model"]["file"]) is not None
    assert got[0]["functions"] == ["video.watch"]  # the function name only: connecting a tool is up to the owner
    again = packs.install(theirs, b, *packs.open_pack(data))  # a second copy, not a replacement
    assert theirs.types[again["types"][0]]["name"] == "月亮灯（2）"
    assert [p["name"] for p in packs.installed(b)] == ["月亮灯一套", "月亮灯一套"]
    theirs.place({"type": entry["types"][0], "room": "living", "at": [3, 8]})
    gone = packs.uninstall(theirs, b, entry["uid"])
    assert gone == {"removed": ["小搁板"], "kept": ["月亮灯"]}  # one is in use: it stays
    assert len(packs.installed(b)) == 1


def test_a_pack_with_anything_wrong_is_refused_whole(tmp_path):
    home = house(tmp_path)
    lamp, _ = made_kinds(home, tmp_path)
    good = packs.export_furniture(home, tmp_path, [lamp["id"]], {"license": "CC0-1.0"})
    _, files = packs.open_pack(good)
    manifest = json.loads(files["manifest.json"])
    bad = {
        "不该有的文件": {**files, "run.js": b"alert(1)"},
        "不该有的文件：../": {**files, "../evil.png": PNG},
        "没有 manifest": {k: v for k, v in files.items() if k != "manifest.json"},
        "许可证": {**files, "manifest.json": {**manifest, "license": "WTFPL"}},
        "模型不在包里": {k: v for k, v in files.items() if not k.startswith("models/")},
        "没用到的文件": {**files, "textures/extra.png": PNG},
        "PNG、JPG、WebP": {**files, "textures/fake.png": b"<svg onload=x>"},
        "不是 .glb": {**{k: v for k, v in files.items()}, next(k for k in files if k.endswith(".glb")): b"not a model"},
        "格式版本": {**files, "manifest.json": {**manifest, "format": 9}},
        "现在还不能装": {**files, "manifest.json": {**manifest, "kind": "plugin"}},
    }
    for why, content in bad.items():
        with pytest.raises(PlacementError, match=why.split("：")[0]):
            packs.open_pack(zipped(content))
    with pytest.raises(PlacementError, match="不是 .nookpack"):
        packs.open_pack(b"just text")


async def test_the_page_previews_installs_exports_and_uninstalls(admin):  # noqa: F811
    await login(admin)
    h = {"Origin": admin.origin}
    runtime = admin.runtime
    runtime.home.ensure(MEMBERS)
    lamp, _ = made_kinds(runtime.home, admin.data_dir)
    out = await admin.post("/api/packs/export", json={"types": [lamp["id"]], "name": "月亮灯", "license": "CC-BY-4.0"}, headers=h)
    assert out.status == 200 and out.headers["Content-Type"] == "application/zip"
    data = await out.read()
    seen = await (await admin.post("/api/packs/preview", data=data, headers=h)).json()
    assert seen["name"] == "月亮灯" and seen["license_name"].startswith("CC BY") and seen["missing"] == ["video.watch"]
    done = await (await admin.post("/api/packs/install", json={"token": seen["token"]}, headers=h)).json()
    assert done["pack"]["name"] == "月亮灯" and any(m["name"] == "月亮灯（2）" for m in done["mine"])
    stale = await admin.post("/api/packs/install", json={"token": seen["token"]}, headers=h)
    assert stale.status == 400  # a staged pack installs once
    off = await (await admin.delete(f"/api/packs/{done['pack']['uid']}", headers=h)).json()
    assert off["removed"] == ["月亮灯（2）"] and off["installed"] == []
    refused = await admin.post("/api/packs/preview", data=b"not a zip", headers=h)
    assert refused.status == 400


def dressed(home, data_dir):
    """A bedroom with a wallpaper, a framed picture and one of the owner's own kinds (with a model)."""
    paper = models.store_image(data_dir, PNG + b"paper")["file"]
    photo = models.store_image(data_dir, PNG + b"photo")["file"]
    home.dress_room("bed-codex", {"walls": {"image": paper, "tile": 2}})
    lamp, _ = made_kinds(home, data_dir)
    home.place({"type": lamp["id"], "room": "bed-codex", "at": [0, 8]})
    home.place({"type": "base/frame", "room": "bed-codex", "wall": "left", "along": 2, "y": 1.2, "image": photo, "name": "全家福"})
    return paper, photo


def test_pictures_go_out_in_a_pack_and_land_in_the_library(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mine = house(a)
    paper, photo = dressed(mine, a)
    found = {p["file"]: p for p in packs.pictures(mine)}
    assert found[paper]["use"] == "walls" and found[paper]["tile"] == 2 and found[photo]["use"] == "picture"
    data = packs.export_surfaces(mine, a, [paper, {"file": photo, "name": "合影"}], {"license": "CC0-1.0"})
    manifest, files = packs.open_pack(data)
    assert [s["name"] for s in manifest["surfaces"]] == ["Codex的卧室的墙", "合影"] and manifest["preview"].startswith("preview.")
    seen = packs.preview(manifest, files, set())
    assert seen["surfaces"][0]["picture"].startswith("data:image/png") and seen["surfaces"][0]["use"] == "墙纸"
    theirs = house(b)
    entry = packs.install(theirs, b, manifest, files)
    assert [p["name"] for p in theirs.pictures] == ["Codex的卧室的墙", "合影"] and len(entry["pictures"]) == 2
    assert models.image_path(b, theirs.pictures[0]["file"]) is not None
    theirs.dress_room("living", {"floor": {"image": theirs.pictures[0]["file"]}})
    gone = packs.uninstall(theirs, b, entry["uid"])
    assert gone == {"removed": ["合影"], "kept": ["Codex的卧室的墙"]}


def test_a_furnished_room_travels_whole(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    mine = house(a)
    dressed(mine, a)
    before = [p for p in mine.pieces if p["room"] == "bed-codex"]
    data = packs.export_room(mine, a, "bed-codex", {"name": "Codex 的房间", "license": "CC-BY-4.0"})
    manifest, files = packs.open_pack(data)
    assert manifest["room"]["name"] == "Codex的卧室" and len(manifest["room"]["pieces"]) == len(before)
    assert [k["name"] for k in manifest["furniture"]] == ["月亮灯"] and manifest["needs"] == ["video.watch"]
    theirs = house(b)
    entry = packs.install(theirs, b, manifest, files)
    rid = entry["rooms"][0]
    room = theirs.room(rid)
    assert room["size"] == [10, 10] and room["walls"]["tile"] == 2 and models.image_path(b, room["walls"]["image"]) is not None
    assert rid not in theirs.shut_rooms() and entry["skipped"] == []  # next to a hallway, with a door; all in place
    placed = [p for p in theirs.pieces if p["room"] == rid]
    assert sorted(p["type"] for p in placed if p["type"].startswith("base/")) == sorted(p["type"] for p in before if p["type"].startswith("base/"))
    lamp = next(p for p in placed if p["type"].startswith("mine/"))
    assert theirs.types[lamp["type"]]["name"] == "月亮灯" and lamp["at"] == [0, 8]
    frame = next(p for p in placed if p["type"] == "base/frame")
    assert frame["name"] == "全家福" and frame["wall"] == "left" and models.image_path(b, frame["image"]) is not None
    packs.uninstall(theirs, b, entry["uid"])
    assert rid in {r["id"] for r in theirs.rooms}  # the room is the owner's now
    mine.rename_room("bed-codex", "Codex")
    mine.room("bed-codex")["name"] = "Claude的卧室"  # as a bedroom is named: longer than a name typed in
    again = packs.install(theirs, b, *packs.open_pack(packs.export_room(mine, a, "bed-codex", {"license": "CC0-1.0"})))
    assert theirs.room(again["rooms"][0])["name"] == "Claude的卧"  # cut to fit


def test_a_room_pack_that_does_not_check_out(tmp_path):
    home = house(tmp_path)
    good = packs.export_room(home, tmp_path, "study", {"license": "CC0-1.0"})
    _, files = packs.open_pack(good)
    manifest = json.loads(files["manifest.json"])
    room = manifest["room"]
    for why, change in {"不认识": {"type": "base/rocket"}, "不在包里": {"type": "pack/7"}, "放在哪": {"at": [1]},
                        "不认识：base/stairs": {"type": "base/stairs"}}.items():
        piece = {**room["pieces"][0], **change}
        bad = {**manifest, "room": {**room, "pieces": [piece]}}
        with pytest.raises(PlacementError, match=why.split("：")[0]):
            packs.open_pack(zipped({**files, "manifest.json": bad}))
    with pytest.raises(PlacementError, match="每边"):
        packs.open_pack(zipped({**files, "manifest.json": {**manifest, "room": {**room, "size": [2, 200]}}}))


async def test_the_page_exports_a_room(admin):  # noqa: F811
    await login(admin)
    admin.runtime.home.ensure(MEMBERS)
    out = await admin.post("/api/packs/export", json={"kind": "room", "room": "study", "license": "CC-BY-4.0"},
                           headers={"Origin": admin.origin})
    assert out.status == 200
    seen = await (await admin.post("/api/packs/preview", data=await out.read(), headers={"Origin": admin.origin})).json()
    assert seen["kind"] == "room" and seen["room"]["name"] == "书房" and seen["room"]["pieces"] == 5
    view = await (await admin.get("/api/packs")).json()
    assert any(r["id"] == "study" for r in view["rooms"]) and view["uses"]["walls"] == "墙纸"


def test_a_piece_whose_place_is_taken_goes_by_the_same_wall(tmp_path):
    home = house(tmp_path)
    room = home.checked_room("study")
    desk = {"type": "base/writing-desk", "room": "study", "at": [7, 11], "turn": 180, "functions": [], "color": None, "name": None}
    with pytest.raises(PlacementError, match="挡住门"):
        home.place(dict(desk))  # right in front of the study's door
    moved = packs._nearby(home, dict(desk), room, home.all_types())
    assert moved["at"][1] == 11 and moved["turn"] == 180  # still against the front wall, facing in
    home.place(moved)


CARD = {"id": "lin", "name": "小林", "kind": "api", "provider": "openai", "model": "gpt-x", "bot_token": "123:secret",
        "api_key": "sk-secret", "command": ["C:/tools/ai.exe"], "aliases": ["林林"], "tagline": "爱吐槽的室友",
        "persona": "住在大阪，喜欢修东西。", "style": "短句，偶尔用颜文字", "relationships": "", "examples": "又坏了？我看看",
        "activity": "high", "appearance": "短发、圆眼镜", "sleep": "01:00-09:00", "busy": "10:00-18:00", "busy_doing": "上班",
        "browse_topics": "修理视频", "enabled": True}


def test_a_member_goes_out_as_her_role_card_only():
    data = packs.export_member(CARD, {"color": "#AA5500", "accessory": "hat"}, {"license": "CC-BY-4.0"})
    manifest, files = packs.open_pack(data)
    text = files["manifest.json"].decode("utf-8")
    for secret in ("123:secret", "sk-secret", "ai.exe", "gpt-x", "openai", '"kind": "api"'):
        assert secret not in text  # no keys, no account, no AI choice, no command
    assert manifest["member"]["name"] == "小林" and manifest["member"]["aliases"] == ["林林"] and manifest["member"]["activity"] == "high"
    assert manifest["look"] == {"color": "#aa5500", "accessory": "hat"}
    seen = packs.preview(manifest, files, set())
    assert seen["member"]["name"] == "小林" and seen["member"]["look"]["accessory"] == "hat"
    made = packs.member_settings(manifest, {"lin"}, {"小林"}, {"kind": "claude_cli", "model": "sonnet"})
    assert made["name"] == "小林2" and made["enabled"] is False and made["kind"] == "claude_cli" and made["id"].startswith("m-")
    assert "bot_token" not in made and "api_key" not in made
    with pytest.raises(PlacementError, match="哪种 AI"):
        packs.member_settings(manifest, set(), set(), {"kind": "rocket"})


def test_a_member_pack_that_does_not_check_out():
    _, files = packs.open_pack(packs.export_member(CARD, None, {"license": "CC0-1.0"}))
    manifest = json.loads(files["manifest.json"])
    for why, change in {"activity": {"member": {**manifest["member"], "activity": "frantic"}},
                        "太长": {"member": {**manifest["member"], "persona": "长" * 5000}},
                        "配饰": {"look": {"color": "#112233", "accessory": "crown-of-thorns"}},
                        "缺少": {"member": {**manifest["member"], "name": ""}},
                        "忙碌时间格式": {"member": {**manifest["member"], "busy": "周一到周五 10-18"}}}.items():
        with pytest.raises(PlacementError, match=why):
            packs.open_pack(zipped({"manifest.json": {**manifest, **change}}))


async def test_the_page_installs_a_member_disabled(admin):  # noqa: F811
    await login(admin)
    h = {"Origin": admin.origin}
    data = packs.export_member(CARD, {"color": "#aa5500", "accessory": "hat"}, {"license": "CC-BY-4.0"})
    seen = await (await admin.post("/api/packs/preview", data=data, headers=h)).json()
    assert seen["kind"] == "member" and seen["member"]["tagline"] == "爱吐槽的室友"
    refused = await admin.post("/api/packs/install", json={"token": seen["token"]}, headers=h)
    assert refused.status == 400  # which AI she runs on is the owner's choice
    seen = await (await admin.post("/api/packs/preview", data=data, headers=h)).json()
    done = await admin.post("/api/packs/install", json={"token": seen["token"], "runs_on": {"kind": "claude_cli", "model": "sonnet"}}, headers=h)
    assert done.status == 200
    new = next(p for p in admin.runtime.settings["participants"] if p["name"] == "小林")
    assert not new["enabled"] and new["kind"] == "claude_cli" and new["persona"] == "住在大阪，喜欢修东西。"
    assert admin.runtime.home.looks[new["id"]] == {"color": "#aa5500", "accessory": "hat"}
    assert (await done.json())["pack"]["members"] == [new["id"]]


