"""Decides who speaks next."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Protocol, Sequence

from .transcript import Message

if TYPE_CHECKING:
    from .participants import Participant


class Moderator(Protocol):
    def pick(
        self,
        candidates: Sequence[Participant],
        last_message: Message,
        last_speaker_id: str | None,
    ) -> Participant | None: ...


class RuleModerator:
    """Rule-based moderator.

    1. Whoever the latest message addressed (by @mention, name or reply) goes first.
    2. Otherwise a random pick, avoiding the previous speaker when possible; chattier
       members (activity "high") are picked more often, quiet ones ("low") less.
    """

    WEIGHTS = {"low": 0.5, "normal": 1.0, "high": 2.0}

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()

    def pick(
        self,
        candidates: Sequence[Participant],
        last_message: Message,
        last_speaker_id: str | None,
    ) -> Participant | None:
        if not candidates:
            return None
        by_id = {p.id: p for p in candidates}
        for pid in last_message.mentions:
            if pid in by_id and pid != last_message.speaker_id:
                return by_id[pid]
        pool = [p for p in candidates if p.id != last_speaker_id] or list(candidates)
        weights = [self.WEIGHTS.get(getattr(p.cfg, "activity", "normal"), 1.0) for p in pool]
        if len(set(weights)) == 1:
            return self.rng.choice(pool)
        return self.rng.choices(pool, weights=weights)[0]
