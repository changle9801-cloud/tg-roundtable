"""Each member's journal of things it saw while browsing on its own ("见闻").

Entries keep the source link as proof, are private to the member until it shares one in
the group, and are forgotten after a few days (longer for memorable or shared ones).

    <data_dir>/journal.json
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError, ParticipantConfig
from .memes import extract_memes

log = logging.getLogger(__name__)

DAY = 86400
MAX_ENTRIES_PER_BROWSE = 3
IN_PROMPT = 8  # most recent entries shown to the member
MEMORABLE = 4  # interest from which an entry is kept for journal_keep_days
SHAREABLE = 3  # interest needed before it is worth sharing in the group


@dataclass
class Entry:
    id: str
    member: str
    ts: float  # when it was seen
    title: str
    summary: str
    take: str  # the member's own view, in character
    url: str
    source: str  # "x" or "web"
    interest: int  # 1-5; 5 = can't wait to share it
    shared_ts: float = 0.0  # when it was posted to the group, 0 if not yet
    expires_at: float = 0.0
    read_level: str = ""


class JournalStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "journal.json"
        self.entries: list[Entry] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.entries = [Entry(**e) for e in raw.get("entries", [])]
            except (OSError, ValueError, TypeError):
                log.warning("见闻文件 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"entries": [asdict(e) for e in self.entries]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- writing ----

    @staticmethod
    def _expiry(pc: ParticipantConfig, seen: float, interest: int, shared: bool) -> float:
        days = pc.journal_keep_days if shared or interest >= MEMORABLE else pc.journal_days
        return seen + days * DAY

    def add(self, pc: ParticipantConfig, items: list[dict], now: float | None = None) -> list[Entry]:
        """Store what a browse session found; links already in the journal are skipped."""
        now = now or time.time()
        known = {e.url for e in self.entries if e.member == pc.id}
        added = []
        for item in items[:MAX_ENTRIES_PER_BROWSE]:
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url.startswith(("http://", "https://")) or not title or url in known:
                continue
            try:
                interest = min(5, max(1, int(item.get("interest") or 3)))
            except (TypeError, ValueError):
                interest = 3
            entry = Entry(
                id=uuid.uuid4().hex[:12],
                member=pc.id,
                ts=now,
                title=title[:120],
                summary=str(item.get("summary") or "").strip()[:500],
                read_level=str(item.get("read_level") or "").strip()[:160],
                take=str(item.get("take") or "").strip()[:300],
                url=url,
                source="x" if str(item.get("source", "")).lower() == "x" or "x.com/" in url or "twitter.com/" in url else "web",
                interest=interest,
                expires_at=self._expiry(pc, now, interest, shared=False),
            )
            self.entries.append(entry)
            known.add(url)
            added.append(entry)
        if added:
            self.save()
        return added

    def mark_shared(self, pc: ParticipantConfig, entry_id: str, now: float | None = None) -> None:
        entry = self.get(entry_id)
        entry.shared_ts = now or time.time()
        entry.expires_at = max(entry.expires_at, self._expiry(pc, entry.ts, entry.interest, shared=True))
        self.save()

    def get(self, entry_id: str) -> Entry:
        entry = next((e for e in self.entries if e.id == entry_id), None)
        if entry is None:
            raise ConfigError("没有这条见闻")
        return entry

    def delete(self, entry_id: str) -> None:
        self.entries.remove(self.get(entry_id))
        self.save()

    def forget_expired(self, now: float | None = None) -> int:
        now = now or time.time()
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.expires_at > now]
        if len(self.entries) != before:
            self.save()
        return before - len(self.entries)

    # ---- reading ----

    def of(self, member: str) -> list[Entry]:
        return sorted((e for e in self.entries if e.member == member), key=lambda e: e.ts, reverse=True)

    def best_unshared(self, member: str, now: float | None = None) -> Entry | None:
        now = now or time.time()
        fresh = [e for e in self.of(member) if not e.shared_ts and e.interest >= SHAREABLE and e.expires_at > now]
        return max(fresh, key=lambda e: (e.interest, e.ts), default=None)

    def prompt_section(self, member: str) -> str:
        entries = self.of(member)[:IN_PROMPT]
        if not entries:
            return ""
        lines = [
            "你最近的见闻（这些是你真的在网上看到过的，聊天时可以自然地提起；"
            "别人问你在做什么、最近看了什么，就照这些回答；除此之外不要编造自己的经历）："
        ]
        for e in entries:
            when = time.strftime("%m-%d %H:%M", time.localtime(e.ts))
            shared = "，已经在群里分享过" if e.shared_ts else ""
            lines.append(f"- [{when}，{'X' if e.source == 'x' else '网上'}{shared}] {e.title}：{e.summary}（你的看法：{e.take}）{e.url}")
        return "\n".join(lines)

    def known_urls(self, member: str) -> list[str]:
        return [e.url for e in self.of(member)]


BROWSE_CHAT = "__browse__"  # a throwaway session, separate from every group chat


async def browse_once(participant, journal: JournalStore, prompt: str, memes=None) -> list[Entry]:
    """Run one private browsing session and keep what it found (memes go to ``memes``)."""
    try:
        raw = await participant.reply(BROWSE_CHAT, prompt)
    finally:
        # A fresh session each time: browsing should not pile up into one long context.
        participant.store.clear_one(BROWSE_CHAT, participant.id)
        participant.reset(BROWSE_CHAT)
    raw, found = extract_memes(raw)  # before the JSON is parsed: the tags would break it
    if found and memes is not None:
        added = memes.add_found(found, participant.cfg.name)
        if added:
            log.info("%s 在梗库里记下了：%s", participant.cfg.name, "；".join(m.text for m in added))
    return journal.add(participant.cfg, parse_findings(raw))


def parse_findings(raw: str) -> list[dict]:
    """The JSON array a browse session answers with; tolerant of fences and chatter."""
    text = raw.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
