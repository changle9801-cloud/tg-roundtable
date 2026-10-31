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
