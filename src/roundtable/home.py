"""The members' home: its rooms, where each member is and what she is doing, and diary reads.

    <data_dir>/home.json  {"rooms": [...], "where": {member: Place}, "looks": {member: {...}},
                           "diary_reads": [{member, day, ts, told_at}]}

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

from .games.spire_bridge import atomic_json

log = logging.getLogger(__name__)

SHARED_ROOMS = (  # id, default name, icon
    ("living", "客厅", "🛋️"),
    ("kitchen", "厨房", "🍳"),
    ("study", "书房", "📚"),
    ("fun", "娱乐室", "🎮"),
    ("storage", "储物间", "📦"),
)
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
    "browse": "在看东西", "read": "在看东西", "write": "在写东西", "play": "在玩游戏",
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
DIARY_NOTE = "阿明能翻你的日记本。日记是你的隐私，他每次翻了哪天的，你都会知道。"
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


class Home:
    def __init__(self, data_dir: Path, now: Callable[[], float] = time.time):
        self.path = data_dir / "home.json"
        self.now = now
        self.rooms: list[dict] = []
        self.where: dict[str, Place] = {}
        self.looks: dict[str, dict] = {}
        self.diary_reads: list[dict] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.rooms = list(raw.get("rooms", []))
                self.where = {m: Place(**p) for m, p in (raw.get("where") or {}).items()}
                self.looks = dict(raw.get("looks") or {})
                self.diary_reads = list(raw.get("diary_reads", []))
            except (ValueError, TypeError) as error:
                # A broken file only loses positions and looks; start the house afresh but keep the file.
                self.path.replace(self.path.with_suffix(".broken.json"))
                self.rooms, self.where, self.looks, self.diary_reads = [], {}, {}, []
                log.warning("home.json 读不了，已改名保留：%s", error)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, {"version": 1, "rooms": self.rooms,
                                "where": {m: asdict(p) for m, p in self.where.items()},
                                "looks": self.looks, "diary_reads": self.diary_reads[-200:]})

    # ---- who lives here ----

    def ensure(self, members: dict[str, str]) -> None:
        """Every enabled member has a bedroom, a look and a place; shared rooms always exist."""
        changed = False
        ids = {r["id"] for r in self.rooms}
        for rid, name, icon in SHARED_ROOMS:
            if rid not in ids:
                self.rooms.append({"id": rid, "name": name, "icon": icon, "shared": True})
                changed = True
        for member, name in members.items():
            room = next((r for r in self.rooms if r["id"] == bedroom(member)), None)
            if room is None:
                self.rooms.append({"id": bedroom(member), "name": f"{name}的卧室", "icon": "🛏️",
                                   "shared": False, "owner": member})
                changed = True
            if member not in self.looks:
                used = {look["color"] for look in self.looks.values()}
                spare = next((c for c in SPARE_COLORS if c not in used), SPARE_COLORS[0])
                self.looks[member] = dict(DEFAULT_LOOKS.get(member) or {"color": spare, "accessory": "hat"})
                changed = True
            if member not in self.where:
                self.where[member] = Place(bedroom(member), "rest", "", self.now())
                changed = True
        if changed:
            self.save()

    def residents(self, members) -> list[str]:
        return [m for m in members if m in self.where]

    # ---- moving with what she really does ----

    def _move(self, member: str, place: Place) -> None:
        self.where[member] = place
        self.save()

    def begin(self, member: str, action: str, detail: str = "", *, peer: str = "", peer_home: bool = False) -> None:
        """A daily-life activity starts: she goes where it happens."""
        here = self.where.get(member) or Place(bedroom(member), "rest")
        now = self.now()
        if action in ("browse", "explore", "read"):
            place = Place("study", "read" if action == "read" else "browse", detail, now, True)
        elif action == "write":
            place = Place("study", "write", detail, now, True)
        elif action == "play":
            place = Place("fun", "play", detail, now, True)
        elif action == "share":
            place = Place("living", "share", detail, now, True)
        elif action == "private" and peer:
            if peer_home and peer in self.where:
                place = Place(self.where[peer].room, "visit", detail, now, True, peer, here.room)
            else:
                place = Place(here.room, "phone", detail, now, True, peer)
        else:  # resting: wherever her own reason says, or the sofa
            room = next((rid for rid, words in REST_ROOMS if words.search(detail or "")), "living")
            place = Place(bedroom(member) if room == "bed" else room, "rest", detail, now, False)
        self._move(member, place)

    def end(self, member: str) -> None:
        """The activity is over: she stays where she is, or goes back home from a visit."""
        place = self.where.get(member)
        if place is None or not place.active:
            return
        if place.verb == "visit" and place.back:
            self._move(member, Place(place.back, "rest", place.detail, self.now()))
        else:
            self._move(member, Place(place.room, place.verb, place.detail, place.since, False, place.peer))

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
        return (f"[日记本] 阿明翻了你 {'、'.join(days)} 的日记（最近一次是 {when}）。"
                "怎么反应随你的性格：可以不在意、害羞、说他几句，也可以以后写得收着点。只告诉你这一次。[/日记本]")

    def prompt_section(self, member: str) -> str:
        if member not in self.where:
            return ""
        shared = "、".join(r["name"] for r in self.rooms if r.get("shared"))
        return (f"[家] 你和朋友们住在同一个家里：{shared}，每人还有自己的卧室（你的叫「{self.room_name(bedroom(member))}」）。"
                f"你做日常活动时会去对应的房间，比如看东西在书房、玩游戏在娱乐室。{DIARY_NOTE}[/家]")

    # ---- views ----

    def room_name(self, rid: str) -> str:
        if rid == "out":
            return "外面"
        return next((r["name"] for r in self.rooms if r["id"] == rid), rid)

    def view(self, members: dict[str, str], phone: dict[str, float] | None = None) -> dict:
        """Everything the admin page draws. ``phone``: when each member last spoke in the group."""
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
        return {"rooms": self.rooms, "people": people, "accessories": ACCESSORIES, "verbs": VERBS,
                "diary_reads": reads, "now": self.now()}

    def guest_lines(self, members: dict[str, str]) -> str:
        """The short text anyone in the group may see: room and a fixed word, nothing more."""
        lines = []
        for member, name in members.items():
            place = self.where.get(member)
            if place is None:
                continue
            words = VERBS.get(place.verb, "在歇着")
            if not place.active and place.verb in ("browse", "read", "write", "play", "phone", "visit", "share"):
                words = "在歇着"
            lines.append(f"{name} · 出门了" if place.verb == "out" else f"{name} · {self.room_name(place.room)} · {words}")
        return "\n".join(lines) or "家里现在没人。"
