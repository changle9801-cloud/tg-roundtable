"""How much a member wants to speak right now, and why.

Every chance in the turn-taking (joining in, answering back, answering the user first, being
woken up, being picked next) is an Urge: named parts added up, then multiplied by modifiers.
The room rolls the dice; this module only does the sums, so each decision can be explained in
the log ("Grok 0.90（接她的话 0.4 + …；× 活跃度高 1.3）"). See docs/urge-to-speak.md.

The numbers here reproduce the turn-taking as it was before this module (phase 1 of the doc).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .routine import BUSY_FACTOR, FRIEND_NAME_WAKE_CHANCE, FRIEND_WAKE_CHANCE, WAKE_CHANCE

TO_ALL_CHANCE = 0.9  # the user speaks to everyone or shows a picture: chance the next member joins in...
TO_ALL_DECAY = 0.55  # ...shrinking by this much for each further one
TALK_BACK_CHANCE = 0.6  # a member names a friend or answers right after her: chance she answers back
ANSWERING_CHANCE = 0.75  # the user goes on talking to whoever just spoke: chance that one answers first
JOIN_ACTIVITY = {"low": 0.5, "normal": 1.0, "high": 1.5}  # how eagerly it joins in
PICK_WEIGHTS = {"low": 0.5, "normal": 1.0, "high": 2.0}  # how often it is the one picked
INTEREST_WEIGHT = 3  # the topic matches what it likes: picked this much more often

ACTIVITY_NAMES = {"low": "低", "normal": "中", "high": "高"}


@dataclass
class Urge:
    member: str  # display name
    parts: list[tuple[str, float]]  # reasons to speak, added up
    factors: list[tuple[str, float]] = field(default_factory=list)  # multiplied in, in order
    cap: float | None = 1.0  # a chance stops at 1; a pick weight has no cap

    @property
    def value(self) -> float:
        v = sum(n for _, n in self.parts)
        for _, f in self.factors:
            v *= f
        return v if self.cap is None else min(v, self.cap)

    def explain(self) -> str:
        parts = " + ".join(f"{name} {n:g}" for name, n in self.parts)
        factors = "".join(f" ×{name} {f:g}" for name, f in self.factors if f != 1)
        return f"{self.member} {self.value:.2f}（{parts}{'；' + factors.strip() if factors else ''}）"


def _activity(cfg) -> str:
    return getattr(cfg, "activity", "normal") or "normal"


def _join_factors(cfg, busy: bool) -> list[tuple[str, float]]:
    level = _activity(cfg)
    factors = [(f"活跃度{ACTIVITY_NAMES.get(level, level)}", JOIN_ACTIVITY.get(level, 1.0))]
    if busy:
        factors.append(("在忙", BUSY_FACTOR))
    return factors


def join_in(p, already: int, *, to_all: bool, busy: bool, chance: float, decay: float) -> Urge | None:
    """One more member chiming in after ``already`` replies; None for the first reply (always)."""
    if already == 0:
        return None
    if to_all:
        part = ("说给大家", TO_ALL_CHANCE * TO_ALL_DECAY ** (already - 1))
    else:
        part = (f"第 {already + 1} 个接话", chance * decay ** (already - 1))
    return Urge(p.name, [part], _join_factors(p.cfg, busy))


def talk_back(p, *, named: bool, busy: bool) -> Urge:
    """A friend's message names her, or goes right after what she said."""
    reason = "被朋友点名" if named else "接她的话"
    return Urge(p.name, [(reason, TALK_BACK_CHANCE)], _join_factors(p.cfg, busy))


def answering(name: str) -> Urge:
    """The user goes on talking to whoever spoke just before, without an @."""
    return Urge(name, [("刚和阿明说话的是她", ANSWERING_CHANCE)])


def woken_by_friend(p, *, at: bool) -> Urge:
    return Urge(p.name, [("被朋友 @ 吵醒" if at else "被朋友喊名字吵醒", FRIEND_WAKE_CHANCE if at else FRIEND_NAME_WAKE_CHANCE)])


def woken_by_user() -> Urge:
    """The user speaks and everyone is asleep: one of them may wake (who is drawn afterwards)."""
    return Urge("有人", [("大家都睡着时阿明说话", WAKE_CHANCE)])


def interested(cfg, text: str) -> bool:
    """Public configured interests only; private discoveries never affect other members."""
    topics = getattr(cfg, "browse_topics", "") or getattr(cfg, "proactive_topics", "")
    terms = re.split(r"[\s,，、;；/|（）()。]+", topics.casefold())
    return any(len(term) >= 2 and term in text for term in terms)


def pick_weight(p, text: str) -> Urge:
    """How likely it is the one picked next among those who may speak (not a chance)."""
    level = _activity(p.cfg)
    factors = [("对上兴趣", INTEREST_WEIGHT)] if interested(p.cfg, text) else []
    return Urge(p.name, [(f"活跃度{ACTIVITY_NAMES.get(level, level)}", PICK_WEIGHTS.get(level, 1.0))], factors, cap=None)
