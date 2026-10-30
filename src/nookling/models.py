"""Files the owner uploads for the house, checked and kept in the data folder: 3D models for
furniture (e.g. made with Tripo) and pictures for floors, walls and murals.

    <data_dir>/home/models/<sha256 prefix>.glb
    <data_dir>/home/images/<sha256 prefix>.png|jpg|webp

Only binary glTF (.glb) with everything packed inside is taken: a model that names an
outside address would make the page fetch it. Draco/meshopt compression is refused for
now, because the page has no decoder for it.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
NAME_RE = re.compile(r"^[0-9a-f]{16}\.glb$")
UNSUPPORTED = {"KHR_draco_mesh_compression": "Draco 压缩", "EXT_meshopt_compression": "meshopt 压缩"}


class ModelError(ValueError):
    pass


def inspect(data: bytes) -> dict:
    """Check a .glb and say what is in it: meshes, triangles (roughly), animations."""
    if len(data) > MAX_BYTES:
        raise ModelError(f"模型太大了：{len(data) // (1024 * 1024)} MB，最多 {MAX_BYTES // (1024 * 1024)} MB")
    if len(data) < 20 or data[:4] != b"glTF":
        raise ModelError("不是 .glb 文件（导出时选 GLB 格式）")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2 or length != len(data):
        raise ModelError("这个 .glb 文件不完整或者版本不对")
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A:  # "JSON"
        raise ModelError("这个 .glb 文件里找不到模型描述")
    try:
        gltf = json.loads(data[20:20 + chunk_len])
    except ValueError:
        raise ModelError("这个 .glb 文件的模型描述读不了") from None
    for kind in ("buffers", "images"):
        for item in gltf.get(kind) or []:
            uri = item.get("uri")
            if uri is not None and not str(uri).startswith("data:"):
                raise ModelError("模型引用了外部文件或网址；导出时要把贴图打包进 .glb")
    for ext in gltf.get("extensionsRequired") or []:
        if ext in UNSUPPORTED:
            raise ModelError(f"模型用了 {UNSUPPORTED[ext]}，页面现在打不开；导出时关掉压缩")
        raise ModelError(f"模型需要 {ext}，页面现在不支持")
    accessors = gltf.get("accessors") or []
    triangles = 0
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            idx = prim.get("indices")
            pos = (prim.get("attributes") or {}).get("POSITION")
            count = accessors[idx]["count"] if isinstance(idx, int) and idx < len(accessors) else (
                accessors[pos]["count"] if isinstance(pos, int) and pos < len(accessors) else 0)
            triangles += count // 3
    return {"bytes": len(data), "meshes": len(gltf.get("meshes") or []), "triangles": triangles,
            "animations": [str(a.get("name") or f"动画{i + 1}") for i, a in enumerate(gltf.get("animations") or [])]}


def folder(data_dir: Path) -> Path:
    return data_dir / "home" / "models"


def store(data_dir: Path, data: bytes) -> dict:
    """Check and keep a model; the same file uploaded twice is kept once."""
    info = inspect(data)
    name = hashlib.sha256(data).hexdigest()[:16] + ".glb"
    path = folder(data_dir) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return {"file": name, **info}


def path_of(data_dir: Path, name: str) -> Path | None:
    if not NAME_RE.match(name):
        return None
    path = folder(data_dir) / name
    return path if path.is_file() else None


# ---- pictures: floor tiles, wallpaper, murals ----

MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_RE = re.compile(r"^[0-9a-f]{16}\.(png|jpg|webp)$")
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def image_kind(data: bytes) -> str:
    """png, jpg or webp from the file's first bytes; anything else is refused."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ModelError(f"图片太大了：{len(data) // 1024} KB，最多 {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ModelError("只收 PNG、JPG、WebP 图片")


def store_image(data_dir: Path, data: bytes) -> dict:
    kind = image_kind(data)
    name = hashlib.sha256(data).hexdigest()[:16] + "." + kind
    path = data_dir / "home" / "images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return {"file": name, "bytes": len(data)}


def image_path(data_dir: Path, name: str) -> Path | None:
    if not IMAGE_RE.match(name):
        return None
    path = data_dir / "home" / "images" / name
    return path if path.is_file() else None
