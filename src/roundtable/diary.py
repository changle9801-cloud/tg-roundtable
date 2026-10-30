"""Private impressions of real activities, shown by day and recalled by their owner."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .game_memories import FEELINGS as GAME_FEELINGS, HALF_LIFE_DAYS
from .games.spire_bridge import atomic_json
from .recall import topic_score
from .sources import source_list

FEELINGS = {**GAME_FEELINGS, "好奇": "🧐", "无聊": "🥱", "开心": "🙂", "难过": "😔", "平静": "🍃"}
RECENT = re.compile(r"日记|近况|最近.{0,8}(?:看|做|玩|干|怎样|怎么样)|今天.{0,8}(?:看|做|玩|心情)")
GENERIC_TOPICS = {"视频", "网页", "日常", "聊天", "朋友", "有趣", "无聊", "生活", "今天"}


@dataclass
class DiaryEntry:
    id: str
    member: str
    ref: str
    kind: str
    title: str
    event: str
    thoughts: str
    at: float
    feelings: dict[str, float] = field(default_factory=dict)
    topics: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    def strength(self, now: float) -> float:
        return max(self.feelings.values(), default=0) * 0.5 ** (max(0, now - self.at) / 86400 / HALF_LIFE_DAYS)


class DiaryStore:
    def __init__(self, data_dir: Path, now=time.time):
        self.path = data_dir / "diary.json"
        self.now = now
        self.entries = [DiaryEntry(**e) for e in json.loads(self.path.read_text(encoding="utf-8")).get("entries", [])] if self.path.exists() else []

    def record(self, member: str, ref: str, result: dict, kind: str = "browse", at: float | None = None) -> DiaryEntry | None:
        """Called with an app-verified activity result, never raw model proposals."""
        sources = source_list(result.get("sources"))
        thoughts = str(result.get("diary") or result.get("take") or "").strip()[:800]
        if (result.get("outcome") not in ("progress", "nothing_new") or not thoughts
                or not (sources or result.get("artifacts"))):
            return None
        existing = next((e for e in self.entries if e.member == member and e.ref == ref), None)
        if existing:
            return existing
        feelings = {}
        raw = result.get("feelings")
        for name, value in (raw.items() if isinstance(raw, dict) else ()):
            if name not in FEELINGS:
                continue
            try:
                strength = float(value)
            except (ValueError, TypeError):
                continue
            if math.isfinite(strength) and strength > 0:
                feelings[name] = min(1.0, strength)
        raw_topics = result.get("topics")
        topics = list(dict.fromkeys(t.strip() for t in raw_topics if isinstance(t, str)
                                   and 2 <= len(t.strip()) <= 24 and t.strip() not in GENERIC_TOPICS))[:6] if isinstance(raw_topics, list) else []
        entry = DiaryEntry(uuid.uuid4().hex[:12], member, ref, kind,
                           str(result.get("title") or "随手记下")[:160], str(result.get("progress") or "")[:1200],
                           thoughts, self.now() if at is None else at, feelings, topics, sources)
        self.entries.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, {"entries": [asdict(e) for e in self.entries]})
        return entry

    def of(self, member: str) -> list[DiaryEntry]:
        return sorted((e for e in self.entries if e.member == member), key=lambda e: e.at, reverse=True)

    def relevant(self, member: str, query: str) -> list[DiaryEntry]:
        own = self.of(member)
        if RECENT.search(query):
            return own[:2]
        query = query.casefold()
        scored = []
        for e in own:
            score = topic_score(query, f"{e.title} {e.event}")
            score += 2 * sum(t.casefold() in query for t in e.topics)
            if score:
                scored.append((score, e.strength(self.now()), e.at, e))
        return [e for _, _, _, e in sorted(scored, key=lambda row: row[:3], reverse=True)[:2]]

    def recall(self, member: str, query: str) -> str:
        entries = self.relevant(member, query)
        if not entries:
            return ""
        lines = ["[你自己的相关日记，其他成员没有读过；不是要你复述的台词]"]
        for e in entries:
            when = datetime.fromtimestamp(e.at).strftime("%m-%d")
            feelings = "、".join(e.feelings)
            lines.append(f"- {when} {e.title}：{e.event}\n  当时的感受：{e.thoughts}" + (f"（{feelings}）" if feelings else ""))
            if e.sources:
                lines.append("  实际读到的范围：" + "；".join(s.get("read_level") or "未记录" for s in e.sources))
        lines.append("只在想接话时自然提起一个相关细节或感受，猜测仍是猜测，不必讲完整故事。[/你自己的相关日记]")
        return "\n".join(lines)

    def view(self) -> list[dict]:
        return [{**asdict(e), "day": datetime.fromtimestamp(e.at).date().isoformat(),
                 "strength": round(e.strength(self.now()), 3)} for e in sorted(self.entries, key=lambda e: e.at, reverse=True)]
