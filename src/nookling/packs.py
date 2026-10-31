"""Packs (docs/packs.md): what the family's home is made of, shared as a file.

A pack is a ``.nookpack`` (a zip) holding only data: ``manifest.json``, an optional preview
picture, ``models/*.glb`` and ``textures/*`` pictures. Nothing in it runs; anything else, too
much of anything, or a manifest that does not check out, and the whole pack is refused.
Installing makes copies (new ids, "（2）" on clashing names); a pack never connects tools.
Three kinds so far: furniture, surface (pictures for floors, walls and frames) and room (a furnished
room, carrying its own kinds and pictures).
"""

from __future__ import annotations

import base64
import io
import json
import re
import time
import uuid
import zipfile
from pathlib import Path

from . import furniture, models
from .furniture import PlacementError
from .home import ROOM_CELLS, TILE_RANGE, Home

FORMAT = 1
PACK_MAX = 60 * 1024 * 1024  # the file
UNPACKED_MAX = 120 * 1024 * 1024  # all of it unpacked: no zip bombs
FILES_MAX = 200
KINDS_MAX = 50  # kinds of furniture in one pack
STAGED_FOR = 1800  # seconds a pack waits between preview and install
LICENSES = {"CC0-1.0": "CC0（随便用）", "CC-BY-4.0": "CC BY 4.0（署名）", "CC-BY-SA-4.0": "CC BY-SA 4.0（署名、相同方式共享）",
            "all-rights-reserved": "保留所有权利"}
PACK_KINDS = ("furniture", "surface", "room")  # member comes with 5c
SURFACES_MAX = 50  # pictures in one surface pack
PIECES_MAX = 60  # pieces in one room
USES = {"floor": "地板", "walls": "墙纸", "picture": "画", "any": "都行"}
NOT_IN_ROOMS = {"base/parcel", "base/stairs"}  # what a room pack does not carry
ENTRY_RE = re.compile(r"^(manifest\.json|preview\.(png|jpg|webp)|models/[A-Za-z0-9_-]{1,48}\.glb|textures/[A-Za-z0-9_-]{1,48}\.(png|jpg|webp))$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def _text(raw, limit: int, what: str, required: bool = False) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise PlacementError(f"{what}要是文字")
    text = " ".join(raw.split()) if limit <= 120 else raw.strip()
    if len(text) > limit:
        raise PlacementError(f"{what}太长了（最多 {limit} 个字）")
    if required and not text:
        raise PlacementError(f"缺少{what}")
    return text


def open_pack(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """Check a pack file through and through; returns its manifest and its files (by path).
    Raises PlacementError saying what is wrong."""
    if len(data) > PACK_MAX:
        raise PlacementError(f"包太大了：{len(data) // (1024 * 1024)} MB，最多 {PACK_MAX // (1024 * 1024)} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PlacementError("不是 .nookpack 文件（应该是一个 zip）") from None
    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > FILES_MAX:
        raise PlacementError(f"包里文件太多了（最多 {FILES_MAX} 个）")
    if sum(i.file_size for i in entries) > UNPACKED_MAX:
        raise PlacementError("包解开以后太大了")
    files: dict[str, bytes] = {}
    for info in entries:
        name = info.filename
        if not ENTRY_RE.match(name):
            raise PlacementError(f"包里有不该有的文件：{name}（只能有 manifest.json、预览图、models/*.glb、textures/ 里的图片）")
        if name in files:
            raise PlacementError(f"包里有两个同名文件：{name}")
        if info.flag_bits & 0x1:
            raise PlacementError("包不能加密")
        limit = models.MAX_BYTES if name.endswith(".glb") else models.MAX_IMAGE_BYTES if name != "manifest.json" else 1024 * 1024
        if info.file_size > limit:
            raise PlacementError(f"{name} 太大了")
        with archive.open(info) as f:
            body = f.read(limit + 1)
        if len(body) > limit or len(body) != info.file_size:
            raise PlacementError(f"{name} 的大小不对")
        try:
            if name.endswith(".glb"):
                models.inspect(body)  # a real glTF binary, nothing it loads from outside
            elif name != "manifest.json":
                models.image_kind(body)
        except models.ModelError as e:
            raise PlacementError(f"{name}：{e}") from None
        files[name] = body
    if "manifest.json" not in files:
        raise PlacementError("包里没有 manifest.json")
    try:
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PlacementError("manifest.json 不是合法的 JSON") from None
    return check_manifest(manifest, files), files


def check_manifest(raw, files: dict[str, bytes]) -> dict:
    """The manifest, each field checked; what a pack says it holds must be in it, and the other way round."""
    if not isinstance(raw, dict):
        raise PlacementError("manifest.json 格式不对")
    if raw.get("format") != FORMAT:
        raise PlacementError(f"不认识这个包的格式版本：{raw.get('format')}")
    if raw.get("kind") not in PACK_KINDS:
        raise PlacementError(f"现在还不能装这种包：{raw.get('kind')}")
    pid = str(raw.get("id") or "")
    if not SLUG_RE.match(pid):
        raise PlacementError("包的 id 只能是小写字母、数字和横线")
    license_ = str(raw.get("license") or "")
    if license_ not in LICENSES:
        raise PlacementError(f"不认识这个许可证：{license_}")
    needs = raw.get("needs") or []
    if not isinstance(needs, list) or not all(isinstance(f, str) and furniture.FUNCTION_RE.match(f) for f in needs):
        raise PlacementError("needs 里要写功能名，比如 video.watch")
    manifest = {"format": FORMAT, "kind": raw["kind"], "id": pid, "name": _text(raw.get("name"), 40, "包的名字", True),
                "author": _text(raw.get("author"), 40, "作者"), "version": _text(raw.get("version") or "1", 20, "版本", True),
                "license": license_, "description": _text(raw.get("description"), 500, "说明"), "needs": list(dict.fromkeys(needs)),
                "preview": None, "furniture": [], "surfaces": [], "room": None}
    preview = raw.get("preview")
    if preview is not None:
        if preview not in files or not preview.startswith("preview."):
            raise PlacementError("预览图不在包里")
        manifest["preview"] = preview
    kinds = raw.get("furniture") if raw["kind"] == "furniture" else raw.get("furniture") or []
    if not isinstance(kinds, list) or len(kinds) > KINDS_MAX or (raw["kind"] == "furniture" and not kinds):
        raise PlacementError(f"家具包里要有 1 到 {KINDS_MAX} 种家具")
    used = {"manifest.json", manifest["preview"]}

    def picture(path, what: str) -> str:
        if not isinstance(path, str) or not path.startswith("textures/") or path not in files:
            raise PlacementError(f"{what}的图片不在包里")
        used.add(path)
        return path

    def tile(value) -> float:
        try:
            return round(min(TILE_RANGE[1], max(TILE_RANGE[0], float(value if value is not None else 1))), 2)
        except (TypeError, ValueError):
            raise PlacementError("图案大小写数字") from None

    def look(raw_look, what: str):
        if raw_look is None:
            return None
        if not isinstance(raw_look, dict):
            raise PlacementError(f"{what}格式不对")
        if raw_look.get("image") is not None:
            return {"image": picture(raw_look["image"], what), "tile": tile(raw_look.get("tile"))}
        color = str(raw_look.get("color") or "")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise PlacementError(f"{what}的颜色要写成 #rrggbb")
        return {"color": color.lower()}

    for i, kind in enumerate(kinds):
        if not isinstance(kind, dict):
            raise PlacementError(f"第 {i + 1} 种家具格式不对")
        model = kind.get("model")
        if model is not None:
            if not isinstance(model, dict) or model.get("file") not in files or not str(model.get("file")).startswith("models/"):
                raise PlacementError(f"第 {i + 1} 种家具的模型不在包里")
            used.add(model["file"])
        # the same checks as a kind made on the page; a placeholder name stands in for the model until it is stored
        checked = furniture.make_type({**kind, "model": {**model, "file": "0" * 16 + ".glb"} if model else None}, f"pack/{i}")
        checked["model"] = {**checked["model"], "file": model["file"]} if model else None
        manifest["furniture"].append(checked)
    if raw["kind"] == "surface":
        surfaces = raw.get("surfaces")
        if not isinstance(surfaces, list) or not 1 <= len(surfaces) <= SURFACES_MAX:
            raise PlacementError(f"图片包里要有 1 到 {SURFACES_MAX} 张图")
        for i, s in enumerate(surfaces):
            if not isinstance(s, dict) or s.get("use", "any") not in USES:
                raise PlacementError(f"第 {i + 1} 张图格式不对（use 只能是 floor、walls、picture、any）")
            manifest["surfaces"].append({"name": _text(s.get("name"), 16, "图的名字") or f"图 {i + 1}",
                                         "image": picture(s.get("image"), f"第 {i + 1} 张图"), "use": s.get("use", "any"),
                                         "tile": tile(s.get("tile"))})
    if raw["kind"] == "room":
        manifest["room"] = _check_room(raw.get("room"), len(manifest["furniture"]), picture, look)
    extra = set(files) - used
    if extra:
        raise PlacementError(f"包里有没用到的文件：{', '.join(sorted(extra))}")
    return manifest


def _check_room(room, kinds: int, picture, look) -> dict:
    """A room pack's room: its name, size, floor and walls, and each piece (a built-in kind by its id, or one of
    the pack's own as pack/<n>), with where it stands or hangs. Whether they fit is seen when installing."""
    if not isinstance(room, dict):
        raise PlacementError("房间包里要有 room")
    try:
        size = [int(v) for v in room.get("size") or ()]
    except (TypeError, ValueError):
        size = []
    if len(size) != 2 or not all(ROOM_CELLS[0] <= v <= ROOM_CELLS[1] for v in size):
        raise PlacementError(f"房间每边 {ROOM_CELLS[0]} 到 {ROOM_CELLS[1]} 格")
    kind = room.get("kind", "room")
    if kind not in ("room", "hall"):
        raise PlacementError("房间的种类只能是 room 或 hall")
    pieces = room.get("pieces") or []
    if not isinstance(pieces, list) or len(pieces) > PIECES_MAX:
        raise PlacementError(f"一间房最多 {PIECES_MAX} 件家具")
    out = []
    for i, p in enumerate(pieces):
        what = f"第 {i + 1} 件家具"
        if not isinstance(p, dict):
            raise PlacementError(f"{what}格式不对")
        tid = str(p.get("type") or "")
        if tid.startswith("pack/"):
            if not tid[5:].isdigit() or int(tid[5:]) >= kinds:
                raise PlacementError(f"{what}用的家具不在包里")
        elif tid not in furniture.BUILTIN_TYPES or tid in NOT_IN_ROOMS:
            raise PlacementError(f"{what}用的家具不认识：{tid}")
        piece = {"type": tid, "color": None, "name": _text(p.get("name"), 16, f"{what}的名字") or None}
        if p.get("color") is not None:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(p["color"])):
                raise PlacementError(f"{what}的颜色要写成 #rrggbb")
            piece["color"] = str(p["color"]).lower()
        fns = p.get("functions") or []
        if not isinstance(fns, list) or not all(isinstance(f, str) and furniture.FUNCTION_RE.match(f) for f in fns):
            raise PlacementError(f"{what}的功能名不对")
        piece["functions"] = list(dict.fromkeys(fns))
        if p.get("image") is not None:
            piece["image"] = picture(p["image"], what)
        if p.get("wall") is not None:
            if p["wall"] not in furniture.WALLS or not isinstance(p.get("along"), int) or not isinstance(p.get("y"), (int, float)):
                raise PlacementError(f"{what}挂在哪写得不对")
            piece.update(wall=p["wall"], along=int(p["along"]), y=round(float(p["y"]), 1))
        else:
            at = p.get("at")
            if not (isinstance(at, list) and len(at) == 2 and all(isinstance(v, int) for v in at)) or p.get("turn", 0) not in furniture.TURNS:
                raise PlacementError(f"{what}放在哪写得不对")
            piece.update(at=list(at), turn=p.get("turn", 0))
        out.append(piece)
    # bedrooms are named for whoever lives there, which can be longer than a name typed in; cut to fit on install
    return {"name": _text(room.get("name"), 16, "房间名", True), "kind": kind, "size": size,
            "floor": look(room.get("floor"), "地板"), "walls": look(room.get("walls"), "墙"), "pieces": out}


# ---- looking at a pack before installing it ----

class Staging:
    """Packs uploaded for a preview, waiting to be installed (in memory, for a while)."""

    def __init__(self):
        self.waiting: dict[str, tuple[float, dict, dict[str, bytes]]] = {}

    def add(self, manifest: dict, files: dict[str, bytes]) -> str:
        now = time.time()
        self.waiting = {k: v for k, v in self.waiting.items() if now - v[0] < STAGED_FOR}
        token = uuid.uuid4().hex
        self.waiting[token] = (now, manifest, files)
        return token

    def take(self, token: str) -> tuple[dict, dict[str, bytes]]:
        entry = self.waiting.pop(str(token), None)
        if entry is None or time.time() - entry[0] >= STAGED_FOR:
            raise PlacementError("这个包等太久了，重新选一次文件")
        return entry[1], entry[2]


def preview(manifest: dict, files: dict[str, bytes], available: set[str]) -> dict:
    """What the owner sees before installing: what it is, who made it, its license, what is in it, what it needs."""
    picture = None
    if manifest["preview"]:
        kind = models.image_kind(files[manifest["preview"]])
        picture = f"data:{models.IMAGE_TYPES[kind]};base64," + base64.b64encode(files[manifest["preview"]]).decode()
    needs = sorted(set(manifest["needs"]) | {f for k in manifest["furniture"] for f in k["functions"]}
                   | {f for p in (manifest["room"] or {}).get("pieces", []) for f in p["functions"]})
    return {"kind": manifest["kind"], "id": manifest["id"], "name": manifest["name"], "author": manifest["author"],
            "version": manifest["version"], "license": manifest["license"], "license_name": LICENSES[manifest["license"]],
            "description": manifest["description"], "picture": picture, "needs": needs,
            "missing": [f for f in needs if f not in available],
            "furniture": [{"name": k["name"], "template": k["template"], "size": k["size"], "height": k["height"],
                           "color": k["color"], "mount": k["mount"], "functions": k["functions"], "tags": k["tags"],
                           "model": len(files[k["model"]["file"]]) if k["model"] else 0} for k in manifest["furniture"]],
            "surfaces": [{"name": x["name"], "use": USES[x["use"]], "tile": x["tile"], "picture": _data_url(files[x["image"]])}
                         for x in manifest["surfaces"]],
            "room": None if not manifest["room"] else {
                "name": manifest["room"]["name"], "size": manifest["room"]["size"], "pieces": len(manifest["room"]["pieces"]),
                "pictures": sum(1 for p in manifest["room"]["pieces"] if p.get("image"))
                + sum(1 for part in ("floor", "walls") if (manifest["room"][part] or {}).get("image"))},
            "bytes": sum(len(b) for b in files.values())}


def _data_url(body: bytes) -> str:
    return f"data:{models.IMAGE_TYPES[models.image_kind(body)]};base64," + base64.b64encode(body).decode()


# ---- installed packs ----

def _registry(data_dir: Path) -> Path:
    return data_dir / "packs.json"


def installed(data_dir: Path) -> list[dict]:
    try:
        return json.loads(_registry(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(data_dir: Path, packs: list[dict]) -> None:
    from .games.spire_bridge import atomic_json  # the same careful write as home.json
    atomic_json(_registry(data_dir), packs)


def _free_name(home: Home, name: str) -> str:
    taken = {t["name"] for t in home.all_types().values()}
    if name not in taken:
        return name
    n = 2
    while f"{name}（{n}）" in taken:
        n += 1
    return f"{name}（{n}）"[:16]


def install(home: Home, data_dir: Path, manifest: dict, files: dict[str, bytes]) -> dict:
    """Make copies of what the pack holds: each kind of furniture becomes one of the owner's own, models and
    pictures are stored like uploads; pictures of a surface pack go into the picture library; a room pack's
    room is added next to a hallway with a door, furnished as it was (what no longer fits is skipped, and
    said). Noted in packs.json, so it can be uninstalled."""
    if len(home.types) + len(manifest["furniture"]) > furniture.MAX_TYPES:
        raise PlacementError("自己做的家具种类太多了，装不下这个包")
    entry = {"uid": uuid.uuid4().hex[:12], "id": manifest["id"], "kind": manifest["kind"], "name": manifest["name"],
             "author": manifest["author"], "version": manifest["version"], "license": manifest["license"],
             "installed_at": time.time(), "types": [], "models": [], "pictures": [], "rooms": [], "skipped": [], "moved": []}
    stored: dict[str, str] = {}

    def picture(path: str) -> str:
        if path not in stored:
            stored[path] = models.store_image(data_dir, files[path])["file"]
        return stored[path]

    made = {}
    for i, kind in enumerate(manifest["furniture"]):
        model = None
        if kind["model"]:
            info = models.store(data_dir, files[kind["model"]["file"]])
            entry["models"].append(info["file"])
            model = {**kind["model"], "file": info["file"]}
        raw = {k: kind[k] for k in ("template", "size", "height", "color", "functions", "tags", "mount")}
        saved = home.save_type({**raw, "name": _free_name(home, kind["name"]), "model": model})
        entry["types"].append(saved["id"])
        made[f"pack/{i}"] = saved["id"]
    for s_ in manifest["surfaces"]:
        file = picture(s_["image"])
        if not any(p["file"] == file for p in home.pictures):
            home.pictures.append({"file": file, "name": s_["name"], "use": s_["use"], "tile": s_["tile"]})
            entry["pictures"].append(file)
    room = manifest["room"]
    if room:
        added = home.add_room(room["name"], room["size"], kind=room["kind"])
        entry["rooms"].append(added["id"])
        for part in ("floor", "walls"):
            if room[part]:
                look = room[part] if "color" in room[part] else {"image": picture(room[part]["image"]), "tile": room[part]["tile"]}
                home.dress_room(added["id"], {part: look})
        types = home.all_types()
        for p in room["pieces"]:
            piece = {**{k: v for k, v in p.items() if k != "image"}, "type": made.get(p["type"], p["type"]), "room": added["id"],
                     "added_by": "owner", "owner": ""}
            if p.get("image"):
                piece["image"] = picture(p["image"])
            label = p.get("name") or types.get(piece["type"], {}).get("name", "家具")
            try:
                home.place(piece)
            except PlacementError as e:
                try:  # its old place is taken here (say, by the new room's door): the nearest free one by the same wall
                    home.place(_nearby(home, piece, added, types))
                    entry["moved"].append(label)
                except PlacementError:
                    entry["skipped"].append(f"{label}：{e}")
    home.save()
    _save(data_dir, installed(data_dir) + [entry])
    return entry


def _nearby(home: Home, piece: dict, room: dict, types: dict) -> dict:
    """Where a piece goes in a room when its old place is taken: by the wall it stood against (or hung on)."""
    from .redecorate import place_by_words

    ftype = types[piece["type"]]
    if "wall" in piece:
        wall = piece["wall"]
        rest = {k: v for k, v in piece.items() if k not in ("wall", "along", "y")}
    else:
        w, d = furniture.turned_size(ftype["size"], piece["turn"])
        x, z = piece["at"]
        cols, rows = room["size"]
        gaps = {"back": z, "front": rows - z - d, "left": x, "right": cols - x - w}
        wall = min(gaps, key=gaps.get) if min(gaps.values()) <= 1 else "middle"
        rest = {k: v for k, v in piece.items() if k not in ("at", "turn")}
    return {**rest, **place_by_words(home, rest, ftype, {"where": {"against": wall}})}


def uninstall(home: Home, data_dir: Path, uid: str) -> dict:
    """Take away the kinds and library pictures a pack brought that nothing in the house uses; the rest stays,
    and is said. Rooms made from a pack are the owner's now and stay."""
    packs = installed(data_dir)
    entry = next((p for p in packs if p["uid"] == uid), None)
    if entry is None:
        raise PlacementError("没有装过这个包")
    removed, kept = [], []
    for tid in entry["types"]:
        if tid not in home.types:
            continue
        if any(p["type"] == tid for p in home.pieces or []):
            kept.append(home.types[tid]["name"])
            continue
        removed.append(home.types[tid]["name"])
        home.delete_type(tid)
    in_use = {r[part]["image"] for r in home.rooms for part in ("floor", "walls") if (r.get(part) or {}).get("image")} \
        | {p["image"] for p in home.pieces or [] if p.get("image")}
    for file in entry.get("pictures", []):
        pic = next((p for p in home.pictures if p["file"] == file), None)
        if pic is None:
            continue
        if file in in_use:
            kept.append(pic["name"])
            continue
        removed.append(pic["name"])
        home.pictures.remove(pic)
    home.save()
    _save(data_dir, [p for p in packs if p["uid"] != uid])
    return {"removed": removed, "kept": kept}


# ---- making a pack ----

def export_furniture(home: Home, data_dir: Path, tids: list[str], meta: dict, picture: bytes | None = None) -> bytes:
    """A furniture pack of some of the owner's own kinds, with their models."""
    if not tids:
        raise PlacementError("选几种自己做的家具")
    kinds = []
    for tid in dict.fromkeys(tids):
        if tid not in home.types:
            raise PlacementError(f"只能导出自己做的家具：{tid}")
        kinds.append(home.types[tid])
    files: dict[str, bytes] = {}
    out = []
    for i, kind in enumerate(kinds):
        model = None
        if kind.get("model"):
            path = models.path_of(data_dir, kind["model"]["file"])
            if path is None:
                raise PlacementError(f"{kind['name']}的模型文件找不到了")
            name = f"models/{i + 1}-{kind['model']['file'][:8]}.glb"
            files[name] = path.read_bytes()
            model = {**kind["model"], "file": name}
        out.append({k: kind[k] for k in ("name", "template", "size", "height", "color", "functions", "tags", "mount")} | {"model": model})
    name = _text(meta.get("name"), 40, "包的名字") or kinds[0]["name"]
    slug = re.sub(r"[^a-z0-9-]+", "-", str(meta.get("id") or "").lower()).strip("-")[:48] or "furniture-" + uuid.uuid4().hex[:8]
    manifest = {"format": FORMAT, "kind": "furniture", "id": slug, "name": name, "author": _text(meta.get("author"), 40, "作者"),
                "version": _text(meta.get("version") or "1", 20, "版本"), "license": meta.get("license") or "CC-BY-4.0",
                "description": _text(meta.get("description"), 500, "说明"), "preview": None,
                "needs": sorted({f for k in out for f in k["functions"]}), "furniture": out}
    if picture:
        kind_ = models.image_kind(picture)
        manifest["preview"] = f"preview.{kind_}"
        files[manifest["preview"]] = picture
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    check_manifest(manifest, files)  # what we hand out is what we would take in
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return buffer.getvalue()


def _write(manifest: dict, files: dict[str, bytes]) -> bytes:
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    check_manifest(manifest, files)  # what we hand out is what we would take in
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            archive.writestr(path, body)
    return buffer.getvalue()


def _head(kind: str, meta: dict, name: str) -> dict:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(meta.get("id") or "").lower()).strip("-")[:48] or f"{kind}-" + uuid.uuid4().hex[:8]
    return {"format": FORMAT, "kind": kind, "id": slug, "name": _text(meta.get("name"), 40, "包的名字") or name,
            "author": _text(meta.get("author"), 40, "作者"), "version": _text(meta.get("version") or "1", 20, "版本"),
            "license": meta.get("license") or "CC-BY-4.0", "description": _text(meta.get("description"), 500, "说明"),
            "preview": None, "needs": []}


def _picture_file(data_dir: Path, file: str, files: dict[str, bytes]) -> str:
    """A stored picture into a pack's textures/ (once), by the name it will have there."""
    path = models.image_path(data_dir, file)
    if path is None:
        raise PlacementError(f"图片找不到了：{file}")
    name = "textures/" + file
    files[name] = path.read_bytes()
    return name


def pictures(home: Home) -> list[dict]:
    """Every picture the house has: the library, and those on floors, walls and in frames; for exporting."""
    out = {p["file"]: dict(p) for p in home.pictures}
    for r in home.rooms:
        for part, word in (("floor", "地板"), ("walls", "墙")):
            look = r.get(part) or {}
            if look.get("image") and look["image"] not in out:
                out[look["image"]] = {"file": look["image"], "name": f"{r['name']}的{word}"[:16], "use": part, "tile": look.get("tile", 1)}
    types = home.all_types()
    for p in home.pieces or []:
        if p.get("image") and p["image"] not in out:
            label = p.get("name") or types.get(p["type"], {}).get("name", "画")
            out[p["image"]] = {"file": p["image"], "name": f"{home.room_name(p['room'])}的{label}"[:16], "use": "picture", "tile": 1}
    return list(out.values())


def export_surfaces(home: Home, data_dir: Path, chosen: list, meta: dict) -> bytes:
    """A picture pack: chosen pictures of the house, each with a name, what it is for and how big a tile is."""
    known = {p["file"]: p for p in pictures(home)}
    files: dict[str, bytes] = {}
    surfaces = []
    for item in chosen if isinstance(chosen, list) else []:
        file = str(item.get("file") if isinstance(item, dict) else item)
        base = known.get(file)
        if base is None:
            raise PlacementError(f"家里没有这张图：{file}")
        extra = item if isinstance(item, dict) else {}
        surfaces.append({"name": extra.get("name") or base["name"], "image": _picture_file(data_dir, file, files),
                         "use": extra.get("use") or base["use"], "tile": extra.get("tile") or base["tile"]})
    if not surfaces:
        raise PlacementError("选几张图")
    manifest = {**_head("surface", meta, surfaces[0]["name"]), "furniture": [], "surfaces": surfaces}
    first = surfaces[0]["image"]  # the first picture is the pack's picture too
    manifest["preview"] = "preview." + first.rsplit(".", 1)[1]
    files[manifest["preview"]] = files[first]
    return _write(manifest, files)


def export_room(home: Home, data_dir: Path, rid: str, meta: dict) -> bytes:
    """A room pack: the room's size, floor and walls, and every piece in it where it stands; the owner's own kinds
    it uses and their models, and its pictures, travel with it. Built-in kinds go by name."""
    room = home.room(rid)
    types = home.all_types()
    files: dict[str, bytes] = {}
    kinds: list[dict] = []
    keys: dict[str, str] = {}
    pieces = []
    for p in home.pieces or []:
        if p["room"] != rid or p["type"] in NOT_IN_ROOMS or p["type"] not in types:
            continue
        tid = p["type"]
        if tid.startswith("mine/"):
            if tid not in keys:
                kind = types[tid]
                model = None
                if kind.get("model"):
                    path = models.path_of(data_dir, kind["model"]["file"])
                    if path is None:
                        raise PlacementError(f"{kind['name']}的模型文件找不到了")
                    name = f"models/{len(kinds) + 1}-{kind['model']['file'][:8]}.glb"
                    files[name] = path.read_bytes()
                    model = {**kind["model"], "file": name}
                kinds.append({k: kind[k] for k in ("name", "template", "size", "height", "color", "functions", "tags", "mount")}
                             | {"model": model})
                keys[tid] = f"pack/{len(kinds) - 1}"
            tid = keys[tid]
        piece = {"type": tid, "color": p.get("color"), "name": p.get("name"), "functions": list(p.get("functions") or [])}
        piece.update({k: p[k] for k in ("wall", "along", "y")} if "wall" in p else {"at": p["at"], "turn": p["turn"]})
        if p.get("image"):
            piece["image"] = _picture_file(data_dir, p["image"], files)
        pieces.append(piece)
    looks = {}
    for part in ("floor", "walls"):
        look = room.get(part)
        looks[part] = None if not look else {"color": look["color"]} if "color" in look else \
            {"image": _picture_file(data_dir, look["image"], files), "tile": look.get("tile", 1)}
    manifest = {**_head("room", meta, room["name"]), "furniture": kinds, "surfaces": [],
                "room": {"name": room["name"], "kind": room.get("kind", "room"), "size": room["size"], **looks, "pieces": pieces}}
    manifest["needs"] = sorted({f for p in pieces for f in p["functions"]} | {f for k in kinds for f in k["functions"]})
    return _write(manifest, files)
