"""The members' home: its rooms, where each member is and what she is doing, and diary reads.

    <data_dir>/home.json  {"rooms": [...], "doors": [...], "floors": [...], "pieces": [...], "types": {...},
                           "where": {member: Place}, "looks": {member: {...}}, "diary_reads": [...]}

Rooms have a position and size in grid cells on their floor (hallways are rooms too) and the
doors join them (see floorplan.py and docs/floorplan.md); pieces are the furniture placed in
them (see furniture.py and docs/furniture.md). A house without pieces gets the default set.

Where a member is follows what she really does. A daily-life activity takes her to its room
and she stays there afterwards; asleep she lies in her bedroom, busy she is out of the house.
Walking between rooms is only drawn by the page. Nothing here calls a model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import floorplan, furniture, models, owner
from .furniture import BEDROOM_DOORS, BEDROOM_SIZE, BUILTIN_TYPES, SHARED_ROOMS, PlacementError
from .games.spire_bridge import atomic_json

log = logging.getLogger(__name__)

ACCESSORIES = {
    "scarf": "围巾", "headphones": "头戴耳机", "sunglasses": "墨镜", "whale-clip": "小鲸鱼发卡",
    "hat": "帽子", "glasses": "眼镜", "bow": "蝴蝶结", "backpack": "小背包",
}
DEFAULT_LOOKS = {
    "claude": {"color": "#d97757", "accessory": "scarf"},
    "codex": {"color": "#2f6b4f", "accessory": "headphones"},
    "grok": {"color": "#4a3566", "accessory": "sunglasses"},
    "deepseek": {"color": "#2b6cb0", "accessory": "whale-clip"},
}
SPARE_COLORS = ("#8a5a9e", "#b7791f", "#2c7a7b", "#c05680", "#4a5568")
# What a member is doing, in the fixed words guests may see. Details stay on the admin page.
VERBS = {
    "browse": "在看东西", "read": "在看东西", "write": "在写东西", "watch": "在看视频", "play": "在玩游戏", "use": "在用东西",
    "phone": "在打电话", "visit": "在找朋友聊天", "share": "在看手机", "rest": "在歇着",
    "sleep": "在睡觉", "out": "出门了",
}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Where she goes when she just wants to rest, from the words of her own reason.
REST_ROOMS = (
    ("bed", re.compile(r"卧室|床|躺|睡|午觉|眯")),
    ("kitchen", re.compile(r"厨房|吃|喝|饭|零食|咖啡|茶|饿")),
    ("fun", re.compile(r"游戏|电视|视频|番|剧|音乐")),
)
# What she does, the function name a piece of furniture must carry for it, and the room she
# goes to when no piece in the house carries it (see docs/furniture.md, 功能名).
ACTIVITIES = {
    "browse": ("browse", "web.read", "study"), "explore": ("browse", "web.read", "study"),
    "read": ("read", "web.read", "study"), "write": ("write", "web.read", "study"),
    "watch": ("watch", "video.watch", "fun"), "play": ("play", "game.play", "fun"),
    "share": ("share", "chat.telegram", "living"),
}
DIARY_NOTE = "{owner}能翻你的日记本。日记是你的隐私，{owner}每次翻了哪天的，你都会知道。"
ROOM_SIZES = {"small": (10, 10), "medium": (14, 12), "large": (18, 12)}
ROOM_NAME_MAX = 8
ROOM_CELLS = (3, 60)
MAX_FLOORS = 5
MOVES_KEPT = 50  # per member: where she went, for 「家里现在」 (docs/house-talk.md, 3c)
MOVES_TOLD = 8  # of today's, the latest she is told about


def _metres(value) -> float:
    """A height given for a piece on a wall, in metres, to the nearest 10 cm."""
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        raise PlacementError("高度要写数字（米）") from None
FLOOR_NAMES = ("一楼", "二楼", "三楼", "四楼", "五楼")  # a room or hallway is at least 3 cells across (a hallway's width), at most 60
PICTURE_TEMPLATES = ("frame", "poster", "rug")  # pieces that can show a picture (a mural is a framed one)
TILE_RANGE = (0.25, 5.0)  # units one floor tile or wallpaper image covers before it repeats
REREAD_SECONDS = 600  # opening the same day again within this is the same read


def bedroom(member: str) -> str:
    return f"bed-{member}"


@dataclass
class Place:
    room: str
    verb: str  # a key of VERBS
    detail: str = ""  # what exactly, for the owner only
    since: float = 0.0
    active: bool = False  # in the middle of an activity right now
    peer: str = ""  # the friend she went to see, or is calling
    back: str = ""  # where she came from on a visit, to return afterwards
    piece: str = ""  # the piece of furniture she is using, if any


class Home:
    def __init__(self, data_dir: Path, now: Callable[[], float] = time.time):
        self.path = data_dir / "home.json"
        self.now = now
        self.rooms: list[dict] = []
        self.where: dict[str, Place] = {}
        self.looks: dict[str, dict] = {}
        self.diary_reads: list[dict] = []
        self.pieces: list[dict] | None = None  # None until furnished
        self.types: dict[str, dict] = {}  # the owner's own kinds of furniture (stage 2d)
        self.extras: list[str] = []  # built-in pieces added to houses made before them, once each
        self.doors: list[dict] = []  # between rooms, or the front door to the outside
        self.floors: list[dict] = [{"level": 0, "name": "一楼"}]
        self.moves: dict[str, list[dict]] = {}  # member -> where she went and what she did there, latest last
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.rooms = list(raw.get("rooms", []))
                self.where = {m: Place(**p) for m, p in (raw.get("where") or {}).items()}
                self.looks = dict(raw.get("looks") or {})
                self.diary_reads = list(raw.get("diary_reads", []))
                self.pieces = list(raw["pieces"]) if "pieces" in raw else None
                self.types = dict(raw.get("types") or {})
                self.extras = list(raw.get("extras") or [])
                self.doors = list(raw.get("doors") or [])
                self.floors = list(raw.get("floors") or self.floors)
                self.moves = {m: list(v) for m, v in (raw.get("moves") or {}).items()}
            except (ValueError, TypeError) as error:
                # A broken file only loses positions and looks; start the house afresh but keep the file.
                self.path.replace(self.path.with_suffix(".broken.json"))
                self.rooms, self.where, self.looks, self.diary_reads = [], {}, {}, []
                self.pieces, self.types = None, {}
                log.warning("home.json 读不了，已改名保留：%s", error)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, {"version": 4, "rooms": self.rooms, "doors": self.doors, "floors": self.floors,
                                "pieces": self.pieces or [],
                                "types": self.types, "extras": self.extras,
                                "where": {m: asdict(p) for m, p in self.where.items()},
                                "looks": self.looks, "diary_reads": self.diary_reads[-200:], "moves": self.moves})

    # ---- who lives here ----

    def ensure(self, members: dict[str, str]) -> None:
        """Every enabled member has a bedroom, a look and a place; shared rooms always exist.

        A room made here gets its default furniture; a house from before furniture was data
        gets the whole default set once.
        """
        changed = False
        new_rooms = []
        ids = {r["id"] for r in self.rooms}
        for rid, name, icon, size, doors in SHARED_ROOMS:
            if rid not in ids:
                self.rooms.append({"id": rid, "name": name, "icon": icon, "shared": True})
                new_rooms.append(rid)
            room = next(r for r in self.rooms if r["id"] == rid)
            if "size" not in room:
                room.update(size=list(size), doors=[dict(d) for d in doors])
                changed = True
        for member, name in members.items():
            room = next((r for r in self.rooms if r["id"] == bedroom(member)), None)
            if room is None:
                room = {"id": bedroom(member), "name": f"{name}的卧室", "icon": "🛏️", "shared": False, "owner": member}
                self.rooms.append(room)
                new_rooms.append(room["id"])
            if "size" not in room:
                room.update(size=list(BEDROOM_SIZE), doors=[dict(d) for d in BEDROOM_DOORS])
                changed = True
            if member not in self.looks:
                used = {look["color"] for look in self.looks.values()}
                spare = next((c for c in SPARE_COLORS if c not in used), SPARE_COLORS[0])
                self.looks[member] = dict(DEFAULT_LOOKS.get(member) or {"color": spare, "accessory": "hat"})
                changed = True
            if member not in self.where:
                self.where[member] = Place(bedroom(member), "rest", "", self.now())
                changed = True
        if self._lay_out(new_rooms):
            changed = True
        furnish = [r for r in self.rooms if (self.pieces is None or r["id"] in new_rooms) and r.get("kind") != "hall"]
        if self.pieces is None:
            self.pieces = []
        for room in furnish:
            self.pieces.extend(furniture.default_pieces(room, "bedroom" if room["id"].startswith("bed-") else room["id"]))
        if "walls" not in self.extras:  # frames and posters that stood against a wall are hung on it
            self._hang_old_pictures()
            self.extras.append("walls")
            changed = True
        if "computer" not in self.extras:  # chat apps moved onto a computer in the living room
            if not any(p["type"] == "base/computer" for p in self.pieces):
                self._add_computer()
            self.extras.append("computer")
            changed = True
        if changed or new_rooms or furnish:
            self.save()

    def _lay_out(self, new_rooms: list[str]) -> bool:
        """Rooms without a position get one. A house from before positions is laid out the old
        automatic way once (hallways become rooms), so it looks the same; a new bedroom goes at
        the end of the bedroom row, a new shared room next to a hallway, each with a door."""
        unplaced = [r for r in self.rooms if "at" not in r]
        if not unplaced:
            return False
        if len(unplaced) == len(self.rooms):
            halls, self.doors = floorplan.legacy_layout(self.rooms)
            self.rooms.extend(halls)
            return True
        for room in unplaced:
            room.pop("doors", None)
            room.setdefault("kind", "room")
            if room["id"].startswith("bed-"):
                beds = [r for r in self.rooms if r["id"].startswith("bed-") and "at" in r and r.get("level", 0) == 0]
                x = max((floorplan.box(r)[2] for r in beds), default=0)
                room.update(at=[x, 0], level=0)
                floorplan.stretch_hall_to(room, self.rooms)
            else:
                floorplan.place_room(room, [r for r in self.rooms if "at" in r], self.doors)
            door = floorplan.auto_door(room, [r for r in self.rooms if "at" in r])
            if door:
                self.doors.append(door)
        return True

    def room_doors(self, room: dict) -> list[dict]:
        """The doors on a room's walls, as the furniture checks want them: wall and centre cell."""
        out = []
        x0, z0, x1, z1 = floorplan.box(room)
        for door in self.doors:
            if room["id"] not in floorplan.door_sides(door, self.rooms):
                continue
            half = door.get("width", floorplan.DOOR) / 2
            if door["line"] == "z":
                out.append({"wall": "back" if door["at"][1] == z0 else "front", "at": door["at"][0] - x0 + half})
            else:
                out.append({"wall": "left" if door["at"][0] == x0 else "right", "at": door["at"][1] - z0 + half})
        return out

    def checked_room(self, rid: str) -> dict:
        """A room with its doors attached, for checking furniture against them."""
        room = self.room(rid)
        return {**room, "doors": self.room_doors(room)}

    def _add_computer(self) -> None:
        rid, at, turn = furniture.COMPUTER
        piece = {"id": furniture.new_id(), "type": "base/computer", "room": rid, "at": list(at), "turn": turn,
                 "color": None, "name": None, "functions": ["chat.telegram"], "added_by": "owner", "owner": ""}
        try:
            furniture.check(piece, self.checked_room(rid), self.all_types(), self.pieces)
        except PlacementError:  # the owner put something there: wherever it fits
            spot = self.free_spot("base/computer", [rid] + [r["id"] for r in self.rooms if r.get("shared")])
            if spot is None:
                log.warning("家里没地方放电脑了")
                return
            piece.update(room=spot[0], at=list(spot[1]), turn=0)
        self.pieces.append(piece)

    # ---- furniture ----

    def all_types(self) -> dict[str, dict]:
        return {**BUILTIN_TYPES, **{tid: {**t, "mount": furniture.mount_of(t)} for tid, t in self.types.items()}}

    def room(self, rid: str) -> dict:
        found = next((r for r in self.rooms if r["id"] == rid), None)
        if found is None:
            raise PlacementError(f"没有这个房间：{rid}")
        return found

    def house_cells(self, piece: dict) -> set[tuple[int, int]]:
        room = self.room(piece["room"])
        ftype = self.all_types()[piece["type"]]
        return {(room["at"][0] + x, room["at"][1] + z) for x, z in furniture.footprint(piece, ftype)}

    def level_of(self, rid: str) -> int:
        return self.room(rid).get("level", 0)

    def stairwells(self, level: int, pieces=None, rooms=None) -> set[tuple[int, int]]:
        """The cells on a floor where stairs from the floor below come up (nothing may stand there)."""
        rooms = self.rooms if rooms is None else rooms
        by = {r["id"]: r for r in rooms}
        out = set()
        for p in self.pieces if pieces is None else pieces:
            if p["type"] == "base/stairs" and by.get(p["room"], {}).get("level", 0) == level - 1 and p["room"] in by:
                at = by[p["room"]]["at"]
                out |= {(at[0] + x, at[1] + z) for x, z in furniture.footprint(p, self.all_types()["base/stairs"])}
        return out

    def _check_floors(self, piece: dict, others: list[dict]) -> None:
        """Stairs need a room or hallway above them, clear of furniture; nothing stands over a stairwell."""
        level = self.level_of(piece["room"])
        cells = self.house_cells(piece)
        blocking = self.all_types()[piece["type"]]["blocks"]
        if blocking and level > 0 and cells & self.stairwells(level, [p for p in others if p["id"] != piece.get("id")]):
            raise PlacementError("这里是楼梯口，不能摆东西")
        if piece["type"] != "base/stairs":
            return
        if not any(f["level"] == level + 1 for f in self.floors):
            raise PlacementError("楼上还没有这一层，先加一层")
        above = [r for r in self.rooms if r.get("level", 0) == level + 1]
        if not all(any(r["at"][0] <= x < r["at"][0] + r["size"][0] and r["at"][1] <= z < r["at"][1] + r["size"][1]
                       for r in above) for x, z in cells):
            raise PlacementError("楼梯上面要有房间或走廊接着")
        for other in others:
            if other["id"] == piece.get("id") or other["room"] not in {r["id"] for r in above}:
                continue
            if self.all_types()[other["type"]]["blocks"] and self.house_cells(other) & cells:
                raise PlacementError("楼上楼梯口的位置摆着东西，先挪开")

    def _stairs_ok(self, rooms: list[dict]) -> None:
        """Every stairway still comes up inside a room (after a room upstairs moved or went)."""
        for p in self.pieces or []:
            if p["type"] != "base/stairs" or p["room"] not in {r["id"] for r in rooms}:
                continue
            level = next(r for r in rooms if r["id"] == p["room"]).get("level", 0)
            room = next(r for r in rooms if r["id"] == p["room"])
            cells = {(room["at"][0] + x, room["at"][1] + z) for x, z in furniture.footprint(p, self.all_types()["base/stairs"])}
            above = [r for r in rooms if r.get("level", 0) == level + 1]
            if not all(any(r["at"][0] <= x < r["at"][0] + r["size"][0] and r["at"][1] <= z < r["at"][1] + r["size"][1]
                           for r in above) for x, z in cells):
                raise PlacementError("这样楼梯上面就没有地方了；先挪楼梯")

    def place(self, piece: dict) -> dict:
        """Put a new piece in a room (on the floor, or on a wall when ``wall`` is given), if it fits.
        Fills in the defaults from its type."""
        ftype = self.all_types().get(piece.get("type"))
        if ftype is None:
            raise PlacementError(f"没有这种家具：{piece.get('type')}")
        base = {"id": furniture.new_id(), "color": None, "name": None,
                "functions": list(ftype["functions"]), "added_by": "owner", "owner": ""}
        if "wall" in piece:
            piece = {**base, **piece}
            piece["y"] = _metres(piece.get("y", furniture.WALL_Y.get(ftype["template"], 1.2)))
            furniture.check_wall(piece, self.checked_room(piece.get("room", "")), self.all_types(), self.pieces or [])
        else:
            if furniture.mount_of(ftype) == "wall":
                raise PlacementError(f"{ftype['name']}要挂在墙上")
            piece = {**base, "turn": 0, **piece}
            furniture.check(piece, self.checked_room(piece.get("room", "")), self.all_types(), self.pieces or [])
            self._check_floors(piece, self.pieces or [])
        self.pieces.append(piece)
        self.save()
        return piece

    def hang(self, pid: str, *, room: str | None = None, wall: str | None = None, along=None, y=None) -> dict:
        """Hang a piece on a wall, or move it along, up or down, or to another wall or room."""
        piece = self.piece(pid)
        on_wall = "wall" in piece
        ftype = self.all_types().get(piece["type"], {})
        moved = {k: v for k, v in piece.items() if k not in ("at", "turn")}
        moved.update(room=room or piece["room"], wall=wall or piece.get("wall"),
                     along=along if along is not None else piece.get("along") if on_wall else None,
                     y=_metres(y) if y is not None else piece.get("y", furniture.WALL_Y.get(ftype.get("template"), 1.2)))
        furniture.check_wall(moved, self.checked_room(moved["room"]), self.all_types(), self.pieces)
        piece.clear()
        piece.update(moved)
        self.save()
        return piece

    def move(self, pid: str, *, room: str | None = None, at=None, turn: int | None = None) -> dict:
        piece = next((p for p in self.pieces or [] if p["id"] == pid), None)
        if piece is None:
            raise PlacementError("没有这件家具")
        if "wall" in piece and turn is not None and at is None:
            raise PlacementError("挂在墙上的东西不用转：它总是朝着房间里面")
        if "wall" in piece and furniture.mount_of(self.all_types().get(piece["type"], {})) == "wall":
            raise PlacementError("这件只能挂在墙上")
        if "wall" in piece and at is None:
            raise PlacementError("放到地上要说放在哪一格")
        moved = {k: v for k, v in piece.items() if k not in ("wall", "along", "y")}  # off the wall, if it was on one
        moved.update(room=room or piece["room"], at=list(at) if at is not None else piece["at"],
                     turn=piece.get("turn", 0) if turn is None else turn)
        furniture.check(moved, self.checked_room(moved["room"]), self.all_types(), self.pieces)
        self._check_floors(moved, self.pieces)
        piece.clear()
        piece.update(moved)
        self.save()
        return piece

    def piece(self, pid: str) -> dict:
        found = next((p for p in self.pieces or [] if p["id"] == pid), None)
        if found is None:
            raise PlacementError("没有这件家具")
        return found

    def remove(self, pid: str) -> None:
        """Take a piece out of the house. Pieces that do something (carry functions) stay; clear them first."""
        piece = self.piece(pid)
        if piece["type"] == "base/parcel":
            raise PlacementError("纸箱在「工具」页面给工具选好功能后会自己拆掉")
        if piece.get("functions"):
            raise PlacementError("这件家具挂着功能，先把功能去掉再删")
        self.pieces.remove(piece)
        for place in self.where.values():
            if place.piece == pid:
                place.piece = ""
        self.save()

    def edit_piece(self, pid: str, changes: dict) -> dict:
        """The owner changes one piece: where it is, which way it faces, its colour, name, functions."""
        piece = self.piece(pid)
        if any(k in changes for k in ("wall", "along", "y")):
            self.hang(pid, room=changes.get("room"), wall=changes.get("wall"), along=changes.get("along"), y=changes.get("y"))
        elif any(k in changes for k in ("room", "at", "turn")):
            self.move(pid, room=changes.get("room"), at=changes.get("at"),
                      turn=None if changes.get("turn") is None else int(changes["turn"]))
        if "color" in changes:
            color = changes["color"]
            if color is not None and not HEX.match(str(color)):
                raise PlacementError("颜色要写成 #rrggbb")
            piece["color"] = color.lower() if color else None
        if "name" in changes:
            piece["name"] = str(changes["name"] or "").strip()[:16] or None
        if "image" in changes:
            template = self.all_types().get(piece["type"], {}).get("template")
            if template not in PICTURE_TEMPLATES:
                raise PlacementError("只有画框、海报、地毯能放图片")
            image = changes["image"]
            if image and models.image_path(self.path.parent, str(image)) is None:
                raise PlacementError("图片要先上传")
            if image:
                piece["image"] = str(image)
            else:
                piece.pop("image", None)
        if "functions" in changes:
            if piece["type"] == "base/parcel":
                raise PlacementError("纸箱的功能跟着它的工具走")
            fns = changes["functions"]
            if not isinstance(fns, list) or not all(isinstance(f, str) and furniture.FUNCTION_RE.match(f) for f in fns):
                raise PlacementError("功能名要像 video.watch 这样")
            piece["functions"] = list(dict.fromkeys(fns))
        self.save()
        return piece

    def put(self, type_id: str, room: str) -> dict:
        """Add a piece of this kind where it first fits in a room (then the owner drags it): on the floor,
        or for a kind that only hangs, on the first wall with room for it."""
        ftype = self.all_types().get(type_id)
        if ftype is not None and furniture.mount_of(ftype) == "wall":
            spot = self.free_wall_spot(type_id, room)
            if spot is None:
                raise PlacementError(f"{self.room(room)['name']}的墙上挂不下了")
            return self.place({"type": type_id, "room": room, **spot})
        spot = self.free_spot(type_id, [room])
        if spot is None:
            raise PlacementError(f"{self.room(room)['name']}里放不下了")
        return self.place({"type": type_id, "room": room, "at": list(spot[1])})

    # ---- kinds of furniture the owner makes ----

    def save_type(self, raw: dict, tid: str | None = None) -> dict:
        """Make a kind (or a copy of one, with ``copy_of``), or change one of the owner's own kinds."""
        if tid is not None and not tid.startswith("mine/"):
            raise PlacementError("内置的家具不能直接改，先复制一份")
        base = None
        if tid is not None:
            base = self.types.get(tid)
            if base is None:
                raise PlacementError("没有这种家具")
        elif raw.get("copy_of"):
            base = self.all_types().get(raw["copy_of"])
            if base is None:
                raise PlacementError("没有这种家具可以复制")
            raw = {**raw, "name": raw.get("name") or f"{base['name']}（复制）"}
        if tid is None:
            if len(self.types) >= furniture.MAX_TYPES:
                raise PlacementError("自己做的家具种类太多了")
            tid = "mine/" + furniture.new_id()[2:]
        made = furniture.make_type(raw, tid, base)
        if tid in self.types:  # a bigger kind must still fit wherever its pieces stand
            trial = {**self.all_types(), tid: made}
            for piece in [p for p in self.pieces or [] if p["type"] == tid]:
                try:
                    furniture.check(piece, self.checked_room(piece["room"]), trial, self.pieces)
                except PlacementError as e:
                    raise PlacementError(f"改了以后，{self.room(piece['room'])['name']}里的那件放不下：{e}") from None
        self.types[tid] = made
        self.save()
        return made

    def delete_type(self, tid: str) -> None:
        if tid not in self.types:
            raise PlacementError("只能删自己做的家具种类")
        if any(p["type"] == tid for p in self.pieces or []):
            raise PlacementError("家里还摆着这种家具，先把它们删掉")
        del self.types[tid]
        self.save()

    # ---- rooms the owner adds ----

    def add_room(self, name: str, size, at=None, kind: str = "room", level: int = 0) -> dict:
        """A new room or hallway: a preset size placed next to a hallway, or drawn by the owner
        (``size`` in cells and ``at``). It gets a door on a wall it shares with a neighbour."""
        if kind not in ("room", "hall"):
            raise PlacementError("只能加房间或走廊")
        name = str(name or ("走廊" if kind == "hall" else "")).strip()[:ROOM_NAME_MAX]
        if not name:
            raise PlacementError("房间要有名字")
        if isinstance(size, str):
            if size not in ROOM_SIZES:
                raise PlacementError("房间大小只能是小、中、大")
            size = ROOM_SIZES[size]
        room = {"id": ("hall-" if kind == "hall" else "room-") + furniture.new_id()[2:], "name": name,
                "icon": "🚶" if kind == "hall" else "🚪", "shared": True, "kind": kind,
                "size": self._cells(size), "level": int(level)}
        if not any(f["level"] == room["level"] for f in self.floors):
            raise PlacementError("没有这一层")
        if at is None:
            floorplan.place_room(room, self.rooms, self.doors, room["level"])
        else:
            room["at"] = self._cells(at, low=-200)
            self._check_space(room)
            if any(d.get("outside") and not floorplan.door_ok(d, self.rooms + [room]) for d in self.doors):
                raise PlacementError("会把大门堵住")
        self.rooms.append(room)
        door = floorplan.auto_door(room, self.rooms)
        if door:
            self.doors.append(door)
        self.save()
        return room

    # ---- the floor plan: rooms move, grow and shrink; doors are added and taken away ----

    @staticmethod
    def _cells(value, low: int = ROOM_CELLS[0]) -> list[int]:
        try:
            a, b = (int(v) for v in value)
        except (TypeError, ValueError):
            raise PlacementError("位置和大小要写成两个整数格") from None
        if low == ROOM_CELLS[0] and not all(ROOM_CELLS[0] <= v <= ROOM_CELLS[1] for v in (a, b)):
            raise PlacementError(f"房间每边 {ROOM_CELLS[0]} 到 {ROOM_CELLS[1]} 格")
        if low != ROOM_CELLS[0] and not all(-200 <= v <= 400 for v in (a, b)):
            raise PlacementError("位置太远了")
        return [a, b]

    def _check_space(self, room: dict) -> None:
        for other in self.rooms:
            if other["id"] != room["id"] and floorplan.overlaps(room, other):
                raise PlacementError(f"和{other['name']}叠在一起了")

    def reshape_room(self, rid: str, at=None, size=None) -> dict:
        """Move a room (its furniture goes with it) or change its size by dragging an edge
        (furniture stays where it stands; it must still fit). Doors that no longer join two
        rooms go; a room left without a door gets one where it now touches a neighbour."""
        room = self.room(rid)
        new = {**room, "at": self._cells(at, low=-200) if at is not None else list(room["at"]),
               "size": self._cells(size) if size is not None else list(room["size"])}
        self._check_space(new)
        moved = new["size"] == room["size"]
        dx, dz = new["at"][0] - room["at"][0], new["at"][1] - room["at"][1]
        pieces = {p["id"]: dict(p) for p in self.pieces or [] if p["room"] == rid}
        if not moved:  # an edge was dragged: furniture keeps its place in the house
            for piece in pieces.values():
                if "wall" in piece:  # it stays on its wall, at the same place along the house
                    piece["along"] -= dx if piece["wall"] in ("back", "front") else dz
                else:
                    piece["at"] = [piece["at"][0] - dx, piece["at"][1] - dz]
        rooms = [new if r["id"] == rid else r for r in self.rooms]
        self._stairs_ok(rooms)
        doors = []
        for door in self.doors:
            sides = floorplan.door_sides(door, self.rooms)
            if rid in sides and moved and door.get("outside"):
                door = {**door, "at": [door["at"][0] + dx, door["at"][1] + dz]}  # the front door goes along
            if floorplan.door_ok(door, rooms):
                doors.append(door)
            elif door.get("outside"):
                raise PlacementError("这样大门就不在外墙上了；先把大门挪到别的外墙")
        if not any(rid in floorplan.door_sides(d, rooms) for d in doors if not d.get("outside")):
            extra = floorplan.auto_door(new, rooms)
            if extra:
                doors.append(extra)
        trial = Home.__new__(Home)  # check the furniture against the new walls and doors
        trial.rooms, trial.doors, trial.types = rooms, doors, self.types
        trial.pieces = [pieces.get(p["id"], p) for p in self.pieces or []]
        for piece in pieces.values():
            try:
                if "wall" in piece:
                    furniture.check_wall(piece, trial.checked_room(rid), self.all_types(), trial.pieces)
                else:
                    furniture.check(piece, trial.checked_room(rid), self.all_types(), trial.pieces)
            except PlacementError as e:
                name = piece.get("name") or self.all_types().get(piece["type"], {}).get("name", "家具")
                raise PlacementError(f"{name}会放不下（{e}）；先把它挪开") from None
        room.update(at=new["at"], size=new["size"])
        for p_ in self.pieces or []:
            if p_["id"] in pieces:
                p_.update(pieces[p_["id"]])
        self.doors = doors
        self.save()
        return room

    # ---- floors ----

    def add_floor(self, name: str = "") -> dict:
        if len(self.floors) >= MAX_FLOORS:
            raise PlacementError(f"最多 {MAX_FLOORS} 层")
        level = max(f["level"] for f in self.floors) + 1
        floor = {"level": level, "name": str(name or FLOOR_NAMES[level]).strip()[:ROOM_NAME_MAX]}
        self.floors.append(floor)
        self.save()
        return floor

    def rename_floor(self, level: int, name: str) -> dict:
        floor = next((f for f in self.floors if f["level"] == level), None)
        name = str(name or "").strip()[:ROOM_NAME_MAX]
        if floor is None or not name:
            raise PlacementError("没有这一层，或者名字是空的")
        floor["name"] = name
        self.save()
        return floor

    def delete_floor(self, level: int) -> None:
        if level == 0 or level != max(f["level"] for f in self.floors):
            raise PlacementError("只能删最上面那一层，一楼不能删")
        if any(r.get("level", 0) == level for r in self.rooms):
            raise PlacementError("这一层还有房间，先删掉")
        self.floors = [f for f in self.floors if f["level"] != level]
        self.save()

    def add_door(self, line: str, at, *, outside: bool = False, level: int = 0) -> dict:
        """A door on a grid line: between the two rooms there, or (``outside``) the front door,
        which moves here if there was one."""
        if line not in ("x", "z"):
            raise PlacementError("门要开在横墙或竖墙上")
        door = {"id": "front" if outside else floorplan.new_door_id(), "level": level, "line": line,
                "at": self._cells(at, low=-200), "width": floorplan.DOOR}
        if outside:
            door["outside"] = True
        sides = floorplan.door_sides(door, self.rooms)
        if outside and (len(sides) != 1 or level != 0):
            raise PlacementError("大门要开在一楼某个房间的外墙上")
        if not outside and len(sides) != 2:
            raise PlacementError("门要开在两个房间共用的墙上，而且整扇门都要在这面墙上")
        for other in self.doors:
            if other.get("outside") and outside:
                continue
            if other["line"] == line and other.get("level", 0) == level:
                along = 0 if line == "z" else 1
                same = other["at"][1 - along] == door["at"][1 - along]
                o0, d0 = other["at"][along], door["at"][along]
                if same and o0 < d0 + door["width"] and d0 < o0 + other.get("width", floorplan.DOOR):
                    raise PlacementError("这里已经有一扇门了")
        doors = [d for d in self.doors if not (outside and d.get("outside"))] + [door]
        trial = Home.__new__(Home)
        trial.rooms, trial.doors, trial.types, trial.pieces = self.rooms, doors, self.types, self.pieces
        for rid in sides:
            for piece in [p for p in self.pieces or [] if p["room"] == rid]:
                try:
                    if "wall" in piece:
                        furniture.check_wall(piece, trial.checked_room(rid), self.all_types(), self.pieces)
                    else:
                        furniture.check(piece, trial.checked_room(rid), self.all_types(), self.pieces)
                except PlacementError:
                    name = piece.get("name") or self.all_types().get(piece["type"], {}).get("name", "家具")
                    raise PlacementError(f"门口被{name}挡着，先把它挪开") from None
        self.doors = doors
        self.save()
        return door

    def remove_door(self, did: str) -> None:
        door = next((d for d in self.doors if d["id"] == did), None)
        if door is None:
            raise PlacementError("没有这扇门")
        if door.get("outside"):
            raise PlacementError("大门不能删，只能挪到别的外墙")
        self.doors.remove(door)
        self.save()

    def surface(self, raw) -> dict | None:
        """A floor or wall look: {"color": "#rrggbb"} or {"image": <uploaded picture>, "tile": units}."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise PlacementError("地面、墙面的格式不对")
        if raw.get("image"):
            if models.image_path(self.path.parent, str(raw["image"])) is None:
                raise PlacementError("图片要先上传")
            try:
                tile = float(raw.get("tile", 1))
            except (TypeError, ValueError):
                raise PlacementError("图案大小写数字") from None
            return {"image": str(raw["image"]), "tile": round(min(TILE_RANGE[1], max(TILE_RANGE[0], tile)), 2)}
        color = str(raw.get("color") or "")
        if not HEX.match(color):
            raise PlacementError("颜色要写成 #rrggbb")
        return {"color": color.lower()}

    def dress_room(self, rid: str, changes: dict) -> dict:
        """The owner sets a room's floor and walls (``None`` puts the default back)."""
        room = self.room(rid)
        for part in ("floor", "walls"):
            if part in changes:
                look = self.surface(changes[part])
                if look is None:
                    room.pop(part, None)
                else:
                    room[part] = look
        self.save()
        return room

    def rename_room(self, rid: str, name: str) -> dict:
        room = self.room(rid)
        name = str(name or "").strip()[:ROOM_NAME_MAX]
        if not name:
            raise PlacementError("房间要有名字")
        room["name"] = name
        self.save()
        return room

    def delete_room(self, rid: str) -> None:
        room = self.room(rid)
        if rid.startswith("bed-"):
            raise PlacementError("卧室不能删")
        if rid == "living":
            raise PlacementError("客厅有大门，不能删")
        if any(p["room"] == rid for p in self.pieces or []):
            raise PlacementError("房间里还有家具，先搬走或删掉")
        if any(place.room == rid for place in self.where.values()):
            raise PlacementError("有人在这个房间里")
        self._stairs_ok([r for r in self.rooms if r["id"] != rid])
        self.doors = [d for d in self.doors if rid not in floorplan.door_sides(d, self.rooms)]
        self.rooms.remove(room)
        self.save()

    def residents(self, members) -> list[str]:
        return [m for m in members if m in self.where]

    # ---- moving with what she really does ----

    def _move(self, member: str, place: Place) -> None:
        before = self.where.get(member)
        self.where[member] = place
        if before is None or (before.room, before.verb, before.piece) != (place.room, place.verb, place.piece):
            log_ = self.moves.setdefault(member, [])  # only real changes: not an activity simply ending in place
            log_.append({"ts": place.since or self.now(), "room": place.room, "verb": place.verb, "piece": place.piece,
                         "peer": place.peer})
            del log_[:-MOVES_KEPT]
        self.save()

    def begin(self, member: str, action: str, detail: str = "", *, peer: str = "", peer_home: bool = False) -> None:
        """A daily-life activity starts: she goes where it happens."""
        here = self.where.get(member) or Place(bedroom(member), "rest")
        now = self.now()
        shut = self.shut_rooms()
        if action in ACTIVITIES:
            verb, function, fallback = ACTIVITIES[action]
            piece = self.carrier(member, function, here.room)
            if not piece and (fallback in shut or not any(r["id"] == fallback for r in self.rooms)):
                fallback = "living"
            place = Place(piece["room"] if piece else fallback, verb, detail, now, True, piece=piece["id"] if piece else "")
        elif action == "private" and peer:
            if peer_home and peer in self.where and self.where[peer].room not in shut:
                place = Place(self.where[peer].room, "visit", detail, now, True, peer, here.room)
            else:
                place = Place(here.room, "phone", detail, now, True, peer)
        else:  # resting: wherever her own reason says, or the sofa
            room = next((rid for rid, words in REST_ROOMS if words.search(detail or "")), "living")
            room = bedroom(member) if room == "bed" else room
            place = Place("living" if room in shut else room, "rest", detail, now, False)
        self._move(member, place)

    def shut_rooms(self) -> set[str]:
        """Rooms nobody can walk into from the front door (no door, or doors only to each other).
        Members are not sent there; the page marks them red."""
        return set(floorplan.unreachable(self.doors, self.rooms, self.stair_links())) if self.doors else set()

    def stair_links(self) -> list[tuple[str, str]]:
        """(room the stairs stand in, room they come up into) for every stairway."""
        out = []
        for p in self.pieces or []:
            if p["type"] != "base/stairs" or p["room"] not in {r["id"] for r in self.rooms}:
                continue
            level = self.level_of(p["room"])
            cells = self.house_cells(p)
            for r in self.rooms:
                if r.get("level", 0) == level + 1 and any(
                        r["at"][0] <= x < r["at"][0] + r["size"][0] and r["at"][1] <= z < r["at"][1] + r["size"][1] for x, z in cells):
                    out.append((p["room"], r["id"]))
        return out

    def carrier(self, member: str, function: str, here: str) -> dict | None:
        """The piece she goes to for a function: in her room if there is one, else a shared room,
        else her own bedroom. Never a piece in someone else's bedroom."""
        mine = bedroom(member)
        shut = self.shut_rooms()
        pieces = [p for p in self.pieces or [] if function in (p.get("functions") or []) and p["room"] not in shut
                  and (not p["room"].startswith("bed-") or p["room"] == mine)]
        here_level = next((r.get("level", 0) for r in self.rooms if r["id"] == here), 0)
        return min(pieces, key=lambda p: (p["room"] != here, self.level_of(p["room"]) != here_level, p["room"] == mine),
                   default=None)

    def use_tool(self, member: str, functions: list[str], tool_name: str, *, during_chat: bool = False) -> None:
        """She called a tool that does these functions: she goes to the piece that carries one.

        Called while she answers in the group, she stays there afterwards but is not busy; in a
        daily-life step the step's end settles her. Out of the house or asleep, she stays put.
        """
        here = self.where.get(member)
        if here is None or here.verb in ("out", "sleep"):
            return
        for function in functions:
            piece = self.carrier(member, function, here.room)
            if piece is None:
                continue
            verb = {"video.watch": "watch", "web.read": "read", "game.play": "play"}.get(function, "use")
            if here.piece == piece["id"] and here.verb == verb:
                return
            self._move(member, Place(piece["room"], verb, tool_name, self.now(), not during_chat, piece=piece["id"]))
            return

    def sync_parcels(self, unlinked: dict[str, str]) -> None:
        """One box in the storage room for each tool not linked to any function yet ({id: name})."""
        boxes = {p.get("tool"): p for p in self.pieces or [] if p["type"] == "base/parcel"}
        changed = False
        for tool_id, box in boxes.items():
            if tool_id not in unlinked:
                self.pieces.remove(box)
                changed = True
            elif box["name"] != f"还没拆箱：{unlinked[tool_id]}":
                box["name"] = f"还没拆箱：{unlinked[tool_id]}"
                changed = True
        for tool_id, name in unlinked.items():
            if tool_id in boxes:
                continue
            spot = self.free_spot("base/parcel", ["storage"] + [r["id"] for r in self.rooms if r.get("shared")])
            if spot is None:
                log.warning("没地方放「%s」的纸箱", name)
                continue
            self.pieces.append({"id": furniture.new_id(), "type": "base/parcel", "room": spot[0], "at": list(spot[1]),
                                "turn": 0, "color": None, "name": f"还没拆箱：{name}",
                                "functions": [furniture.unlinked_name(tool_id)], "added_by": "owner", "owner": "",
                                "tool": tool_id})
            changed = True
        if changed:
            self.save()

    def free_spot(self, type_id: str, rooms: list[str]) -> tuple[str, tuple[int, int]] | None:
        """The first cell, room by room, where a piece of this type fits without turning it."""
        for rid in dict.fromkeys(rooms):
            room = next((r for r in self.rooms if r["id"] == rid), None)
            if room is None:
                continue
            w, d = room["size"]
            for z in range(d):
                for x in range(w):
                    piece = {"id": "", "type": type_id, "room": rid, "at": [x, z], "turn": 0}
                    try:
                        furniture.check(piece, self.checked_room(rid), self.all_types(), self.pieces or [])
                        self._check_floors(piece, self.pieces or [])
                    except PlacementError:
                        continue
                    return rid, (x, z)
        return None

    def free_wall_spot(self, type_id: str, rid: str, y: float | None = None) -> dict | None:
        """The first place on a room's walls (back first) where a piece of this kind hangs at its usual height."""
        ftype = self.all_types()[type_id]
        room = self.checked_room(rid)
        y = furniture.WALL_Y.get(ftype["template"], 1.2) if y is None else y
        for wall in ("back", "left", "right", "front"):
            for along in range(furniture.wall_length(room, wall)):
                spot = {"wall": wall, "along": along, "y": y}
                try:
                    furniture.check_wall({"id": "", "type": type_id, "room": rid, **spot}, room, self.all_types(), self.pieces or [])
                except PlacementError:
                    continue
                return spot
        return None

    def _hang_old_pictures(self) -> None:
        """Frames and posters stood on the floor against a wall; now they hang on it, where they were.
        One that does not fit there goes to the nearest free place on the walls of its room; one with no
        room anywhere stays on the floor, as it was."""
        types = self.all_types()
        for piece in list(self.pieces or []):
            ftype = types.get(piece["type"])
            if not ftype or "wall" in piece or furniture.mount_of(ftype) != "wall":
                continue
            hung = furniture.onto_wall(piece, ftype)
            others = [p for p in self.pieces if p is not piece]
            try:
                furniture.check_wall(hung, self.checked_room(piece["room"]), types, others)
            except PlacementError:
                spot = self.free_wall_spot(piece["type"], piece["room"])
                if spot is None:
                    log.warning("挂不上墙，留在地上：%s", piece["id"])
                    continue
                hung.update(spot)
            piece.clear()
            piece.update(hung)

    def end(self, member: str) -> None:
        """The activity is over: she stays where she is, or goes back home from a visit."""
        place = self.where.get(member)
        if place is None or not place.active:
            return
        if place.verb == "visit" and place.back:
            self._move(member, Place(place.back, "rest", place.detail, self.now()))
        else:
            self._move(member, Place(place.room, place.verb, place.detail, place.since, False, place.peer, piece=place.piece))

    def settle(self, member: str, routine: str) -> None:
        """Follow her routine: "asleep", "busy" or "online". Safe to call on every tick."""
        place = self.where.get(member)
        if place is None:
            return
        if routine == "asleep" and place.verb != "sleep":
            self._move(member, Place(bedroom(member), "sleep", "", self.now()))
        elif routine == "busy" and place.verb != "out":
            self._move(member, Place("out", "out", "", self.now()))
        elif routine == "online" and place.verb == "sleep":
            self._move(member, Place(bedroom(member), "rest", "刚睡醒", self.now()))
        elif routine == "online" and place.verb == "out":
            self._move(member, Place("living", "rest", "刚回家", self.now()))

    # ---- looks ----

    def set_look(self, member: str, color: str, accessory: str) -> dict:
        if member not in self.looks:
            raise ValueError("没有这位成员")
        if not HEX.match(color or ""):
            raise ValueError("颜色要写成 #rrggbb")
        if accessory not in ACCESSORIES:
            raise ValueError("配饰不在清单里")
        self.looks[member] = {**self.looks[member], "color": color.lower(), "accessory": accessory}
        self.save()
        return self.looks[member]

    # ---- the diary book ----

    def read_diary(self, member: str, day: str) -> None:
        """The owner opened one day of her diary. Opening the list of days is not reading."""
        now = self.now()
        for read in reversed(self.diary_reads):
            if read["member"] == member and read["day"] == day and now - read["ts"] < REREAD_SECONDS:
                return
        self.diary_reads.append({"member": member, "day": day, "ts": now, "told_at": 0.0})
        self.save()

    def tell(self, member: str) -> str:
        """Diary reads she has not heard about yet, once: the line for her next prompt."""
        fresh = [r for r in self.diary_reads if r["member"] == member and not r["told_at"]]
        if not fresh:
            return ""
        now = self.now()
        for read in fresh:
            read["told_at"] = now
        self.save()
        days = []
        for read in fresh:
            day = datetime.fromisoformat(read["day"])
            label = f"{day.month} 月 {day.day} 日"
            if label not in days:
                days.append(label)
        when = datetime.fromtimestamp(fresh[-1]["ts"]).strftime("%m-%d %H:%M")
        return (f"[日记本] {owner.name()}翻了你 {'、'.join(days)} 的日记（最近一次是 {when}）。"
                "怎么反应随你的性格：可以不在意、害羞、说他几句，也可以以后写得收着点。只告诉你这一次。[/日记本]")

    def prompt_section(self, member: str) -> str:
        if member not in self.where:
            return ""
        shared = "、".join(r["name"] for r in self.rooms if r.get("shared") and r.get("kind") != "hall")
        return (f"[家] 你和朋友们住在同一个家里：{shared}，每人还有自己的卧室（你的叫「{self.room_name(bedroom(member))}」）。"
                f"你做日常活动时会去对应的房间，比如看东西在书房、玩游戏在娱乐室。{DIARY_NOTE.format(owner=owner.name())}[/家]")

    # ---- views ----

    def room_name(self, rid: str) -> str:
        if rid == "out":
            return "外面"
        return next((r["name"] for r in self.rooms if r["id"] == rid), rid)

    def view(self, members: dict[str, str], phone: dict[str, float] | None = None,
             available: set[str] | None = None) -> dict:
        """Everything the admin page draws. ``phone``: when each member last spoke in the group;
        ``available``: functions something can do right now (pieces carrying none are grey)."""
        phone = phone or {}
        people = []
        for member, name in members.items():
            place = self.where.get(member)
            if place is None:
                continue
            people.append({"id": member, "name": name, **asdict(place), "words": VERBS.get(place.verb, ""),
                           "room_name": self.room_name(place.room), "look": self.looks.get(member, {}),
                           "phone_at": phone.get(member, 0.0)})
        reads = [r for r in self.diary_reads if r["member"] in members][-30:]
        doors = [{**d, "rooms": floorplan.door_sides(d, self.rooms)} for d in self.doors]
        return {"rooms": self.rooms, "doors": doors, "floors": self.floors,
                "unreachable": sorted(self.shut_rooms()),
                "pieces": self.pieces or [], "types": self.all_types(),
                "available": sorted(available or ()), "function_names": furniture.FUNCTIONS,
                "templates": {k: {"name": v[0], "size": list(v[1]), "height": v[2], "color": v[3],
                                  "mount": furniture.TEMPLATE_MOUNT.get(k, "floor")}
                              for k, v in furniture.TEMPLATES.items() if k != "parcel"},
                "room_sizes": {k: list(v) for k, v in ROOM_SIZES.items()},
                "people": people, "accessories": ACCESSORIES, "verbs": VERBS,
                "diary_reads": reads, "now": self.now()}

    def house_note(self, member: str, members: dict[str, str]) -> str:
        """「家里现在」 for one member: the floors and rooms, what in each room can be used and for what,
        where everyone is, and where she has been today. All from the house as it really is; short, and
        fresh every time (docs/house-talk.md, 3c)."""
        types = self.all_types()
        rooms = [r for r in self.rooms if r.get("kind") != "hall"]
        lines = ["[家里现在]"]
        for floor in sorted(self.floors, key=lambda f: f["level"]):
            here = [r["name"] for r in rooms if r.get("level", 0) == floor["level"]]
            if here:
                lines.append(f"{floor['name']}：" + "、".join(here))
        usable = []
        for room in rooms:
            if room["id"].startswith("bed-") and room["id"] != bedroom(member):
                continue  # other people's bedrooms: only their names above
            counts: dict[str, int] = {}
            for piece in self.pieces or []:
                ftype = types.get(piece["type"])
                if piece["room"] != room["id"] or not ftype or piece["type"] == "base/parcel":
                    continue
                does = [furniture.FUNCTIONS[f] for f in piece.get("functions", []) if f in furniture.FUNCTIONS]
                if not does and not ftype.get("uses"):
                    continue  # just to look at
                label = (piece.get("name") or ftype["name"]) + (f"（{'、'.join(does)}）" if does else "")
                counts[label] = counts.get(label, 0) + 1
            if counts:
                usable.append(f"- {room['name']}：" + "、".join(k if n == 1 else f"{k}×{n}" for k, n in counts.items()))
        if usable:
            lines += ["能用的家具：", *usable]
        lines.append("大家现在：")
        for mid, name in members.items():
            place = self.where.get(mid)
            if place is None:
                continue
            who = "你" if mid == member else name
            words = VERBS.get(place.verb, "在歇着")
            lines.append(f"- {who}：出门了" if place.verb == "out" else f"- {who}：{self.room_name(place.room)} · {words}")
        midnight = time.mktime(time.localtime(self.now())[:3] + (0, 0, 0, 0, 0, -1))
        today = [m for m in self.moves.get(member, []) if m["ts"] >= midnight][-MOVES_TOLD:]
        if today:
            lines.append("你今天去过：")
            pieces = {p["id"]: p for p in self.pieces or []}
            for m in today:
                where = "外面" if m["verb"] == "out" else self.room_name(m["room"])
                piece = pieces.get(m.get("piece") or "")
                at = f"的{piece.get('name') or types.get(piece['type'], {}).get('name', '')}旁" if piece else ""
                peer = f"（找{members.get(m['peer'], m['peer'])}）" if m.get("peer") and m["verb"] in ("visit", "phone") else ""
                lines.append(f"- {time.strftime('%H:%M', time.localtime(m['ts']))} {where}{at} · {VERBS.get(m['verb'], '在歇着')}{peer}")
        lines.append("这些是家里此刻的真实情况。说到家里的事以这里为准；这里没有的，就说不知道，不要编。")
        lines.append("[/家里现在]")
        return "\n".join(lines)

    def guest_lines(self, members: dict[str, str]) -> str:
        """The short text anyone in the group may see: room and a fixed word, nothing more."""
        lines = []
        for member, name in members.items():
            place = self.where.get(member)
            if place is None:
                continue
            words = VERBS.get(place.verb, "在歇着")
            if not place.active and place.verb in ("browse", "read", "write", "watch", "play", "use", "phone", "visit", "share"):
                words = "在歇着"
            lines.append(f"{name} · 出门了" if place.verb == "out" else f"{name} · {self.room_name(place.room)} · {words}")
        return "\n".join(lines) or "家里现在没人。"
