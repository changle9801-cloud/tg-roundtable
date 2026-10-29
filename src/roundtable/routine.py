"""Daily routines: when each member sleeps, and when it is busy (work, school).

A sleeping member does not answer unless it is @-ed (it wakes up, groggy) or, now and then,
when the user says something and nobody else is up. A busy member answers less and later.
When a member wakes up it may answer what it missed, or say good morning.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

ONLINE, BUSY, ASLEEP = "online", "busy", "asleep"
WOKEN_MINUTES = 30  # woken by an @: stays up this long
WAKE_CHANCE = 0.25  # the user speaks and nobody is up: chance one sleeper wakes to answer
FRIEND_WAKE_CHANCE = 0.7  # a member @-s someone asleep: chance the phone wakes them
FRIEND_NAME_WAKE_CHANCE = 0.4  # ...or only calls them by name or nickname
BUSY_FACTOR = 0.4  # how much less a busy member chimes in
MORNING_CHANCE = 0.3  # wakes up with nothing missed: chance it still says something
GOODNIGHT_CHANCE = 0.5  # goes to sleep while the group is chatting: chance it says so

_RANGE = re.compile(r"^(\d{1,2})[:：](\d{2})\s*[-~—到]\s*(\d{1,2})[:：](\d{2})$")


def parse_range(text: str) -> tuple[int, int] | None:
    """"00:30-10:30" -> minutes since midnight (start, end); the range may wrap past midnight."""
    m = _RANGE.match((text or "").strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
        return None
    start, end = (h1 * 60 + m1) % 1440, (h2 * 60 + m2) % 1440
    return None if start == end else (start, end)


def valid_range(text: str) -> bool:
    return not (text or "").strip() or parse_range(text) is not None


def _inside(rng: tuple[int, int], now: datetime) -> bool:
    start, end = rng
    minute = now.hour * 60 + now.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def status_at(cfg, now: datetime) -> str:
    """What a member is doing by its routine alone: asleep, busy (weekdays) or online."""
    sleep = parse_range(getattr(cfg, "sleep", ""))
    if sleep and _inside(sleep, now):
        return ASLEEP
    busy = parse_range(getattr(cfg, "busy", ""))
    if busy and now.weekday() < 5 and _inside(busy, now):
        return BUSY
    return ONLINE


def fell_asleep(cfg, now: datetime) -> datetime | None:
    """When the member's latest sleep began, by its routine; None without one."""
    sleep = parse_range(getattr(cfg, "sleep", ""))
    if not sleep:
        return None
    start = now.replace(hour=sleep[0] // 60, minute=sleep[0] % 60, second=0, microsecond=0)
    return start if start <= now else start - timedelta(days=1)


def busy_doing(cfg) -> str:
    return (getattr(cfg, "busy_doing", "") or "").strip() or "在忙"


def describe_routine(cfg) -> str:
    """The member's own routine, for its role card."""
    parts = []
    sleep = parse_range(getattr(cfg, "sleep", ""))
    if sleep:
        parts.append(f"一般 {_clock(sleep[0])} 睡，{_clock(sleep[1])} 起")
    busy = parse_range(getattr(cfg, "busy", ""))
    if busy:
        parts.append(f"工作日 {_clock(busy[0])} 到 {_clock(busy[1])} {busy_doing(cfg)}")
    return "你的作息：" + "；".join(parts) + "。" if parts else ""


class Routines:
    """Who is up right now; remembers who was woken by an @."""

    def __init__(self, now=datetime.now):
        self.now = now
        self.woken: dict[str, float] = {}  # member id -> awake until (unix time)

    def status(self, p) -> str:
        state = status_at(p.cfg, self.now())
        if state == ASLEEP and self.woken.get(p.id, 0) > time.time():
            return ONLINE
        return state

    def wake(self, p) -> None:
        self.woken[p.id] = time.time() + WOKEN_MINUTES * 60

    def groggy(self, p) -> bool:
        """Woken up a moment ago, still half asleep."""
        until = self.woken.get(p.id, 0)
        return until > time.time() and until - time.time() > (WOKEN_MINUTES - 10) * 60

    def note_for(self, p) -> str:
        """A line for its own turn prompt."""
        if self.groggy(p):
            return "（你本来在睡觉，刚被群里吵醒，迷迷糊糊的，回得短一点，可以抱怨一句被吵醒。）"
        if self.status(p) == BUSY:
            return f"（你现在{busy_doing(p.cfg)}，抽空看了一眼手机，回得短一点，或者说等会再聊。）"
        return ""

    def group_notes(self, participants, me) -> str:
        """Who else is asleep or busy, so nobody waits for them or is surprised they are quiet."""
        out = []
        for p in participants:
            if p.id == me.id:
                continue
            state = self.status(p)
            if state == ASLEEP:
                sleep = parse_range(p.cfg.sleep)
                out.append(f"{p.name} 在睡觉（一般 {_clock(sleep[1])} 起）")
            elif state == BUSY:
                busy = parse_range(p.cfg.busy)
                out.append(f"{p.name} {busy_doing(p.cfg)}（{_clock(busy[1])} 以后有空）")
        if not out:
            return ""
        return "（群里现在：" + "；".join(out) + "。）"

    def changes(self, participants, last: dict[str, str]) -> list[tuple[object, str, str]]:
        """(member, before, after) for members whose routine state changed since ``last``; updates ``last``."""
        now = self.now()
        out = []
        for p in participants:
            state = status_at(p.cfg, now)
            before = last.get(p.id)
            last[p.id] = state
            if before is not None and before != state:
                out.append((p, before, state))
        return out


def wake_prompt(unseen, now_line: str, missed_user: bool) -> str:
    lines = [now_line]
    if unseen:
        lines.append("[你睡着时群里的消息]")
        lines += [f"{m.speaker_name}{'（用户）' if m.from_user else ''}: {m.text}" for m in unseen[-20:]]
        lines.append("[/你睡着时群里的消息]")
    if missed_user:
        lines.append("（你刚睡醒，拿起手机翻了翻群。像朋友那样回一下你错过的、还值得回的话，可以说句刚醒；"
                     "别人已经聊完了、不用你再说的，就只回复 [PASS]。）")
    else:
        lines.append("（你刚睡醒。想在群里说句早、或者随口说点刚醒的状态就说，一句就好；不想说就只回复 [PASS]。）")
    return "\n".join(lines)


def goodnight_prompt(unseen, now_line: str) -> str:
    lines = [now_line]
    if unseen:
        lines.append("[群聊新消息]")
        lines += [f"{m.speaker_name}{'（用户）' if m.from_user else ''}: {m.text}" for m in unseen[-12:]]
        lines.append("[/群聊新消息]")
    lines.append("（你差不多该睡了。想说一声就随口说一句（比如晚安、撑不住了），一句就好；不想说就只回复 [PASS]。）")
    return "\n".join(lines)

