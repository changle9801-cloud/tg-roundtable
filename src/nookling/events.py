"""Problems worth the owner's attention, kept across restarts.

Warnings and errors from the log, and the times the bot was not running, go to a short list
on the status page. Several problems close together, or any error, also reach the owner in a
private message from the controller bot, at most every half hour.

    <data_dir>/events.json   {"items": [...], "alive": last heartbeat, "alerted": last private message}
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

KEEP = 200
REPEAT_SECONDS = 600  # the same problem again within this: counted, not listed again
DOWN_AFTER = 300  # no heartbeat for this long: the bot was not running
ALERT_WARNINGS = 3  # this many problems within ALERT_WINDOW, or a single error...
ALERT_WINDOW = 600
ALERT_EVERY = 1800  # ...messages the owner, at most this often
ALERT_LINES = 5


@dataclass
class Event:
    ts: float
    level: str  # "warning", "error" or "down" (the bot was not running)
    source: str
    text: str
    count: int = 1
    last: float = 0.0  # when it last happened (repeats)


def _source(name: str) -> str:
    return name.removeprefix("nookling.")


def _key(text: str) -> str:
    return re.sub(r"\d+", "#", text)[:120]


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class EventLog(logging.Handler):
    def __init__(self, data_dir: Path, now=time.time):
        super().__init__(logging.WARNING)
        self.path = Path(data_dir) / "events.json"
        self.now = now
        self.items: list[Event] = []
        self.alive = 0.0
        self.alerted = 0.0
        with suppress(OSError, ValueError, TypeError, KeyError):
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Event(**e) for e in data.get("items", [])]
            self.alive = float(data.get("alive", 0))
            self.alerted = float(data.get("alerted", 0))

    # ---- recording ----

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        with suppress(Exception):
            text = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                text += f"：{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            level = "error" if record.levelno >= logging.ERROR else "warning"
            self.add(level, _source(record.name), text.strip().splitlines()[0][:300] if text.strip() else "")

    def add(self, level: str, source: str, text: str) -> None:
        now = self.now()
        key = _key(text)
        same = next((e for e in reversed(self.items) if e.source == source and _key(e.text) == key), None)
        if same is not None and now - same.last < REPEAT_SECONDS:
            same.count += 1
            same.last = now
            if level == "error":
                same.level = "error"
        else:
            self.items.append(Event(now, level, source, text, last=now))
            del self.items[:-KEEP]

    def started(self) -> None:
        """At startup: note a gap since the last heartbeat (the bot was down)."""
        now = self.now()
        if self.alive and now - self.alive > DOWN_AFTER:
            self.add("down", "bot", f"bot 没在运行：{_clock(self.alive)} 到 {_clock(now)}")
        self.beat()

    def beat(self) -> None:
        """Still running; also saves the list."""
        self.alive = self.now()
        self.save()

    def save(self) -> None:
        with suppress(OSError):
            data = {"items": [asdict(e) for e in self.items], "alive": self.alive, "alerted": self.alerted}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def clear(self) -> None:
        self.items = []
        self.save()

    def recent(self, n: int = 50) -> list[dict]:
        return [asdict(e) for e in reversed(self.items[-n:])]

    # ---- telling the owner ----

    def alert_text(self) -> str:
        """A private message for the owner, or "" when nothing new is bad enough (or one was sent lately)."""
        now = self.now()
        if now - self.alerted < ALERT_EVERY:
            return ""
        new = [e for e in self.items if e.last > self.alerted and e.level != "down"]
        recent = sum(e.count for e in new if e.last >= now - ALERT_WINDOW)
        if not any(e.level == "error" for e in new) and recent < ALERT_WARNINGS:
            return ""
        lines = [f"- {_clock(e.last)} {e.source}：{e.text}" + (f"（{e.count} 次）" if e.count > 1 else "")
                 for e in new[-ALERT_LINES:]]
        more = f"\n还有 {len(new) - ALERT_LINES} 条。" if len(new) > ALERT_LINES else ""
        return "⚙️ 圆桌最近出了点问题：\n" + "\n".join(lines) + more + "\n详情在管理页面的「状态」里。"

    def mark_alerted(self) -> None:
        self.alerted = self.now()
        self.save()
