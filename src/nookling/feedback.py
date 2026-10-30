"""The user's reactions on members' messages: likes become sample lines, dislikes a warning.

    <data_dir>/feedback.json

A 👍 (or ❤, 😂 ...) on a member's message is kept as a candidate sample line; on the admin
page the user turns good ones into that member's 示例台词. A 👎 (or 💩 ...) goes straight into the
member's prompt as something not to say again, until it is deleted or old.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError

log = logging.getLogger(__name__)

LIKES = {"👍", "❤", "❤️", "🔥", "😂", "🤣", "😁", "👏", "💯", "🥰", "😍", "🤩", "❤‍🔥", "🏆", "🎉", "😎", "🤗", "🫡", "💘", "😘", "🍓", "👌", "😇"}
DISLIKES = {"👎", "💩", "🤮", "🥱", "😐", "🤨", "😡", "🖕", "😴", "💔"}
DISLIKE_DAYS = 30  # a dislike is in the prompt this long
IN_PROMPT = 6
MAX_TEXT = 200


def plain_text(transcript: str) -> str:
    """What a member said, without the [表情:…] / [点了 …] notes and the links shared with it."""
    text = re.sub(r"\[[^\]\n]{0,80}\]", " ", transcript)
    text = re.sub(r"https?://\S+", " ", text)
    return " ".join(text.split())


def kind_of(emoji: str) -> str | None:
    if emoji in LIKES:
        return "liked"
    if emoji in DISLIKES:
        return "disliked"
    return None


@dataclass
class Feedback:
    id: str
    member: str  # member id
    text: str  # what it said
    kind: str  # liked / disliked
    emoji: str
    ts: float
    status: str = "new"  # liked: new / used (became a sample line) / ignored; disliked: new


class FeedbackStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "feedback.json"
        self.items: list[Feedback] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = [Feedback(**f) for f in raw.get("feedback", [])]
            except (OSError, ValueError, TypeError):
                log.warning("反馈记录 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"feedback": [asdict(f) for f in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def react(self, member: str, text: str, emoji: str) -> Feedback | None:
        """The user put ``emoji`` on a member's message; None if it says nothing about the message."""
        kind = kind_of(emoji)
        text = " ".join((text or "").split())[:MAX_TEXT]
        if kind is None or not text:
            return None
        same = next((f for f in self.items if f.member == member and f.text == text), None)
        if same is not None:
            if same.kind != kind:  # changed its mind
                same.kind, same.status = kind, "new"
            same.emoji, same.ts = emoji, time.time()
            self.save()
            return same
        item = Feedback(uuid.uuid4().hex[:12], member, text, kind, emoji, time.time())
        self.items.append(item)
        self.items = self.items[-300:]
        self.save()
        return item

    def unreact(self, member: str, text: str) -> bool:
        """The reaction was taken back: forget it, unless it already became a sample line."""
        text = " ".join((text or "").split())[:MAX_TEXT]
        before = len(self.items)
        self.items = [f for f in self.items if not (f.member == member and f.text == text and f.status != "used")]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def get(self, fid: str) -> Feedback:
        item = next((f for f in self.items if f.id == fid), None)
        if item is None:
            raise ConfigError("没有这条反馈")
        return item

    def mark(self, fid: str, status: str) -> Feedback:
        item = self.get(fid)
        item.status = status
        self.save()
        return item

    def delete(self, fid: str) -> None:
        self.items.remove(self.get(fid))
        self.save()

    def dislikes(self, member: str) -> list[Feedback]:
        since = time.time() - DISLIKE_DAYS * 86400
        return [f for f in self.items if f.member == member and f.kind == "disliked" and f.ts >= since][-IN_PROMPT:]

    def prompt_section(self, member: str) -> str:
        bad = self.dislikes(member)
        if not bad:
            return ""
        lines = ["用户不喜欢你说过的这些话（点了踩），以后别再这么说，类似的语气和套路也别用："]
        lines += [f"- 「{f.text}」" for f in bad]
        return "\n".join(lines)
