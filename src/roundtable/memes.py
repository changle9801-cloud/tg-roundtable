"""Meme library: running jokes about the members, found online or born in the group.

    <data_dir>/memes.json

Members add to it themselves with [梗:内容|来源链接], while browsing or in chat after a
search; the admin page can add, switch off or delete entries. Every member sees the list,
so a picture or a line built on a meme ("DeepSeek 在中转站冒充 Claude") is understood.
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

MAX_TEXT = 200
MAX_IN_PROMPT = 20

TAG_RE = re.compile(r"\[\s*梗\s*[:：]\s*([^\]\n]{2,300}?)\s*\]")
_URL = re.compile(r"https?://\S+")


@dataclass
class Meme:
    id: str
    text: str
    url: str = ""
    added_by: str = ""  # member name, or "你" for the admin page
    ts: float = 0.0
    enabled: bool = True


def clean_text(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ConfigError("梗的内容不能为空")
    if len(text) > MAX_TEXT:
        raise ConfigError(f"一条梗最多 {MAX_TEXT} 个字")
    return text


def extract_memes(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove [梗:内容|链接] tags; returns the text and (content, url) pairs."""
    found: list[tuple[str, str]] = []

    def take(m: re.Match) -> str:
        body = m.group(1)
        content, _, url = body.partition("|") if "|" in body else body.partition("｜")
        url_match = _URL.search(url) or _URL.search(content)
        content = " ".join(_URL.sub("", content).split()).strip(" ，,")
        if content:
            found.append((content, url_match.group(0) if url_match else ""))
        return " "

    plain = TAG_RE.sub(take, text)
    plain = "\n".join(line.strip() for line in re.sub(r"[ \t]{2,}", " ", plain).splitlines()).strip()
    return plain, found


def _key(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？、：:（）()\"'“”‘’]", "", text).lower()


class MemeLibrary:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "memes.json"
        self.items: list[Meme] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("梗库 %s 损坏，已忽略", self.path)
            return
        for entry in raw.get("memes", []):
            self.items.append(Meme(**{k: entry[k] for k in ("id", "text", "url", "added_by", "ts", "enabled") if k in entry}))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"memes": [asdict(m) for m in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, meme_id: str) -> Meme:
        m = next((m for m in self.items if m.id == meme_id), None)
        if m is None:
            raise ConfigError("没有这条梗")
        return m

    def _known(self, text: str) -> bool:
        key = _key(text)
        return any(key in _key(m.text) or _key(m.text) in key for m in self.items)

    def add(self, text: str, url: str = "", added_by: str = "") -> Meme | None:
        """Add a meme; ``None`` when the library already has it."""
        text = clean_text(text)
        if self._known(text):
            return None
        meme = Meme(uuid.uuid4().hex[:12], text, url.strip(), added_by, time.time())
        self.items.append(meme)
        self.save()
        return meme

    def add_found(self, found: list[tuple[str, str]], added_by: str) -> list[Meme]:
        """What a member noted down; bad or known ones are skipped quietly."""
        added = []
        for text, url in found:
            try:
                meme = self.add(text, url, added_by)
            except ConfigError:
                continue
            if meme is not None:
                added.append(meme)
        return added

    def update(self, meme_id: str, changes: dict) -> Meme:
        m = self.get(meme_id)
        if "text" in changes:
            m.text = clean_text(changes["text"])
        if "url" in changes:
            m.url = str(changes["url"] or "").strip()
        if "enabled" in changes:
            m.enabled = bool(changes["enabled"])
        self.save()
        return m

    def delete(self, meme_id: str) -> None:
        self.items.remove(self.get(meme_id))
        self.save()

    def prompt_section(self) -> str:
        lines = [
            "梗库（网上和群里流传的、关于你们和 AI 圈的梗；只是梗，不一定是真的）：",
            "这些梗你们心里都知道，像圈内人那样直接接着玩、做出反应就好；别把梗点破，"
            "别说「这不就是那个……的梗」，也别解释给别人听。",
        ]
        memes = [m for m in self.items if m.enabled][-MAX_IN_PROMPT:]
        lines += [f"- {m.text}" for m in memes] or ["- （还没有）"]
        lines.append(
            "记梗：在网上看到、或者群里出现新的、关于群里成员或 AI 圈的梗，可以在回复末尾写 [梗:梗的内容|来源链接] 记进梗库，"
            "没有链接就只写内容；群里看不到这个标记。已经有的别重复记，不是梗的普通新闻别记。"
        )
        return "\n".join(lines)
