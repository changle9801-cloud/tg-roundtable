"""How much a member wants to speak right now, and why.

When a message lands, everyone who may speak gets an Urge for it: named reasons added up,
then multiplied by modifiers, a chance from 0 to 1. The room rolls the dice for each and
lets the most eager speak first (docs/urge-to-speak.md). Waking up and the moderator's pick
between equals are Urges too. This module only does the sums, so each decision can be
explained in the log ("Grok 0.35（接她的话 0.4 + 底子 0.05；×活跃度高 1.3 ×这轮说过 1 句 0.6）").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .routine import BUSY_FACTOR, FRIEND_NAME_WAKE_CHANCE, FRIEND_WAKE_CHANCE, WAKE_CHANCE

# Wanting to speak about the latest message: reasons, added up...
PENDING = 0.2  # the message that opened the round (the user's, or a friend's post) she hasn't answered yet
TO_ALL = 0.35  # ...and it spoke to everyone ("你们", "大家") or showed a picture
ANSWERING = 0.6  # the user goes on talking to her without an @
NAMED = 0.5  # a friend's message calls her by name
FOLLOWS = 0.4  # a friend's message goes right after hers (often against it); once a round
ABOUT = 0.4  # it is about her: a drawing of or for her
IN_THREAD = 0.1  # she said something in the last few messages: friends keep talking in pairs
BRIEF = 0.3  # the user only said "嗯": wanting to answer it at all is this much less
INTEREST = 0.2  # the topic matches what she likes
BASE = 0.05  # nothing in particular, she may still chip in
# ...then multiplied.
ACTIVITY = {"low": 0.6, "normal": 1.0, "high": 1.3}
TIRED = 0.6  # for every line she already said this round
DEFAULT_LIVELINESS = 0.5  # round.follow_up_chance at which the numbers above apply as they are

# Between equally eager members the moderator picks: chattier ones and those into the topic more often.
PICK_WEIGHTS = {"low": 0.5, "normal": 1.0, "high": 2.0}
INTEREST_WEIGHT = 3

ACTIVITY_NAMES = {"low": "低", "normal": "中", "high": "高"}


@dataclass
class Urge:
    member: str  # display name
    parts: list[tuple[str, float]]  # reasons to speak, added up
    factors: list[tuple[str, float]] = field(default_factory=list)  # multiplied in, in order
    cap: float | None = 1.0  # a chance stops at 1; a pick weight has no cap
    sure: bool = False  # the group is set to everyone talking: always wants to (still ranked by raw)

    @property
    def raw(self) -> float:
        """Uncapped: how much, for putting the most eager first."""
        v = sum(n for _, n in self.parts)
        for _, f in self.factors:
            v *= f
        return v

    @property
    def value(self) -> float:
        if self.sure:
            return 1.0
        return self.raw if self.cap is None else min(self.raw, self.cap)

    def explain(self) -> str:
        parts = " + ".join(f"{name} {n:g}" for name, n in self.parts)
        factors = "".join(f" ×{name} {f:g}" for name, f in self.factors if f != 1)
        sure = "；热闹程度拉满" if self.sure else ""
        return f"{self.member} {self.value:.2f}（{parts}{'；' + factors.strip() if factors else ''}{sure}）"


def _activity(cfg) -> str:
    return getattr(cfg, "activity", "normal") or "normal"


def wants_to_speak(p, *, pending: bool, replies: int, answering: bool, to_all: bool, named: bool, follows: bool,
                   interested: bool, said: int, busy: bool, liveliness: float, crowding: float,
                   brief: bool = False, about: bool = False, in_thread: bool = False) -> Urge:
    """How much she wants to say something now that the latest message has landed.

    ``replies``: replies already started since the message that opened the round; the more
    there are, the less a further answer to that message is needed (``crowding`` per reply).
    ``said``: lines she already said this round. ``liveliness`` scales everything (the group's
    follow-up chance over DEFAULT_LIVELINESS: 0 means only owed answers; at a follow-up chance
    of 1 everyone always wants to, until their turns run out).
    """
    parts = []
    if pending:  # answering the opening message: less needed the more have already
        crowd = crowding ** max(0, replies - 1)
        parts.append(("还没回这条", PENDING * crowd))
        if to_all:
            parts.append(("说给大家", TO_ALL * crowd))
    if answering:
        parts.append(("刚和阿明说话的是她", ANSWERING))
    if named:
        parts.append(("被朋友喊名字", NAMED))
    elif follows:
        parts.append(("接她的话", FOLLOWS))
    if about:
        parts.append(("说的是她", ABOUT))
    if in_thread:
        parts.append(("在这段对话里", IN_THREAD))
    if interested:
        parts.append(("对上兴趣", INTEREST))
    parts.append(("底子", BASE))
    level = _activity(p.cfg)
    factors = [(f"活跃度{ACTIVITY_NAMES.get(level, level)}", ACTIVITY.get(level, 1.0))]
    if busy:
        factors.append(("在忙", BUSY_FACTOR))
    if said:
        factors.append((f"这轮说过 {said} 句", TIRED ** said))
    if brief and pending:
        factors.append(("阿明只是应了一声", BRIEF))
    if liveliness != 1:
        factors.append(("热闹程度", liveliness))
    return Urge(p.name, parts, factors, sure=liveliness >= 1 / DEFAULT_LIVELINESS)


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
    """How likely it is the one picked among equally eager members (not a chance)."""
    level = _activity(p.cfg)
    factors = [("对上兴趣", INTEREST_WEIGHT)] if interested(p.cfg, text) else []
    return Urge(p.name, [(f"活跃度{ACTIVITY_NAMES.get(level, level)}", PICK_WEIGHTS.get(level, 1.0))], factors, cap=None)
