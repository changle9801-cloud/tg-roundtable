"""When members speak up on their own: daily cap, allowed hours, random spacing.

    <data_dir>/proactive.json   per member: today's counts, next check, links already shared
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import ParticipantConfig
from .settings import parse_hours

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>()\[\]（）「」，。！？]+")
KEPT_URLS = 200
RETRY_LATER = 5 * 60  # due, but the group is busy or not quiet yet
FIRST_CHECK = (10 * 60, 30 * 60)  # after switching it on or a restart without a plan


def find_urls(text: str) -> list[str]:
    return [u.rstrip(".,;:!?'\"") for u in URL_RE.findall(text)]


@dataclass
class Plan:
    date: str = ""  # local date the counters belong to
    posts: int = 0
    attempts: int = 0  # posts plus passes; capped so a picky model cannot burn quota
    next_at: float = 0.0
    urls: list[str] = field(default_factory=list)


class ProactiveScheduler:
    def __init__(
        self,
        path: Path,
        rng: random.Random | None = None,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.path = path
        self.rng = rng or random.Random()
        self.now = now
        self.plans: dict[str, Plan] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.plans = {pid: Plan(**p) for pid, p in raw.items()}
            except (OSError, ValueError, TypeError):
                log.warning("主动发言状态文件 %s 损坏，已忽略", path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: asdict(v) for k, v in self.plans.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # ---- timing ----

    def in_hours(self, pc: ParticipantConfig, at: datetime | None = None) -> bool:
        start, end = parse_hours(pc.proactive_hours) or (10, 23)
        hour = (at or self.now()).hour
        return start <= hour < end if start < end else hour >= start or hour < end % 24

    def _spacing(self, pc: ParticipantConfig) -> float:
        start, end = parse_hours(pc.proactive_hours) or (10, 23)
        hours = (end - start) % 24 or 24
        return hours * 3600 / max(pc.proactive_per_day, 1)

    def plan(self, pc: ParticipantConfig) -> Plan:
        """The member's plan for today, starting a fresh one on a new day."""
        now = self.now()
        today = now.date().isoformat()
        plan = self.plans.setdefault(pc.id, Plan())
        if plan.date != today:
            plan.date, plan.posts, plan.attempts = today, 0, 0
            if plan.next_at < now.timestamp():
                plan.next_at = now.timestamp() + self.rng.uniform(*FIRST_CHECK)
        return plan

    def due(self, pc: ParticipantConfig) -> bool:
        if not pc.proactive:
            return False
        plan = self.plan(pc)
        return (
            self.in_hours(pc)
            and plan.posts < pc.proactive_per_day
            and plan.attempts < pc.proactive_per_day * 2
            and self.now().timestamp() >= plan.next_at
        )

    def postpone(self, pc: ParticipantConfig, seconds: float = RETRY_LATER) -> None:
        self.plan(pc).next_at = self.now().timestamp() + seconds

    def record(self, pc: ParticipantConfig, posted_text: str | None) -> None:
        """After an attempt: count it, remember shared links, schedule the next one."""
        plan = self.plan(pc)
        plan.attempts += 1
        if posted_text is not None:
            plan.posts += 1
            for url in find_urls(posted_text):
                if url not in plan.urls:
                    plan.urls.append(url)
            plan.urls = plan.urls[-KEPT_URLS:]
        # Spread the day's posts over the allowed hours, with jitter so it is not on the hour.
        plan.next_at = self.now().timestamp() + self._spacing(pc) * self.rng.uniform(0.7, 1.3)
        self.save()

    def recent_urls(self, pc: ParticipantConfig, limit: int = 30) -> list[str]:
        return self.plan(pc).urls[-limit:]

    def describe(self, pc: ParticipantConfig) -> dict:
        plan = self.plan(pc)
        return {
            "posts_today": plan.posts,
            "next_at": plan.next_at,
            "in_hours": self.in_hours(pc),
            "done_for_today": plan.posts >= pc.proactive_per_day or plan.attempts >= pc.proactive_per_day * 2,
        }


def seconds_quiet(last_ts: float, now: float | None = None) -> float:
    """How long the group has been quiet; a group with no saved messages counts as quiet."""
    if not last_ts:
        return float("inf")
    return (now or time.time()) - last_ts


__all__ = ["ProactiveScheduler", "find_urls", "seconds_quiet"]
