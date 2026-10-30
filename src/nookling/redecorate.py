"""The family changing the house themselves (docs/redecorate.md, 4a).

She says in words what she wants to change in her own bedroom; this finds the cells, makes the
change through the same checks as the owner's (it fits, keeps doors clear, ...), and writes it into
「家里的变化」 so everyone hears of it. Nothing here calls a model.
"""

from __future__ import annotations

import math
import re

from . import furniture
from .furniture import PlacementError
from .home import Home, bedroom

WEEK = 7 * 86400
PER_WEEK = 3  # times a week she may redecorate (wishes do not count)
PER_TIME = 3  # small changes at a time
BEDROOM_MAX = 16  # pieces in her bedroom
NAME_MAX = 8
NOT_HERS = {"base/stairs", "base/parcel"}  # kinds she cannot add
HANG = {"high": 1.7, "middle": 1.2, "low": 0.6}  # metres, for "hang"
WALL_WORDS = {"back": "后墙", "front": "前墙", "left": "左墙", "right": "右墙"}
FACE_TURN = {"front": 0, "left": 90, "back": 180, "right": 270}  # facing that way, by the room
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def left_this_week(home: Home, member: str) -> int:
    now = home.now()
    return PER_WEEK - sum(1 for ts in home.redecorations.get(member, []) if now - ts < WEEK)


def _label(piece: dict, ftype: dict) -> str:
    return piece.get("name") or ftype["name"]


def where_words(piece: dict, ftype: dict, room: dict) -> str:
    """Where a piece is in its room, in words: which walls it is against, which side, which way it faces."""
    if "wall" in piece:
        return f"挂在{WALL_WORDS[piece['wall']]}上，离地 {piece['y']} 米"
    cols, rows = room["size"]
    w, d = furniture.turned_size(ftype["size"], piece["turn"])
    x, z = piece["at"]
    near = 1  # a cell's gap from a wall still reads as against it
    touch = [name for name, ok in (("后墙", z <= near), ("前墙", rows - z - d <= near), ("左墙", x <= near), ("右墙", cols - x - w <= near)) if ok]
    cx, cz = x + w / 2, z + d / 2
    side = "左边" if cx < cols / 3 else "右边" if cx > cols * 2 / 3 else "中间"
    depth = "靠里" if cz < rows / 3 else "靠外" if cz > rows * 2 / 3 else "中间"
    facing = {0: "朝前", 90: "朝左", 180: "朝后", 270: "朝右"}[piece["turn"]]
    return f"{'靠' + '、'.join(touch) if touch else '房间' + depth}，{side}，{facing}"


def menu(home: Home, member: str) -> str:
    """What she sees when she wants to change something at home: her room piece by piece, what she
    may add, and how to say it. Empty when she has no redecorating left this week."""
    left = left_this_week(home, member)
    rid = bedroom(member)
    room = next((r for r in home.rooms if r["id"] == rid), None)
    if left <= 0 or room is None:
        return ""
    types = home.all_types()
    cols, rows = room["size"]
    doors = "、".join(dict.fromkeys(WALL_WORDS[d["wall"]] for d in home.room_doors(room))) or "（没有门）"
    lines = [f"[改改家里] 这周还能改 {left} 次；这次最多 {PER_TIME} 件小改动。",
             f"你的卧室「{room['name']}」：{cols / 2:g} × {rows / 2:g} 米，门在{doors}。里面现在有："]
    for p in home.pieces or []:
        t = types.get(p["type"])
        if p["room"] == rid and t:
            extra = "，带功能（可以挪，不能扔）" if p.get("functions") else ""
            lines.append(f"- {p['id']}：{_label(p, t)}（{t['name']}）{where_words(p, t, room)}{extra}")
    lines.append("能加的东西（种类 ID：名字，占地格数，放法）：")
    mounts = {"floor": "放地上", "wall": "挂墙", "either": "放地上或挂墙"}
    for tid, t in types.items():
        if tid not in NOT_HERS:
            lines.append(f"- {tid}：{t['name']}，{t['size'][0]}×{t['size'][1]} 格，{mounts[furniture.mount_of(t)]}")
    lines.append(f"""规矩：只能改你自己的卧室。你加的东西不带功能（想要能用的东西，以后许愿）。卧室最多 {BEDROOM_MAX} 件；名字最长 {NAME_MAX} 个字；床不能全扔掉。
放在哪用话说：where 里写 against（back 后墙 / front 前墙 / left 左墙 / right 右墙 / middle 房间中间）和 near（挨着哪件东西，写它的 ID，可空）；挂墙的东西另写 hang（high / middle / low）。靠墙放的会朝着房间里面；也可以写 face：朝哪件东西（ID），或 front / back / left / right。
真想改的时候才改，不用每次都改满。只输出 JSON：{{"changes": [...], "why": "为什么这样改"}}，changes 里每一项是下面之一：
{{"do":"add","kind":"种类 ID","name":"可空","color":"#rrggbb，可空","where":{{"against":"back","near":""}},"hang":"","face":""}}
{{"do":"move","piece":"ID","where":{{"against":"left","near":""}},"hang":"","face":""}}
{{"do":"turn","piece":"ID","face":"朝哪件东西的 ID，或 front / back / left / right"}}
{{"do":"color","piece":"ID","color":"#rrggbb"}}
{{"do":"name","piece":"ID","name":"新名字"}}
{{"do":"remove","piece":"ID"}}
{{"do":"room_name","name":"卧室的新名字"}}
{{"do":"room_color","part":"floor 或 walls","color":"#rrggbb"}}
改了主意就输出 {{"changes": [], "why": "..."}}。""")
    return "\n".join(lines)


def _name(value) -> str | None:
    name = str(value or "").strip()
    if len(name) > NAME_MAX:
        raise PlacementError(f"名字最长 {NAME_MAX} 个字：{name}")
    return name or None


def _color(value) -> str | None:
    if value in (None, ""):
        return None
    if not HEX.match(str(value)):
        raise PlacementError(f"颜色要写成 #rrggbb：{value}")
    return str(value).lower()


def _centre(piece: dict, ftype: dict, room: dict) -> tuple[float, float]:
    """A piece's middle, in cells from its room's corner (one on a wall: on the wall's line)."""
    if "wall" in piece:
        along = piece["along"] + ftype["size"][0] / 2
        cols, rows = room["size"]
        return {"back": (along, 0), "front": (along, rows), "left": (0, along), "right": (cols, along)}[piece["wall"]]
    w, d = furniture.turned_size(ftype["size"], piece["turn"])
    return piece["at"][0] + w / 2, piece["at"][1] + d / 2


def _turn_towards(frm: tuple[float, float], to: tuple[float, float]) -> int:
    vx, vz = to[0] - frm[0], to[1] - frm[1]
    return max(FACE_TURN.values(), key=lambda a: -math.sin(math.radians(a)) * vx + math.cos(math.radians(a)) * vz)


def place_by_words(home: Home, piece: dict, ftype: dict, change: dict, moving: str = "") -> dict:
    """Cells (or a place on a wall) for a piece, from her words: against which wall or in the middle, next
    to which piece, how high, which way it faces. The nearest place that fits; none: PlacementError."""
    room = home.checked_room(piece["room"])
    types = home.all_types()
    others = [p for p in home.pieces or [] if p["id"] != moving]
    where = change.get("where") if isinstance(change.get("where"), dict) else {}
    against, hang, face = str(where.get("against") or ""), str(change.get("hang") or ""), str(change.get("face") or "")
    near = next((p for p in others if p["id"] == str(where.get("near") or "") and p["room"] == piece["room"]), None)
    target = near or next((p for p in others if p["id"] == face and p["room"] == piece["room"]), None)
    near_at = _centre(near, types[near["type"]], room) if near else None
    face_at = _centre(target, types[target["type"]], room) if target and face == target["id"] else None
    mount = furniture.mount_of(ftype)
    cols, rows = room["size"]
    best, score = None, math.inf
    if mount == "wall" or (mount == "either" and hang in HANG):
        y0 = HANG.get(hang, furniture.WALL_Y.get(ftype["template"], 1.2))
        top = max(0.0, furniture.WALL_TALL - ftype["height"])
        for wall in [against] if against in WALL_WORDS else list(WALL_WORDS):
            n = furniture.wall_length(room, wall)
            for along in range(n - ftype["size"][0] + 1):
                for dy in (0, -0.3, 0.3, -0.6, 0.6):
                    y = round(min(top, max(0.0, y0 + dy)), 1)
                    spot = {"wall": wall, "along": along, "y": y}
                    centre = _centre(spot, ftype, room)
                    s = abs(y - y0) * 10 + (math.dist(centre, near_at) if near_at else abs(along + ftype["size"][0] / 2 - n / 2) * 0.1)
                    if s >= score:
                        continue
                    try:
                        furniture.check_wall({**piece, **spot}, room, types, others)
                    except PlacementError:
                        continue
                    best, score = spot, s
    else:
        turns = [{"back": 0, "front": 180, "left": 270, "right": 90}[against]] if against in WALL_WORDS \
            else [FACE_TURN[face]] if face in FACE_TURN else [0, 90, 180, 270]
        for turn in turns:
            w, d = furniture.turned_size(ftype["size"], turn)
            for z in range(rows - d + 1):
                for x in range(cols - w + 1):
                    centre = (x + w / 2, z + d / 2)
                    if face_at and _turn_towards(centre, face_at) != turn and not against:
                        continue  # it has to look at what she said
                    gap = {"back": z, "front": rows - z - d, "left": x, "right": cols - x - w}
                    s = gap[against] * 3 if against in gap else math.dist(centre, (cols / 2, rows / 2)) if against == "middle" \
                        else min(gap.values()) * 2
                    if near_at:
                        s += math.dist(centre, near_at)
                    if s >= score:
                        continue
                    spot = {"at": [x, z], "turn": turn}
                    try:
                        furniture.check({**piece, **spot}, room, types, others)
                        home._check_floors({**piece, **spot}, others)
                    except PlacementError:
                        continue
                    best, score = spot, s
    if best is None:
        raise PlacementError(f"{room['name']}里{'靠' + WALL_WORDS[against] if against in WALL_WORDS else ''}放不下{_label(piece, ftype)}了")
    return best


def apply(home: Home, member: str, plan: dict) -> dict:
    """Her plan for her bedroom, change by change. What was done goes into 「家里的变化」; what could not be
    done comes back with the reason. Counts as one of her redecorations this week when anything was done."""
    changes = plan.get("changes") if isinstance(plan, dict) else None
    if not isinstance(changes, list):
        raise PlacementError("没说要改什么")
    if left_this_week(home, member) <= 0:
        raise PlacementError(f"这周已经改过 {PER_WEEK} 次了")
    done, refused = [], []
    for change in changes[:PER_TIME]:
        try:
            text, undo = _one(home, member, change if isinstance(change, dict) else {})
        except PlacementError as e:
            refused.append(str(e))
            continue
        done.append(text)
        home.record_change(member, text, undo)
    if len(changes) > PER_TIME:
        refused.append(f"一次最多改 {PER_TIME} 件，后面的没做")
    if done:
        home.redecorations.setdefault(member, []).append(home.now())
        del home.redecorations[member][:-10]
        home.save()
    return {"done": done, "refused": refused}


def _one(home: Home, member: str, change: dict) -> tuple[str, dict]:
    """One change in her bedroom: (what she did, in words, said of her; how to undo it)."""
    rid = bedroom(member)
    room = home.room(rid)
    types = home.all_types()
    do = str(change.get("do") or "")

    def hers() -> dict:
        piece = next((p for p in home.pieces or [] if p["id"] == str(change.get("piece") or "")), None)
        if piece is None or piece["room"] != rid:
            raise PlacementError(f"{change.get('piece')} 不在你的卧室里")
        return piece

    if do == "add":
        tid = str(change.get("kind") or "")
        ftype = types.get(tid)
        if ftype is None or tid in NOT_HERS:
            raise PlacementError(f"没有这种东西可以加：{tid}")
        if sum(1 for p in home.pieces or [] if p["room"] == rid) >= BEDROOM_MAX:
            raise PlacementError(f"卧室里已经有 {BEDROOM_MAX} 件了，先扔点东西")
        piece = {"type": tid, "room": rid, "functions": [], "added_by": member, "owner": member,
                 "name": _name(change.get("name")), "color": _color(change.get("color"))}
        piece.update(place_by_words(home, piece, ftype, change))
        placed = home.place(piece)
        return f"在自己卧室加了{_label(placed, ftype)}（{where_words(placed, ftype, room)}）", {"added": placed["id"]}
    if do == "move":
        piece = hers()
        ftype = types[piece["type"]]
        before = dict(piece)
        spot = place_by_words(home, piece, ftype, change, moving=piece["id"])
        home.edit_piece(piece["id"], spot)
        return f"把卧室里的{_label(piece, ftype)}挪到了{where_words(piece, ftype, room)}", {"piece": before}
    if do == "turn":
        piece = hers()
        ftype = types[piece["type"]]
        if "wall" in piece:
            raise PlacementError("挂在墙上的东西不用转")
        face = str(change.get("face") or "")
        target = next((p for p in home.pieces or [] if p["id"] == face and p["room"] == rid), None)
        turn = FACE_TURN.get(face) if target is None else _turn_towards(_centre(piece, ftype, room), _centre(target, types[target["type"]], room))
        if turn is None:
            raise PlacementError("要说朝哪：一件东西的 ID，或 front / back / left / right")
        before = dict(piece)
        home.edit_piece(piece["id"], {"turn": turn})
        return f"把卧室里的{_label(piece, ftype)}转了个方向（{where_words(piece, ftype, room)}）", {"piece": before}
    if do == "color":
        piece = hers()
        color = _color(change.get("color"))
        if color is None:
            raise PlacementError("要说换成什么颜色")
        before = dict(piece)
        home.edit_piece(piece["id"], {"color": color})
        return f"给卧室里的{_label(piece, types[piece['type']])}换了个颜色（{color}）", {"piece": before}
    if do == "name":
        piece = hers()
        name = _name(change.get("name"))
        if name is None:
            raise PlacementError("要说起什么名字")
        before = dict(piece)
        old = _label(piece, types[piece["type"]])
        home.edit_piece(piece["id"], {"name": name})
        return f"给卧室里的{old}起名叫「{name}」", {"piece": before}
    if do == "remove":
        piece = hers()
        ftype = types[piece["type"]]
        beds = [p for p in home.pieces or [] if p["room"] == rid and types.get(p["type"], {}).get("template") == "bed"]
        if ftype["template"] == "bed" and len(beds) <= 1:
            raise PlacementError("床不能扔：要睡觉")
        before = dict(piece)
        home.remove(piece["id"])
        return f"把卧室里的{_label(piece, ftype)}扔掉了", {"piece": before, "removed": True}
    if do == "room_name":
        name = _name(change.get("name"))
        if name is None:
            raise PlacementError("要说改成什么名字")
        old = room["name"]
        home.rename_room(rid, name)
        return f"把自己的卧室改名叫「{name}」", {"room": rid, "name": old}
    if do == "room_color":
        part = str(change.get("part") or "")
        color = _color(change.get("color"))
        if part not in ("floor", "walls") or color is None:
            raise PlacementError("要说换地板还是墙（floor / walls），换成什么颜色")
        old = room.get(part)
        home.dress_room(rid, {part: {"color": color}})
        return f"把卧室的{'地板' if part == 'floor' else '墙'}换成了 {color}", {"room": rid, "part": part, "look": old}
    raise PlacementError(f"不认识这种改法：{do}")
