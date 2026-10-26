"""Seeing pictures sent in the group.

Members whose model takes images get the picture itself; the others only know a picture
was sent and can ask a friend who can see it. Optionally one member (Codex) describes every
picture for everyone first instead.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Sequence

from .config import ParticipantConfig

CACHE_SIZE = 200
IMAGE_KEEP_SECONDS = 2 * 24 * 3600  # saved pictures, for members who answer later
MAX_IMAGES = 2  # pictures handed to a model in one turn


def supports_vision(pc: ParticipantConfig) -> bool:
    """Can describe a picture for everyone in a one-off run (the "auto"/member-id mode)."""
    return pc.kind == "codex_cli"


def can_see_images(pc: ParticipantConfig) -> bool:
    """The model itself is handed the pictures sent in the group."""
    return pc.kind == "codex_cli" or (pc.kind == "api" and getattr(pc, "vision", False))


def appearance_lines(members: Sequence[ParticipantConfig]) -> str:
    lines = [f"- {m.name}：{m.appearance}" for m in members if m.appearance]
    return "群里成员的样子（看到图或别人描述图时，认得出是谁）：\n" + "\n".join(lines) if lines else ""


def sight_instructions(sees: bool, helpers: Sequence[str]) -> str:
    if sees:
        return (
            "看图：\n"
            "- 群里发的图片你能直接看到（会附在消息里）。\n"
            "- 朋友请你帮忙看图时，用一两句话说清图里有什么、是什么梗；图里的人像群里谁，就直接说是谁。"
        )
    lines = ["看图：", "- 群里发的图片你看不到，只知道有人发了图；别瞎猜图里是什么。"]
    if helpers:
        names = "、".join(f"@{n}" for n in helpers)
        lines.append(f"- 想知道图里是什么，就像朋友那样直说看不了图，@一个能看图的朋友帮忙说说：{names}。")
    else:
        lines.append("- 想知道的话，直说看不了图，请发图的人说说图里是什么。")
    return "\n".join(lines)


def look_prompt(what: str, caption: str, appearances: str = "", recent: Sequence[str] = ()) -> str:
    said = f"，还说了：「{caption}」" if caption else ""
    parts = [
        f"群里有人发了这张{what}{said}。用一两句中文说清图里有什么：人物或动物、在做什么、表情神态、"
        "图里的关键文字（照抄），如果是梗图或表情包就点出梗。只描述，不评价，不超过 80 字。"
    ]
    if appearances:
        parts.append(appearances + "\n图里的人像群里谁，就直接说是谁，并说清这张图在群里是什么意思。")
    if recent:
        parts.append("群里刚才在聊：\n" + "\n".join(recent))
    return "\n\n".join(parts)


class SeenCache:
    """Descriptions by Telegram file_unique_id, so a picture sent again is not looked at twice."""

    def __init__(self, size: int = CACHE_SIZE):
        self.size = size
        self.items: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key]
        return None

    def put(self, key: str, text: str) -> None:
        self.items[key] = text
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)
