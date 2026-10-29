"""The group's own history: the day's memorable moments, so members can bring up old stories.

    <data_dir>/chronicle.json

Once a day (late evening, or the next time the bot runs) one member reads that day's group
chat and writes down one to three moments worth remembering ("阿明把洗衣液倒进了咖啡机"),
plus any running joke the group made up; those go into the meme library. Every member sees
the recent moments, so "你上次不是还……" becomes possible.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from .config import ConfigError
from .recall import topic_score

log = logging.getLogger(__name__)

RUN_AFTER_HOUR = 23  # write the day up after 23:00 (or later, for a day that was missed)
MIN_MESSAGES = 6  # a quiet day has nothing to remember
KEEP_DAYS = 90
IN_PROMPT = 8


@dataclass
class Moment:
    id: str
    day: str  # YYYY-MM-DD
    title: str
    what: str
    people: list[str] = field(default_factory=list)
    created: float = 0.0


def parse_reply(raw: str) -> tuple[list[dict], list[str]]:
    """The writer's JSON: {"moments": [...], "memes": [...]}; tolerant of fences and chatter."""
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise ConfigError("整理结果不是 JSON")
    try:
        data = json.loads(m.group(0))
    except ValueError:
        raise ConfigError("整理结果不是合法的 JSON") from None
    moments = [x for x in data.get("moments") or [] if isinstance(x, dict)]
    memes = [str(x).strip() for x in data.get("memes") or [] if str(x).strip()]
    return moments[:3], memes[:2]


def build_prompt(day: str, lines: Sequence[str], known: Sequence[str], memes: Sequence[str]) -> str:
    parts = [
        f"下面是一个朋友群在 {day} 这一天的聊天记录。你来当群里的「记事本」，挑出值得以后翻旧账、回味的名场面。",
        "要求：",
        "- 挑 1 到 3 个：好笑的、离谱的、有人出糗的、大家一起起哄的、或者对某人很重要的事。平淡的日常不用记，真没有就返回空列表。",
        "- 每个写一句标题和一两句经过（谁、干了什么、怎么收场），用口语，像朋友之间回忆往事那样，不要评价。",
        "- people 写参与的人名。",
        "- 如果这一天群里形成了新的梗（反复出现的说法、外号、名句），写进 memes，一句话说清这个梗是什么；没有就空着。",
        "- 一次澄清、道歉或忘记内容不自动成为新梗。描述实际反复使用过的说法，不给谁规定固定台词或长期性格。",
        '只输出 JSON：{"moments":[{"title":"一句话标题","what":"经过","people":["名字"]}],"memes":["梗的说明"]}',
    ]
    if known:
        parts.append("这些以前已经记过了，别重复：\n" + "\n".join(f"- {t}" for t in known))
    if memes:
        parts.append("这些梗梗库里已经有了：\n" + "\n".join(f"- {t}" for t in memes))
    parts.append("聊天记录：\n" + "\n".join(lines))
    return "\n\n".join(parts)


class Chronicle:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "chronicle.json"
        self.moments: list[Moment] = []
        self.done_days: list[str] = []  # days already written up (even when nothing was worth it)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.moments = [Moment(**m) for m in raw.get("moments", [])]
                self.done_days = list(raw.get("done_days", []))
            except (OSError, ValueError, TypeError):
                log.warning("大事记 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"moments": [asdict(m) for m in self.moments], "done_days": self.done_days[-120:]},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def day_to_write(self, days_with_chat: Sequence[str], now: datetime) -> str | None:
        """The oldest finished day with enough chat that has not been written up yet."""
        today = now.date().isoformat()
        for day in sorted(set(days_with_chat)):
            if day in self.done_days:
                continue
            if day < today or (day == today and now.hour >= RUN_AFTER_HOUR):
                return day
        return None

    def record(self, day: str, moments: list[dict]) -> list[Moment]:
        added = []
        known = {m.title for m in self.moments}
        for raw in moments:
            title = str(raw.get("title") or "").strip()[:60]
            what = str(raw.get("what") or "").strip()[:300]
            if not title or not what or title in known:
                continue
            people = [str(p).strip()[:20] for p in raw.get("people") or [] if str(p).strip()][:6]
            moment = Moment(uuid.uuid4().hex[:12], day, title, what, people, time.time())
            self.moments.append(moment)
            added.append(moment)
        if day not in self.done_days:
            self.done_days.append(day)
        cutoff = date.fromordinal(date.today().toordinal() - KEEP_DAYS).isoformat()
        self.moments = [m for m in self.moments if m.day >= cutoff]
        self.save()
        return added

    def delete(self, mid: str) -> None:
        moment = next((m for m in self.moments if m.id == mid), None)
        if moment is None:
            raise ConfigError("没有这条大事记")
        self.moments.remove(moment)
        self.save()

    def forget_day(self, day: str) -> None:
        """Let a day be written up again (admin "重新整理")."""
        self.done_days = [d for d in self.done_days if d != day]
        self.save()

    def prompt_section(self) -> str:
        recent = sorted(self.moments, key=lambda m: m.day)[-IN_PROMPT:]
        if not recent:
            return ""
        lines = ["群里的往事（大事记，大家都记得；聊天时可以自然地翻旧账，别每次都提）："]
        for m in recent:
            month, day = int(m.day[5:7]), int(m.day[8:10])
            lines.append(f"- {month}月{day}日 {m.title}：{m.what}")
        return "\n".join(lines)

    def recall(self, query: str, names=()) -> str:
        matches = [(topic_score(query, m.title + " " + m.what, names), m) for m in self.moments]
        chosen = sorted((pair for pair in matches if pair[0]), key=lambda pair: (pair[0], pair[1].day), reverse=True)[:2]
        if not chosen:
            return ""
        return ("[当前话题相关的群聊往事]\n"
                + "\n".join(f"- {m.day} {m.title}：{m.what}" for _, m in chosen)
                + "\n这是当时发生的事，不能据此猜对方现在的动机；只在眼前需要时提及。[/群聊往事]")
