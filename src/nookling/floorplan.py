"""The house's floor plan: where each room is on its floor, the hallways, and the doors.

Rooms are rectangles on a grid of half-unit cells, one grid per storey (``level``; a room's
``floor`` is its floor's look): ``at`` is the top-left
cell, ``size`` the cells across and deep. Rooms on a level may touch but not overlap.
Hallways are rooms too (``kind: "hall"``). A door sits on a grid line: ``line: "z"`` is a
wall running along x (constant z), ``line: "x"`` one running along z; ``at`` is its first
cell. It joins the two rooms on either side, or a room and the outside. See docs/floorplan.md.
"""

from __future__ import annotations

import uuid
from collections import deque

HALL = 3  # cells: how wide a hallway is
DOOR = 3  # cells: how wide a door is


def new_door_id() -> str:
    return "d-" + uuid.uuid4().hex[:8]


def box(room: dict) -> tuple[int, int, int, int]:
    x, z = room["at"]
    w, d = room["size"]
    return x, z, x + w, z + d


def overlaps(a: dict, b: dict) -> bool:
    if a.get("level", 0) != b.get("level", 0):
        return False
    ax0, az0, ax1, az1 = box(a)
    bx0, bz0, bx1, bz1 = box(b)
    return ax0 < bx1 and bx0 < ax1 and az0 < bz1 and bz0 < az1


def door_sides(door: dict, rooms: list[dict]) -> list[str]:
    """The rooms a door opens into (one for the front door, two for an inside door)."""
    x, z = door["at"]
    w = door.get("width", DOOR)
    out = []
    for r in rooms:
        if r.get("level", 0) != door.get("level", 0) or "at" not in r:
            continue
        rx0, rz0, rx1, rz1 = box(r)
        if door["line"] == "z":
            if (z == rz0 or z == rz1) and rx0 <= x and x + w <= rx1:
                out.append(r["id"])
        elif (x == rx0 or x == rx1) and rz0 <= z and z + w <= rz1:
            out.append(r["id"])
    return out


def touching(door: dict, rooms: list[dict]) -> list[str]:
    """Rooms with any part of their wall where the door is (a room half in front of the
    front door blocks it as much as one right in front)."""
    x, z = door["at"]
    w = door.get("width", DOOR)
    out = []
    for r in rooms:
        if r.get("level", 0) != door.get("level", 0) or "at" not in r:
            continue
        rx0, rz0, rx1, rz1 = box(r)
        if door["line"] == "z":
            if (z == rz0 or z == rz1) and rx0 < x + w and x < rx1:
                out.append(r["id"])
        elif (x == rx0 or x == rx1) and rz0 < z + w and z < rz1:
            out.append(r["id"])
    return out


def door_ok(door: dict, rooms: list[dict]) -> bool:
    sides = door_sides(door, rooms)
    if door.get("outside"):
        return len(sides) == 1 and len(touching(door, rooms)) == 1
    return len(sides) == 2


def neighbours(doors: list[dict], rooms: list[dict]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {r["id"]: set() for r in rooms}
    for door in doors:
        sides = door_sides(door, rooms)
        if len(sides) == 2 and not door.get("outside"):
            a, b = sides
            graph[a].add(b)
            graph[b].add(a)
    return graph


def unreachable(doors: list[dict], rooms: list[dict]) -> list[str]:
    """Rooms nobody can walk into from the front door (or from the first room, with no front door)."""
    graph = neighbours(doors, rooms)
    starts = [s for d in doors if d.get("outside") for s in door_sides(d, rooms)] or [rooms[0]["id"]] if rooms else []
    seen, todo = set(starts), deque(starts)
    while todo:
        for nxt in graph.get(todo.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return [r["id"] for r in rooms if r["id"] not in seen]


# ---- the house as it was drawn before rooms had positions ----

def legacy_layout(rooms: list[dict]) -> tuple[list[dict], list[dict]]:
    """Positions for rooms laid out the old automatic way, the hallways as rooms, and the doors.

    Mirrors the old page layout (bedrooms in a row; study, a hall, the games room; kitchen,
    living room, storage, then further rooms in rows below), so the house looks the same.
    Returns (hall rooms to add, doors); the given rooms get ``at``, ``level`` and ``kind``.
    """
    by = {r["id"]: r for r in rooms}
    beds = [r for r in rooms if r["id"].startswith("bed-")]
    pos: dict[str, tuple[int, int]] = {}
    x = 0
    for r in beds:
        pos[r["id"]] = (x, 0)
        x += r["size"][0]
    depth_a = max([10] + [r["size"][1] for r in beds])
    z_b = depth_a + HALL
    hall_x, depth_b = 0, 0
    if "study" in by:
        pos["study"] = (0, z_b)
        hall_x, depth_b = by["study"]["size"][0], by["study"]["size"][1]
    fun_w = 0
    if "fun" in by:
        pos["fun"] = (hall_x + HALL, z_b)
        fun_w = by["fun"]["size"][0]
        depth_b = max(depth_b, by["fun"]["size"][1])
    z_c = z_b + depth_b + HALL
    row_c = [by[i] for i in ("kitchen", "living", "storage") if i in by]
    row_c += [r for r in rooms if r["id"] not in pos and r not in row_c]
    limit = max(40, x, hall_x + HALL + fun_w)
    rows: list[list[dict]] = [[]]
    width = 0
    for r in row_c:
        if width + r["size"][0] > limit and rows[-1]:
            rows.append([])
            width = 0
        rows[-1].append(r)
        width += r["size"][0]
    corridors = [depth_a, z_b + depth_b]  # z of each horizontal hall's top edge
    z, widest = z_c, 0
    for i, row in enumerate(rows):
        if i > 0:
            corridors.append(z)
            z += HALL
        xx, depth = 0, 0
        for r in row:
            pos[r["id"]] = (xx, z)
            xx += r["size"][0]
            depth = max(depth, r["size"][1])
        widest = max(widest, xx)
        z += depth
    total_w = max(x, hall_x + HALL + fun_w, widest)
    left = HALL if len(rows) > 1 else 0  # a side hall joined the extra rows to the others
    for r in rooms:
        px, pz = pos[r["id"]]
        r.update(at=[px + left, pz], level=0, kind="room")
    halls = [{"id": f"hall-{i + 1}", "name": "走廊", "icon": "🚶", "shared": True, "kind": "hall", "level": 0,
              "at": [left, cz], "size": [total_w, HALL]} for i, cz in enumerate(corridors)]
    if "study" in by:
        halls.append({"id": f"hall-{len(halls) + 1}", "name": "走廊", "icon": "🚶", "shared": True, "kind": "hall",
                      "level": 0, "at": [hall_x + left, z_b], "size": [HALL, depth_b]})
    doors = []
    for r in rooms:
        rx, rz = r["at"]
        w, d = r["size"]
        for old in r.pop("doors", []) or []:
            along = w if old["wall"] in ("front", "back") else d
            centre = old.get("at", along / 2)
            start = max(0, min(along - DOOR, round(centre - DOOR / 2)))
            line, at = {"front": ("z", [rx + start, rz + d]), "back": ("z", [rx + start, rz]),
                        "left": ("x", [rx, rz + start]), "right": ("x", [rx + w, rz + start])}[old["wall"]]
            door = {"id": "front" if old.get("outside") else new_door_id(), "level": 0, "line": line, "at": at, "width": DOOR}
            if old.get("outside"):
                door["outside"] = True
            doors.append(door)
    if "study" in by:  # the middle hall opens onto both corridors
        for cz in (z_b, z_b + depth_b):
            doors.append({"id": new_door_id(), "level": 0, "line": "z", "at": [hall_x + left, cz], "width": HALL, "open": True})
    if left:  # the side hall opens onto every corridor
        side = {"id": f"hall-{len(halls) + 1}", "name": "走廊", "icon": "🚶", "shared": True, "kind": "hall", "level": 0,
                "at": [0, corridors[0]], "size": [HALL, corridors[-1] + HALL - corridors[0]]}
        halls.append(side)
        for cz in corridors:
            doors.append({"id": new_door_id(), "level": 0, "line": "x", "at": [HALL, cz], "width": HALL, "open": True})
    return halls, doors


# ---- placing a new room ----

def place_room(room: dict, rooms: list[dict], doors: list[dict], level: int = 0) -> None:
    """Find a spot for a new room on a level (storey): touching a hallway if it can, else any room,
    keeping the house compact and never covering the front door. Sets ``at`` and ``level``."""
    w, d = room["size"]
    same = [r for r in rooms if r.get("level", 0) == level and "at" in r]
    if not same:
        room.update(at=[0, 0], level=level)
        return
    fronts = [door for door in doors if door.get("outside") and door.get("level", 0) == level]
    candidates = []
    for r in same:
        x0, z0, x1, z1 = box(r)
        for at in ([x0, z1], [x1, z0], [x0, z0 - d], [x0 - w, z0], [x1 - w, z1], [x0, z1 - d + 0]):
            candidates.append(at)
    hx0 = min(box(r)[0] for r in same)
    hz0 = min(box(r)[1] for r in same)
    hx1 = max(box(r)[2] for r in same)
    hz1 = max(box(r)[3] for r in same)
    best = None
    for at in candidates:
        trial = {**room, "at": at, "level": level}
        if any(overlaps(trial, r) for r in same):
            continue
        if any(touching(f, [trial]) for f in fronts):
            continue  # would wall up the front door
        beside = [r for r in same if shared_wall(trial, r) >= DOOR]
        if not beside:
            continue
        grow = (max(hx1, at[0] + w) - min(hx0, at[0])) * (max(hz1, at[1] + d) - min(hz0, at[1]))
        score = (not any(r.get("kind") == "hall" for r in beside), grow, at[1], at[0])
        if best is None or score < best[0]:
            best = (score, at)
    at = best[1] if best else [hx0, hz1 + HALL]  # nowhere touching: below the house (a door is up to the owner)
    room.update(at=list(at), level=level)


def shared_wall(a: dict, b: dict) -> int:
    """How many cells of wall two rooms on a level share (0 if they do not touch)."""
    if a.get("level", 0) != b.get("level", 0):
        return 0
    ax0, az0, ax1, az1 = box(a)
    bx0, bz0, bx1, bz1 = box(b)
    if az1 == bz0 or bz1 == az0:
        return max(0, min(ax1, bx1) - max(ax0, bx0))
    if ax1 == bx0 or bx1 == ax0:
        return max(0, min(az1, bz1) - max(az0, bz0))
    return 0


def auto_door(room: dict, rooms: list[dict]) -> dict | None:
    """A door in the middle of the longest wall a room shares with a neighbour, a hallway first."""
    best = None
    for r in rooms:
        if r["id"] == room["id"]:
            continue
        length = shared_wall(room, r)
        if length < DOOR:
            continue
        key = (r.get("kind") != "hall", -length)
        if best is None or key < best[0]:
            best = (key, r)
    if best is None:
        return None
    other = best[1]
    ax0, az0, ax1, az1 = box(room)
    bx0, bz0, bx1, bz1 = box(other)
    if az1 == bz0 or bz1 == az0:
        lo, hi = max(ax0, bx0), min(ax1, bx1)
        return {"id": new_door_id(), "level": room.get("level", 0), "line": "z",
                "at": [lo + (hi - lo - DOOR) // 2, az1 if az1 == bz0 else az0], "width": DOOR}
    lo, hi = max(az0, bz0), min(az1, bz1)
    return {"id": new_door_id(), "level": room.get("level", 0), "line": "x",
            "at": [ax1 if ax1 == bx0 else ax0, lo + (hi - lo - DOOR) // 2], "width": DOOR}


def stretch_hall_to(room: dict, rooms: list[dict]) -> None:
    """A new bedroom at the end of the bedroom row: lengthen the hallway in front of it to reach."""
    x0, z0, x1, z1 = box(room)
    for r in rooms:
        if r.get("kind") == "hall" and r.get("level", 0) == room.get("level", 0) and r["at"][1] == z1:
            hx0, hz0, hx1, hz1 = box(r)
            if hx0 <= x0 < hx1 + 1 and x1 > hx1:
                r["size"][0] = x1 - hx0
                return
