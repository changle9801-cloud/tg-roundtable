"""Chat messages and mention detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .participants import Participant

USER = "user"


@dataclass(frozen=True)
class Message:
    speaker_id: str  # USER for humans, otherwise the participant id
    speaker_name: str
    text: str
    # Participant ids addressed by this message, in the order they appear.
    mentions: tuple[str, ...] = field(default=())
    ts: float = 0.0  # unix time; 0 when unknown

    def to_json(self) -> dict:
        return {
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "text": self.text,
            "mentions": list(self.mentions),
            "ts": self.ts,
        }

    @classmethod
    def from_json(cls, data: dict) -> Message:
        return cls(
            data["speaker_id"], data["speaker_name"], data["text"],
            tuple(data.get("mentions") or ()), float(data.get("ts") or 0),
        )

    @property
    def from_user(self) -> bool:
        return self.speaker_id == USER


def find_mentions(text: str, participants: Sequence[Participant], *, from_user: bool) -> list[str]:
    """Return ids of participants addressed in ``text``.

    Humans address a model by @name or just by its name; models must use @name,
    since they often mention each other's names in passing ("I agree with Codex").
    """
    lower = text.lower()
    hits: list[tuple[int, str]] = []
    for p in participants:
        positions = []
        for name in (p.name, *p.aliases):
            if not name:
                continue
            name = name.lower()
            m = re.search("@" + re.escape(name), lower)
            if m:
                positions.append(m.start())
            if from_user:
                i = lower.find(name)
                if i >= 0:
                    positions.append(i)
        if positions:
            hits.append((min(positions), p.id))
    return [pid for _, pid in sorted(hits)]
