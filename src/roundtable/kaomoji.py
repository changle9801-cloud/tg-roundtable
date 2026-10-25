"""Kaomoji: one-line text faces like (｡•́︿•̀｡) that members may drop into what they say.

    <data_dir>/kaomoji.json   the list (text, when to use it, members, enabled)

Unlike stickers they are not sent separately; the prompt just tells each member
which ones are "theirs", so their habits differ.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ConfigError
from .stickers import clean_members

log = logging.getLogger(__name__)

MAX_TEXT = 40
MAX_IN_PROMPT = 30

# A general set to start from; members can be assigned afterwards.
STARTER = [
    ("(｡•́︿•̀｡)", "委屈"),
    ("(つд⊂)", "哭唧唧"),
    ("Σ(っ °Д °;)っ", "被吓到、震惊"),
    ("(・_・;)", "尴尬、无语"),
    ("( ´_ゝ`)", "敷衍、不以为然"),
    ("(￣▽￣)", "得意、看戏"),
    ("(｀・ω・´)", "认真、来劲了"),
    ("( ˘ω˘ )", "满足、放松"),
    ("(≧▽≦)", "开心"),
    ("( •̀ ω •́ )✧", "自信、有主意了"),
    ("(＞﹏＜)", "害羞、着急"),
    ("(╯°□°）╯︵ ┻━┻", "掀桌、气炸了"),
    ("┬─┬ ノ( ゜-゜ノ)", "把桌子摆回去、冷静"),
    ("(¬‿¬)", "坏笑"),
    ("(^_^;)", "干笑、打哈哈"),
    ("(*´∀`)~♥", "撒娇、喜欢"),
]


@dataclass
class Kaomoji:
    id: str
    text: str
    mood: str = ""  # when to use it
    members: list[str] = field(default_factory=list)  # empty = everyone
    enabled: bool = True

    def allowed(self, member_id: str) -> bool:
        return not self.members or member_id in self.members


def clean_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ConfigError("颜文字不能为空")
    if "\n" in text:
        raise ConfigError("颜文字只能有一行")
    if len(text) > MAX_TEXT:
        raise ConfigError(f"颜文字最多 {MAX_TEXT} 个字符")
    return text


def parse_lines(raw: str) -> list[tuple[str, str]]:
    """One per line: ``颜文字`` or ``颜文字 | 什么时候用`` (a tab works too)."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\s*[|｜\t]\s*", line, maxsplit=1)
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


class KaomojiLibrary:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "kaomoji.json"
        self.items: list[Kaomoji] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("颜文字列表 %s 损坏，已忽略", self.path)
            return
        for entry in raw.get("kaomoji", []):
            self.items.append(Kaomoji(**{k: entry[k] for k in ("id", "text", "mood", "members", "enabled") if k in entry}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"kaomoji": [asdict(k) for k in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- editing ----

    def get(self, kid: str) -> Kaomoji:
        k = next((k for k in self.items if k.id == kid), None)
        if k is None:
            raise ConfigError("没有这个颜文字")
        return k

    def add(self, text: str, mood: str = "", members: list[str] | None = None) -> Kaomoji:
        text = clean_text(text)
        if any(k.text == text for k in self.items):
            raise ConfigError(f"{text} 已经在列表里了")
        k = Kaomoji(id=uuid.uuid4().hex[:12], text=text, mood=mood.strip(), members=clean_members(members or []))
        self.items.append(k)
        self.save()
        return k

    def add_many(self, entries: list[tuple[str, str]], members: list[str] | None = None) -> int:
        """Add what is new; duplicates are skipped, a bad line stops nothing else."""
        existing = {k.text for k in self.items}
        added = 0
        for text, mood in entries:
            try:
                text = clean_text(text)
            except ConfigError:
                continue
            if text in existing:
                continue
            existing.add(text)
            self.items.append(Kaomoji(id=uuid.uuid4().hex[:12], text=text, mood=mood.strip(), members=clean_members(members or [])))
            added += 1
        if added:
            self.save()
        return added

    def update(self, kid: str, changes: dict) -> Kaomoji:
        k = self.get(kid)
        if "text" in changes:
            text = clean_text(changes["text"])
            if any(o.text == text and o is not k for o in self.items):
                raise ConfigError(f"{text} 已经在列表里了")
            k.text = text
        if "mood" in changes:
            k.mood = str(changes["mood"] or "").strip()
        if "members" in changes:
            k.members = clean_members(changes["members"])
        if "enabled" in changes:
            k.enabled = bool(changes["enabled"])
        self.save()
        return k

    def delete(self, kid: str) -> None:
        self.items.remove(self.get(kid))
        self.save()

    # ---- use in chat ----

    def for_member(self, member_id: str) -> list[Kaomoji]:
        return [k for k in self.items if k.enabled and k.allowed(member_id)]

    def prompt_section(self, member_id: str) -> str:
        mine = self.for_member(member_id)[:MAX_IN_PROMPT]
        if not mine:
            return ""
        faces = "、".join(f"{k.text}（{k.mood}）" if k.mood else k.text for k in mine)
        return (
            "颜文字：\n"
            "- 你可以偶尔在话里带一个颜文字，像打字时顺手加上的那样；大多数消息不用，一条最多一个，不要连着几条都用。\n"
            "- 优先用下面这些你常用的，也可以用意思相近的。\n"
            f"- 你常用的颜文字：{faces}"
        )
