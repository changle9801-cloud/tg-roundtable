"""Decides who speaks next."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Protocol, Sequence

from .transcript import Message
from .urge import pick_weight

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

    1. A resolved correction subject checks first; otherwise whoever the latest
       message addressed (by @mention, name or reply) goes first.
    2. Otherwise a random pick, avoiding the previous speaker when possible; chattier
       members (activity "high") are picked more often, quiet ones ("low") less.
    """

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
        for pid in (*last_message.recheck_targets, *last_message.mentions):
            if pid in by_id and pid != last_message.speaker_id:
                return by_id[pid]
        pool = [p for p in candidates if p.id != last_speaker_id] or list(candidates)
        text = ' '.join([last_message.text, *[
            str(source.get(key, '')) for source in last_message.sources
            for key in ('title', 'excerpt')
        ]]).casefold()
        weights = [pick_weight(p, text).value for p in pool]
        if len(set(weights)) == 1:
            return self.rng.choice(pool)
        return self.rng.choices(pool, weights=weights)[0]
