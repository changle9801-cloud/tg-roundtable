"""Seeing pictures: one member (Codex) looks at images sent in the group and everyone gets
a short description, so members whose model cannot see images can still talk about them."""

from __future__ import annotations

from collections import OrderedDict

from .config import ParticipantConfig

CACHE_SIZE = 200


def supports_vision(pc: ParticipantConfig) -> bool:
    return pc.kind == "codex_cli"


def look_prompt(what: str, caption: str) -> str:
    said = f"，还说了：「{caption}」" if caption else ""
    return (
        f"群里有人发了这张{what}{said}。用一两句中文说清图里有什么：人物或动物、在做什么、表情神态、"
        "图里的关键文字（照抄），如果是梗图或表情包就点出梗。只描述，不评价，不超过 80 字。"
    )


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
