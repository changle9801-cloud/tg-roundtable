"""Packs (docs/packs.md): what the family's home is made of, shared as a file.

A pack is a ``.nookpack`` (a zip) holding only data: ``manifest.json``, an optional preview
picture, ``models/*.glb`` and ``textures/*`` pictures. Nothing in it runs; anything else, too
much of anything, or a manifest that does not check out, and the whole pack is refused.
Installing makes copies (new ids, "（2）" on clashing names); a pack never connects tools.
This part (5a) does furniture packs.
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
from .home import Home

FORMAT = 1
PACK_MAX = 60 * 1024 * 1024  # the file
UNPACKED_MAX = 120 * 1024 * 1024  # all of it unpacked: no zip bombs
FILES_MAX = 200
KINDS_MAX = 50  # kinds of furniture in one pack
STAGED_FOR = 1800  # seconds a pack waits between preview and install
LICENSES = {"CC0-1.0": "CC0（随便用）", "CC-BY-4.0": "CC BY 4.0（署名）", "CC-BY-SA-4.0": "CC BY-SA 4.0（署名、相同方式共享）",
            "all-rights-reserved": "保留所有权利"}
PACK_KINDS = ("furniture",)  # surface, room and member come with 5b and 5c
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
                "preview": None, "furniture": []}
    preview = raw.get("preview")
    if preview is not None:
        if preview not in files or not preview.startswith("preview."):
            raise PlacementError("预览图不在包里")
        manifest["preview"] = preview
    kinds = raw.get("furniture")
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= KINDS_MAX:
        raise PlacementError(f"家具包里要有 1 到 {KINDS_MAX} 种家具")
    used = {"manifest.json", manifest["preview"]}
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
    extra = set(files) - used
    if extra:
        raise PlacementError(f"包里有没用到的文件：{', '.join(sorted(extra))}")
    return manifest


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
    needs = sorted(set(manifest["needs"]) | {f for k in manifest["furniture"] for f in k["functions"]})
    return {"kind": manifest["kind"], "id": manifest["id"], "name": manifest["name"], "author": manifest["author"],
            "version": manifest["version"], "license": manifest["license"], "license_name": LICENSES[manifest["license"]],
            "description": manifest["description"], "picture": picture, "needs": needs,
            "missing": [f for f in needs if f not in available],
            "furniture": [{"name": k["name"], "template": k["template"], "size": k["size"], "height": k["height"],
                           "color": k["color"], "mount": k["mount"], "functions": k["functions"], "tags": k["tags"],
                           "model": len(files[k["model"]["file"]]) if k["model"] else 0} for k in manifest["furniture"]],
            "bytes": sum(len(b) for b in files.values())}


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
    """Make copies of what the pack holds: each kind of furniture becomes one of the owner's own, models stored
    like uploads. Noted in packs.json, so it can be uninstalled."""
    if len(home.types) + len(manifest["furniture"]) > furniture.MAX_TYPES:
        raise PlacementError("自己做的家具种类太多了，装不下这个包")
    made, stored = [], []
    for kind in manifest["furniture"]:
        model = None
        if kind["model"]:
            info = models.store(data_dir, files[kind["model"]["file"]])
            stored.append(info["file"])
            model = {**kind["model"], "file": info["file"]}
        raw = {k: kind[k] for k in ("template", "size", "height", "color", "functions", "tags", "mount")}
        saved = home.save_type({**raw, "name": _free_name(home, kind["name"]), "model": model})
        made.append(saved["id"])
    entry = {"uid": uuid.uuid4().hex[:12], "id": manifest["id"], "kind": manifest["kind"], "name": manifest["name"],
             "author": manifest["author"], "version": manifest["version"], "license": manifest["license"],
             "installed_at": time.time(), "types": made, "models": stored}
    _save(data_dir, installed(data_dir) + [entry])
    return entry


def uninstall(home: Home, data_dir: Path, uid: str) -> dict:
    """Take away the kinds a pack brought that nothing in the house uses; the rest stays, and is said."""
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
