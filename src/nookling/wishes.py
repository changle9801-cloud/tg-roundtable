"""The wish list (docs/redecorate.md, 4c): what a member wants at home that needs the owner's yes.

A wish says what and why, and when she can put it plainly, how (``ask``): a piece for a room, a
piece moved to another room, a new room, functions for a piece, a piece thrown out or recoloured
in a shared room. The owner approves it (then it is carried out, as asked or as the owner changed
it), declines it with a reason, or, for what cannot be done here (a new ability), does it and
marks it done. She hears the answer once. Nothing here calls a model.
"""

from __future__ import annotations

import re

from . import furniture, owner
from .furniture import PlacementError
from .home import OWNER, ROOM_NAME_MAX, ROOM_SIZES, Home, bedroom

OPEN_MAX = 3  # open wishes per member
TAG_RE = re.compile(r"\[\s*心愿\s*[:：]\s*([^\]|｜\n]{2,60}?)\s*(?:[|｜]\s*([^\]\n]{0,120}?))?\s*\]")
ASKS = ("add", "move_to", "remove", "color", "name", "room", "functions")
STATUS_WORDS = {"open": "等你回", "approved": "同意了，等你去弄", "done": "弄好了", "declined": "没同意"}


def extract_wishes(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove [心愿:想要什么|为什么] tags from a reply; returns the text and the (what, why) pairs."""
    found: list[tuple[str, str]] = []

    def take(m: re.Match) -> str:
        found.append((" ".join(m.group(1).split()), " ".join((m.group(2) or "").split())))
        return " "

    plain = TAG_RE.sub(take, text)
    plain = "\n".join(line.strip() for line in re.sub(r"[ \t]{2,}", " ", plain).splitlines()).strip()
    return plain, found


def open_wishes(home: Home, member: str) -> list[dict]:
    return [w for w in home.wishes if w["member"] == member and w["status"] == "open"]


def add(home: Home, member: str, what: str, why: str = "", ask=None, source: str = "life") -> dict:
    """A new wish, if she has room for one."""
    what, why = " ".join(str(what or "").split())[:60], " ".join(str(why or "").split())[:120]
    if len(what) < 2:
        raise PlacementError("要说想要什么")
    if len(open_wishes(home, member)) >= OPEN_MAX:
        raise PlacementError(f"已经有 {OPEN_MAX} 条心愿在等主人回了")
    wish = {"id": furniture.new_id().replace("p-", "w-"), "member": member, "what": what, "why": why,
            "ask": ask if isinstance(ask, dict) and ask.get("type") in ASKS else None, "source": source,
            "status": "open", "answer": "", "result": "", "changed": False, "ts": home.now(), "answered_at": 0.0,
            "told_at": 0.0}
    home.wishes.append(wish)
    del home.wishes[:-100]
    home.save()
    return wish


def carry_out(home: Home, wish: dict, ask: dict) -> str:
    """Do what a wish asks for, through the house's usual checks; returns what was done, in words."""
    from .redecorate import _color, _label, _name, place_by_words, where_words  # the same placing as redecorating

    types = home.all_types()
    kind = ask.get("type")
    member = wish["member"]

    def piece_of() -> dict:
        piece = next((p for p in home.pieces or [] if p["id"] == str(ask.get("piece") or "")), None)
        if piece is None:
            raise PlacementError(f"没有这件家具：{ask.get('piece')}")
        return piece

    if kind == "add":
        rid, tid = str(ask.get("room") or ""), str(ask.get("kind") or "")
        ftype, room = types.get(tid), home.room(rid)
        if ftype is None or tid in ("base/stairs", "base/parcel"):
            raise PlacementError(f"没有这种家具：{tid}")
        piece = {"type": tid, "room": rid, "functions": [], "added_by": member, "owner": member if rid == bedroom(member) else "",
                 "name": _name(ask.get("name")), "color": _color(ask.get("color"))}
        piece.update(place_by_words(home, piece, ftype, ask))
        placed = home.place(piece)
        return f"在{room['name']}加了{_label(placed, ftype)}（{where_words(placed, ftype, home.room(rid))}）"
    if kind == "move_to":
        piece, rid = piece_of(), str(ask.get("room") or "")
        room, ftype = home.room(rid), types[piece["type"]]
        spot = place_by_words(home, {**piece, "room": rid}, ftype, ask, moving=piece["id"])
        home.edit_piece(piece["id"], {"room": rid, **spot})
        return f"把{_label(piece, ftype)}搬到了{room['name']}（{where_words(piece, ftype, room)}）"
    if kind == "remove":
        piece = piece_of()
        home.remove(piece["id"])
        return f"把{home.room_name(piece['room'])}的{_label(piece, types[piece['type']])}拿走了"
    if kind == "color":
        piece, color = piece_of(), _color(ask.get("color"))
        if color is None:
            raise PlacementError("要说什么颜色")
        home.edit_piece(piece["id"], {"color": color})
        return f"给{home.room_name(piece['room'])}的{_label(piece, types[piece['type']])}换了颜色（{color}）"
    if kind == "name":
        piece, name = piece_of(), _name(ask.get("name"))
        if name is None:
            raise PlacementError("要说起什么名字")
        old = _label(piece, types[piece["type"]])
        home.edit_piece(piece["id"], {"name": name})
        return f"给{home.room_name(piece['room'])}的{old}起名叫「{name}」"
    if kind == "room":
        name = str(ask.get("name") or "").strip()[:ROOM_NAME_MAX]
        size = str(ask.get("size") or "medium")
        if size not in ROOM_SIZES:
            raise PlacementError("房间大小只能是 small、medium、large")
        room = home.add_room(name, size)
        return f"加了一间{room['name']}（{room['size'][0] / 2:g} × {room['size'][1] / 2:g} 米）"
    if kind == "functions":
        piece = piece_of()
        fns = ask.get("functions")
        if not isinstance(fns, list) or not all(isinstance(f, str) and furniture.FUNCTION_RE.match(f) for f in fns):
            raise PlacementError("功能名要像 video.watch 这样")
        home.edit_piece(piece["id"], {"functions": list(dict.fromkeys([*piece.get("functions", []), *fns]))})
        return f"让{home.room_name(piece['room'])}的{_label(piece, types[piece['type']])}能{'、'.join(furniture.FUNCTIONS.get(f, f) for f in fns)}了"
    raise PlacementError("这个心愿要你自己去弄，弄好后点「做好了」")


def answer(home: Home, wid: str, decision: str, *, ask=None, note: str = "", names: dict[str, str] | None = None) -> dict:
    """The owner's answer: approve (carried out now when it can be: as asked, or as ``ask`` changes it),
    decline (``note`` says why), or done (something the owner did by hand). ``ask`` of type "manual":
    approved, and the owner will do it by hand."""
    wish = next((w for w in home.wishes if w["id"] == wid), None)
    if wish is None:
        raise PlacementError("没有这条心愿")
    note = " ".join(str(note or "").split())[:200]
    if decision == "decline":
        if wish["status"] not in ("open", "approved"):
            raise PlacementError("这条心愿已经回过了")
        if not note:
            raise PlacementError("不同意的话写一句为什么，她会知道")
        wish.update(status="declined", answer=note)
    elif decision == "approve":
        if wish["status"] != "open":
            raise PlacementError("这条心愿已经回过了")
        manual = isinstance(ask, dict) and ask.get("type") == "manual"  # the owner will do it by hand
        how = None if manual else ask if isinstance(ask, dict) and ask.get("type") in ASKS else wish.get("ask")
        if how:
            result = carry_out(home, wish, how)
            wish.update(status="done", result=result, changed=how != wish.get("ask"), answer=note)
            if how != wish.get("ask"):
                wish["ask"] = how
            who = (names or {}).get(wish["member"], wish["member"])
            home.record_change(OWNER, f"按{who}的心愿{result}", {}, kind="wish")
        else:
            wish.update(status="approved", answer=note)  # the owner will do it by hand
    elif decision == "done":
        if wish["status"] not in ("open", "approved"):
            raise PlacementError("这条心愿已经回过了")
        wish.update(status="done", answer=note or wish["answer"])
    else:
        raise PlacementError("只能同意、不同意或做好了")
    wish.update(answered_at=home.now(), told_at=0.0)
    home.save()
    return wish


def news(home: Home, member: str) -> str:
    """What became of her wishes since she last heard, once."""
    fresh = [w for w in home.wishes if w["member"] == member and w["answered_at"] and not w["told_at"]]
    if not fresh:
        return ""
    for w in fresh:
        w["told_at"] = home.now()
    home.save()
    lines = []
    for w in fresh:
        said = f"（{owner.name()}说：{w['answer']}）" if w["answer"] else ""
        if w["status"] == "declined":
            lines.append(f"你许的愿「{w['what']}」{owner.name()}没同意{said}。过几天可以换个说法再许。")
        elif w["status"] == "approved":
            lines.append(f"你许的愿「{w['what']}」{owner.name()}同意了，会去弄{said}。")
        elif w["result"]:
            lines.append(f"你许的愿「{w['what']}」{owner.name()}同意了{'（改了一点）' if w['changed'] else ''}，已经弄好：{w['result']}{said}。")
        else:
            lines.append(f"你许的愿「{w['what']}」{owner.name()}弄好了{said}。")
    return "[心愿] " + "".join(lines)


def lines_for(home: Home, member: str) -> list[str]:
    """Her wishes still waiting, for what she is shown when she thinks about the house."""
    return [f"- 「{w['what']}」（{STATUS_WORDS[w['status']]}）" for w in home.wishes
            if w["member"] == member and w["status"] in ("open", "approved")]


def view(home: Home) -> list[dict]:
    """The wish list for the page: open ones first, then the latest answered."""
    order = {"open": 0, "approved": 1}
    shown = sorted(home.wishes[-60:], key=lambda w: (order.get(w["status"], 2), -w["ts"]))
    return [{k: w[k] for k in ("id", "member", "what", "why", "ask", "status", "answer", "result", "changed", "ts", "source")}
            for w in shown]
