"""Chat messages and mention detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence
from .sources import source_list

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
    images: tuple[str, ...] = field(default=())  # saved picture files sent with the message
    sources: tuple[dict, ...] = field(default=())  # only sources explicitly shared in this room
    recheck_targets: tuple[str, ...] = field(default=())  # people whose statement/memory the human is correcting
    external_to_session: bool = False  # posted on this member's behalf outside its public model session

    def to_json(self) -> dict:
        data = {
            "speaker_id": self.speaker_id,
            "speaker_name": self.speaker_name,
            "text": self.text,
            "mentions": list(self.mentions),
            "ts": self.ts,
        }
        if self.images:
            data["images"] = list(self.images)
        if self.sources:
            data["sources"] = source_list(self.sources)
        if self.recheck_targets:
            data["recheck_targets"] = list(self.recheck_targets)
        if self.external_to_session:
            data["external_to_session"] = True
        return data

    @classmethod
    def from_json(cls, data: dict) -> Message:
        return cls(
            data["speaker_id"], data["speaker_name"], data["text"],
            tuple(data.get("mentions") or ()), float(data.get("ts") or 0), tuple(data.get("images") or ()),
            tuple(source_list(data.get("sources"))),
            tuple(data.get("recheck_targets") or ()),
            bool(data.get("external_to_session", False)),
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


_RECHECK = re.compile(r"记错|记岔|记混|忘了|忘记|没想起|没记住|不记得|说错|搞错|弄错|搞混|弄混|误会了", re.I)
_CONTRAST = re.compile(r"不是.{1,80}?\s*[，,]?\s*(?:而)?是(?P<positive>[^。！？!?；;]{1,120})")


def find_recheck_targets(text: str, participants: Sequence[Participant], recent: Sequence[Message],
                         *, reply_to: str | None = None, now: float = 0) -> list[str]:
    """Conservative public-text routing; explicit @/reply wins over inference.

    For 'not A misremembering, B forgot', B gets a chance to check before spectators.
    Names/titles must be configured, and ambiguous references remain normal chat.
    No private memory is consulted to decide who is invited to speak.
    """
    if reply_to:
        return []
    explicit = set(find_mentions(text, participants, from_user=False))
    # A quoted sentence is evidence, not necessarily the user's correction now.
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"', lambda m: ' ' * len(m.group()), text)
    vocative = ""
    for participant in participants:
        for name in (participant.name, *participant.aliases):
            match = re.match(r"\s*@?" + re.escape(name) + r"\s*[，,:：]\s*", text, re.I) if name else None
            if match:
                explicit.add(participant.id)
                vocative = match.group()
    contrast = _CONTRAST.search(text)
    clause = contrast.group("positive") if contrast else text
    cue = _RECHECK.search(clause)
    if not cue:
        return []
    if re.search(r"(?:没有|并没有|并非|没|不是)\s*(?:真的|真|真地|真的就)?\s*$", clause[:cue.start()]):
        return []
    lower = clause.casefold()
    matches = []
    for participant in participants:
        for name in (participant.name, *participant.aliases):
            if not name:
                continue
            for match in re.finditer(re.escape(name.casefold()), lower[:cue.start()]):
                between = clause[match.end():cue.start()]
                if len(between) > 16 or re.search(r"[，,。；;!?！？]|(?:说|觉得|以为|问).*[我你他她]|[我你他她]", between):
                    continue
                matches.append((match.end(), participant.id))
    if matches:
        closest = max(position for position, _ in matches)
        owners = {pid for position, pid in matches if position == closest}
        return list(owners) if len(owners) == 1 and (not explicit or owners <= explicit) else []
    # 'You just misremembered': use the immediate recent speaker, not a random member.
    address = clause[:cue.start()]
    if not contrast:
        if vocative:
            address = address[len(vocative):]
        elif len(explicit) == 1:
            participant = next(p for p in participants if p.id in explicit)
            for name in (participant.name, *participant.aliases):
                address = re.sub(r"^\s*@" + re.escape(name) + r"\s*", "", address, flags=re.I)
    if re.match(r"\s*你(?:刚才|刚刚|方才|之前|这次|是不是|可能|好像|确实|真的|又|还|都|就|也|可|把这件事)*\s*$", address):
        if len(explicit) == 1:
            return list(explicit)
        previous = next((m for m in reversed(recent) if not m.from_user), None)
        ids = {p.id for p in participants}
        if not explicit and previous and previous.speaker_id in ids and now and 0 <= now - previous.ts <= 600:
            return [previous.speaker_id]
    return []
