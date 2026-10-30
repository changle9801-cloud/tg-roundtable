"""Furniture: kinds of furniture (types), the pieces placed in rooms, and where they may go.

A type says what a piece looks like (a built-in template, sized and coloured), what people
can do on it (``uses``: sit, lie, stand, each at a spot facing a way), whether it is a
screen, and which functions it carries by default. A piece is one placed item: its room,
the grid cell of its footprint's top-left corner, and its turn. See docs/furniture.md.

Units: a grid cell is half a unit; sizes and spots are in cells, heights in units.
Turns are clockwise seen from above, 0 = the front faces the bottom of the room.
"""

from __future__ import annotations

import math
import re
import uuid

CELL = 0.5  # units per grid cell
DOOR_WIDTH = 3  # cells
DOOR_CLEAR = 2  # cells in front of a door that must stay free
TURNS = (0, 90, 180, 270)


def _type(tid, name, template, size, height, color, *, uses=(), screen=None, blocks=True, functions=(), tags=()):
    return {"id": f"base/{tid}", "name": name, "template": template, "size": list(size), "height": height,
            "color": color, "model": None, "blocks": blocks, "uses": [dict(u) for u in uses],
            "screen": screen, "functions": list(functions), "tags": list(tags)}


def _sit(x, z, face=0, y=0.0):
    return {"pose": "sit", "at": [x, z], "face": face, "y": y}


# The furniture of the house as first drawn (stage 1), now as data. Front = bottom (+z).
BUILTIN_TYPES = {t["id"]: t for t in (
    _type("bed", "床", "bed", (3, 5), 0.47, "#a1887f",
          uses=[{"pose": "lie", "at": [1.5, 2.8], "face": 0, "y": 0.5}, _sit(3.6, 3.6, 90)], tags=["卧室"]),
    _type("writing-desk", "书桌和日记本", "writing-desk", (3, 1), 0.7, "#bcaaa4",
          uses=[_sit(1.5, 1.7, 180)], tags=["卧室"]),
    _type("floor-lamp", "落地灯", "floor-lamp", (1, 1), 1.1, "#ffe082", tags=["装饰"]),
    _type("rug-small", "小地毯", "rug", (3, 2), 0.02, "#d7ccc8", blocks=False, tags=["装饰"]),
    _type("computer-desk", "电脑桌", "computer-desk", (3, 3), 0.72, "#d7ccc8",
          uses=[_sit(1.5, 2.2, 180)], functions=["web.read"], tags=["书房"]),
    _type("bookshelf", "书架", "bookshelf", (5, 1), 2.0, "#8d6e63", tags=["书房"]),
    _type("plant", "盆栽", "plant", (1, 1), 1.1, "#66bb6a", tags=["装饰"]),
    _type("big-plant", "大盆栽", "plant", (2, 2), 1.4, "#43a047", tags=["装饰"]),
    _type("tv", "电视", "tv", (6, 1), 1.85, "#212121", screen={"face": 0},
          functions=["video.watch", "game.play"], tags=["娱乐"]),
    _type("sofa-3", "三人沙发", "sofa", (6, 2), 0.9, "#5c6bc0",
          uses=[_sit(1, 0.9, 0, 0.05), _sit(3, 0.9, 0, 0.05), _sit(5, 0.9, 0, 0.05)], tags=["客厅", "娱乐"]),
    _type("beanbag", "懒人沙发", "beanbag", (2, 2), 0.4, "#ff8a65", uses=[_sit(1, 1, 180, 0.05)], tags=["娱乐"]),
    _type("rug-large", "大地毯", "rug", (5, 4), 0.02, "#b39ddb", blocks=False, tags=["装饰"]),
    _type("coffee-table", "茶几", "table", (4, 2), 0.4, "#bcaaa4", tags=["客厅"]),
    _type("frame", "画框", "frame", (2, 1), 0.6, "#90caf9", blocks=False, tags=["装饰"]),
    _type("fridge", "冰箱", "fridge", (2, 2), 1.9, "#eceff1", tags=["厨房"]),
    _type("counter", "橱柜", "counter", (8, 1), 0.9, "#b0bec5", tags=["厨房"]),
    _type("dining-table", "餐桌和四把椅子", "dining-table", (4, 4), 0.72, "#d7ccc8",
          uses=[_sit(1, 0.4, 0), _sit(3, 0.4, 0), _sit(1, 3.6, 180), _sit(3, 3.6, 180)], tags=["厨房"]),
    _type("boxes", "一堆纸箱", "boxes", (3, 3), 1.2, "#bcaaa4", tags=["储物间"]),
)}

# Rooms as first drawn: id, name, icon, size in cells, doors (wall, optional centre cell).
SHARED_ROOMS = (
    ("living", "客厅", "🛋️", (18, 12), ({"wall": "back", "at": 3}, {"wall": "front", "outside": True})),
    ("kitchen", "厨房", "🍳", (12, 12), ({"wall": "back"},)),
    ("study", "书房", "📚", (18, 12), ({"wall": "front"},)),
    ("fun", "娱乐室", "🎮", (18, 12), ({"wall": "front"},)),
    ("storage", "储物间", "📦", (10, 12), ({"wall": "back"},)),
)
BEDROOM_SIZE = (10, 10)
BEDROOM_DOORS = ({"wall": "front"},)

# The default furniture of each room: (type, at, turn, extra fields).
DEFAULT_LAYOUT = {
    "bedroom": [("bed", (1, 1), 0, {}), ("writing-desk", (6, 0), 0, {}), ("floor-lamp", (9, 0), 0, {}),
                ("rug-small", (5, 6), 0, {})],
    "study": [("computer-desk", (2, 0), 0, {}), ("computer-desk", (8, 0), 0, {}), ("computer-desk", (14, 0), 0, {}),
              ("bookshelf", (17, 6), 90, {}), ("plant", (0, 10), 0, {})],
    "fun": [("tv", (6, 0), 0, {}), ("rug-large", (6, 2), 0, {}), ("sofa-3", (6, 6), 180, {}),
            ("beanbag", (1, 9), 0, {})],
    "living": [("sofa-3", (6, 1), 0, {"color": "#8d6e63"}), ("rug-large", (5, 3), 0, {"color": "#ffe0b2"}),
               ("coffee-table", (7, 5), 0, {}), ("frame", (5, 0), 0, {"color": "#90caf9"}),
               ("frame", (8, 0), 0, {"color": "#a5d6a7"}), ("frame", (11, 0), 0, {"color": "#ffcc80"}),
               ("big-plant", (16, 0), 0, {})],
    "kitchen": [("fridge", (0, 0), 0, {}), ("counter", (0, 3), 270, {}), ("dining-table", (6, 5), 0, {})],
    "storage": [("boxes", (0, 0), 0, {})],
}


class PlacementError(ValueError):
    pass


def new_id() -> str:
    return "p-" + uuid.uuid4().hex[:8]


def turned_size(size, turn: int) -> tuple[int, int]:
    w, d = size
    return (d, w) if turn in (90, 270) else (w, d)


def footprint(piece: dict, ftype: dict) -> set[tuple[int, int]]:
    w, d = turned_size(ftype["size"], piece["turn"])
    x, z = piece["at"]
    return {(x + i, z + j) for i in range(w) for j in range(d)}


def door_cells(room: dict) -> set[tuple[int, int]]:
    """The cells in front of each door that furniture in the way must keep free."""
    w, d = room["size"]
    cells = set()
    for door in room.get("doors", ()):
        along = w if door["wall"] in ("front", "back") else d
        centre = door.get("at", along / 2)
        cols = [i for i in range(along) if i < centre + DOOR_WIDTH / 2 and i + 1 > centre - DOOR_WIDTH / 2]
        depth = range(DOOR_CLEAR)
        for i in cols:
            for k in depth:
                cells.add({"front": (i, d - 1 - k), "back": (i, k), "left": (k, i), "right": (w - 1 - k, i)}[door["wall"]])
    return cells


def check(piece: dict, room: dict, types: dict, others: list[dict]) -> None:
    """Raise PlacementError unless the piece fits: inside the room, not on another, not blocking a door."""
    ftype = types.get(piece.get("type"))
    if ftype is None:
        raise PlacementError(f"没有这种家具：{piece.get('type')}")
    if piece.get("turn") not in TURNS:
        raise PlacementError("朝向只能是 0、90、180、270")
    at = piece.get("at")
    if not (isinstance(at, (list, tuple)) and len(at) == 2 and all(isinstance(v, int) for v in at)):
        raise PlacementError("位置要写成 [列, 行]，都是整数")
    w, d = turned_size(ftype["size"], piece["turn"])
    rw, rd = room["size"]
    if at[0] < 0 or at[1] < 0 or at[0] + w > rw or at[1] + d > rd:
        raise PlacementError(f"{ftype['name']}放不下：超出了{room.get('name', room['id'])}")
    if not ftype["blocks"]:
        return
    mine = footprint(piece, ftype)
    if mine & door_cells(room):
        raise PlacementError(f"{ftype['name']}挡住门了")
    for other in others:
        if other["id"] == piece.get("id") or other["room"] != piece["room"]:
            continue
        otype = types.get(other["type"])
        if otype and otype["blocks"] and mine & footprint(other, otype):
            raise PlacementError(f"{ftype['name']}和{other.get('name') or otype['name']}叠在一起了")


def default_pieces(room: dict, kind: str) -> list[dict]:
    pieces = []
    for tid, at, turn, extra in DEFAULT_LAYOUT.get(kind, ()):
        piece = {"id": new_id(), "type": f"base/{tid}", "room": room["id"], "at": list(at), "turn": turn,
                 "color": None, "name": None, "functions": list(BUILTIN_TYPES[f"base/{tid}"]["functions"]),
                 "added_by": "owner", "owner": room.get("owner", "")}
        piece.update(extra)
        pieces.append(piece)
    return pieces


def spot(piece: dict, ftype: dict, use: dict) -> tuple[float, float, float]:
    """Where a use puts a person, in room units (x, z), and the angle she faces (degrees clockwise)."""
    w, d = ftype["size"]
    cx, cz = (use["at"][0] - w / 2) * CELL, (use["at"][1] - d / 2) * CELL  # from the footprint's centre
    a = math.radians(piece["turn"])
    rx, rz = cx * math.cos(a) - cz * math.sin(a), cx * math.sin(a) + cz * math.cos(a)
    tw, td = turned_size(ftype["size"], piece["turn"])
    ox, oz = (piece["at"][0] + tw / 2) * CELL, (piece["at"][1] + td / 2) * CELL
    return ox + rx, oz + rz, (piece["turn"] + use.get("face", 0)) % 360


# ---- functions: what a piece of furniture can do, by name (docs/furniture.md, 功能名) ----

FUNCTIONS = {
    "web.search": "联网搜索",
    "web.read": "读网页",
    "video.watch": "看视频",
    "game.play": "玩游戏",
    "draw.picture": "画画",
    "chat.telegram": "用 Telegram 聊天",
    "diary.write": "写日记",
}
BUILTIN_PROVIDERS = {  # who does a built-in function when nobody linked a tool to it
    "web.search": "builtin:search", "game.play": "builtin:game", "draw.picture": "builtin:draw",
    "chat.telegram": "app:telegram", "diary.write": "builtin:diary",
}
FUNCTION_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}\.[a-z][a-z0-9-]{0,23}$")
# A tool's own tool names that make it the obvious provider of a built-in function.
TOOL_HINTS = {"web.read": ("read_post", "read_feed"), "video.watch": ("watch_video", "search_videos")}


def unlinked_name(tool_id: str) -> str:
    """The function a tool has until the owner links it somewhere: its box in the storage room."""
    return f"tool.{tool_id}"


def default_links(tools: list[dict]) -> dict[str, list[str]]:
    """Links for a house that never had any: tools that read pages or watch videos do just that."""
    links: dict[str, list[str]] = {}
    for tool in tools:
        for function, hints in TOOL_HINTS.items():
            if set(tool.get("allowed") or ()) & set(hints):
                links.setdefault(function, []).append(f"tool:{tool['id']}")
    return links


def tool_functions(links: dict[str, list[str]], tool_id: str) -> list[str]:
    """The functions a tool does: the ones it is linked to, else its own unlinked name."""
    names = [f for f, providers in links.items() if f"tool:{tool_id}" in providers]
    return names or [unlinked_name(tool_id)]


BUILTIN_TYPES["base/parcel"] = _type("parcel", "纸箱", "parcel", (2, 2), 0.7, "#c8a26b", tags=["储物间"])
