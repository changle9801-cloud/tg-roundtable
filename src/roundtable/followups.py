"""Things to ask about later: "明天面试" gets a "面试怎么样？" the next day.

    <data_dir>/followups.json

A member who hears about something coming up adds [跟进:YYYY-MM-DD HH:MM|要问的事] to its
reply (hidden from the group). When the time comes, a member asks about it in the group,
like a friend who remembered. Asking happens in the daytime only, a few times a day at most.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .config import ConfigError
from .transcript import labelled

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"\[\s*跟进\s*[:：]\s*([^\]|｜\n]{1,40}?)\s*[|｜]\s*([^\]\n]{2,120}?)\s*\]")
MAX_AHEAD_DAYS = 60
DAY_START, DAY_END = 9, 23  # asks only between 9:00 and 23:00
PER_DAY = 3
RETRY_SECONDS = 600  # the group was busy: look again in ten minutes

# Words that suggest the user is talking about something still to come.
FUTURE_WORDS = re.compile(r"明天|后天|大后天|下周|下个月|周[一二三四五六日天末]|星期[一二三四五六日天]|今晚|晚上|待会|等会|"
                          r"过几天|月底|月初|号|考试|面试|出差|出门|旅行|旅游|看病|复查|体检|约了|要去|准备去|打算")

INSTRUCTIONS = (
    "记下要跟进的事：朋友提到以后要做、要发生的事（面试、考试、出差、看病、约会、等结果……），值得之后问一句的，"
    "在回复末尾加 [跟进:YYYY-MM-DD HH:MM|之后要问的事]，时间写你打算问的时候（一般是那件事结束以后），"
    "群里看不到这个标记。小事和已经记过的不用记。"
)


@dataclass
class FollowUp:
    id: str
    text: str  # what to ask about, e.g. "阿明今天面试，问问结果"
    due: float  # when to ask (unix time)
    chat: str  # the chat it came from
    noted_by: str  # member id
    created: float
    status: str = "pending"  # pending / done / skipped
    asked_at: float = 0.0


def parse_when(value: str, now: datetime) -> datetime | None:
    """"2026-10-27 18:00", "2026-10-27", "10-27 18:00" or "10-27"; None if it makes no sense."""
    value = value.strip().replace("/", "-").replace("：", ":")
    m = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", value)
    if not m:
        return None
    year = int(m.group(1) or now.year)
    hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (20, 0)  # no time: ask in the evening
    try:
        when = datetime(year, int(m.group(2)), int(m.group(3)), hour, minute)
    except ValueError:
        return None
    if not m.group(1) and when < now - timedelta(days=1):  # "01-03" said in December means next year
        when = when.replace(year=year + 1)
    if when < now - timedelta(hours=1) or when > now + timedelta(days=MAX_AHEAD_DAYS):
        return None
    return when


def extract_followups(text: str, now: datetime | None = None) -> tuple[str, list[tuple[datetime, str]]]:
    """Remove [跟进:时间|事情] tags; returns the text and the (time, thing) pairs that make sense."""
    now = now or datetime.now()
    found: list[tuple[datetime, str]] = []

    def take(m: re.Match) -> str:
        when = parse_when(m.group(1), now)
        if when is not None:
            found.append((when, " ".join(m.group(2).split())))
        return " "

    plain = TAG_RE.sub(take, text)
    plain = "\n".join(line.strip() for line in re.sub(r"[ \t]{2,}", " ", plain).splitlines()).strip()
    return plain, found


def _key(text: str) -> str:
    return re.sub(r"[\s，。,.!！?？、：:（）()]", "", text)


def _daytime(when: datetime) -> datetime:
    """Move a time outside 9:00-23:00 to the next 9:00."""
    if when.hour < DAY_START:
        return when.replace(hour=DAY_START, minute=0)
    if when.hour >= DAY_END:
        return (when + timedelta(days=1)).replace(hour=DAY_START, minute=0)
    return when


class FollowUpStore:
    def __init__(self, data_dir: Path, now: Callable[[], datetime] = datetime.now):
        self.path = data_dir / "followups.json"
        self.now = now
        self.items: list[FollowUp] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = [FollowUp(**f) for f in raw.get("followups", [])]
            except (OSError, ValueError, TypeError):
                log.warning("跟进记录 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"followups": [asdict(f) for f in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def pending(self) -> list[FollowUp]:
        return sorted((f for f in self.items if f.status == "pending"), key=lambda f: f.due)

    def add(self, text: str, when: datetime, chat: str, noted_by: str = "") -> FollowUp | None:
        """Note something to ask about; ``None`` if the same thing is already noted."""
        text = " ".join((text or "").split())[:120]
        if len(text) < 2:
            raise ConfigError("要跟进的事不能为空")
        key = _key(text)
        if any(_key(f.text) == key or (key in _key(f.text) or _key(f.text) in key) for f in self.pending()):
            return None
        item = FollowUp(uuid.uuid4().hex[:12], text, _daytime(when).timestamp(), chat, noted_by, time.time())
        self.items.append(item)
        self.items = self.items[-200:]
        self.save()
        return item

    def asked_today(self) -> int:
        today = self.now().date()
        return sum(1 for f in self.items if f.asked_at and datetime.fromtimestamp(f.asked_at).date() == today)

    def due(self) -> list[FollowUp]:
        """Follow-ups whose time has come, if asking is allowed right now."""
        now = self.now()
        if not DAY_START <= now.hour < DAY_END or self.asked_today() >= PER_DAY:
            return []
        return [f for f in self.pending() if f.due <= now.timestamp()]

    def finish(self, item: FollowUp, asked: bool) -> None:
        item.status = "done" if asked else "skipped"
        item.asked_at = self.now().timestamp() if asked else 0.0
        self.save()

    def later(self, item: FollowUp, seconds: float = RETRY_SECONDS) -> None:
        item.due = max(item.due, self.now().timestamp()) + seconds
        self.save()

    def update(self, fid: str, changes: dict) -> FollowUp:
        item = self.get(fid)
        if "text" in changes:
            text = " ".join(str(changes["text"] or "").split())[:120]
            if len(text) < 2:
                raise ConfigError("要跟进的事不能为空")
            item.text = text
        if "due" in changes:
            when = parse_when(str(changes["due"]), self.now())
            if when is None:
                raise ConfigError("时间要像 2026-10-27 18:00 这样，而且不能是过去的时间")
            item.due = _daytime(when).timestamp()
            item.status = "pending"
        self.save()
        return item

    def get(self, fid: str) -> FollowUp:
        item = next((f for f in self.items if f.id == fid), None)
        if item is None:
            raise ConfigError("没有这条跟进")
        return item

    def delete(self, fid: str) -> None:
        self.items.remove(self.get(fid))
        self.save()

    def prompt_section(self) -> str:
        lines = [INSTRUCTIONS]
        pending = self.pending()[:8]
        if pending:
            lines.append("已经记下、到时候要问的事（不用重复记）：")
            lines += [f"- {datetime.fromtimestamp(f.due):%m-%d %H:%M} {f.text}" for f in pending]
        return "\n".join(lines)


def build_followup_prompt(item: FollowUp, unseen, now_line: str) -> str:
    """The turn for asking about it. ``unseen`` is what was said since the member last looked."""
    lines = [now_line]
    if unseen:
        lines.append("[群聊新消息]")
        lines += labelled(unseen[-20:])
        lines.append("[/群聊新消息]")
    lines.append(
        f"（之前记下要问的事：{item.text}。现在到时候了，像朋友那样自然地问一句，比如问结果、问感觉；"
        "别说「我记下了要问你」「提醒一下」这种话。如果群里已经说过结果了，或者现在问不合适，就只回复 [PASS]。）"
    )
    return "\n".join(lines)
