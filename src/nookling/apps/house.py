"""The house as a place to talk (docs/house-talk.md, 3a).

The family's conversation at home uses the same logic as a group chat; this ``Sink`` is
where its words go. The conversation keeps them in its own transcript (``chats/__home.jsonl``),
which the house page shows; nothing goes to Telegram. Whoever speaks walks to the living room.
"""

from __future__ import annotations

import itertools
import logging
import time
from contextlib import asynccontextmanager
from typing import Callable

from ..participants import Participant

log = logging.getLogger(__name__)

HOUSE_TALK = "__home"  # the conversation's key; keys starting with "__" are not group chats


class HouseSink:
    def __init__(self, on_speak: Callable[[str], None] = lambda pid: None):
        self.on_speak = on_speak  # a member said something out loud: she goes where the talk is
        self.thinking: dict[str, float] = {}  # member id -> since when she is working on a reply
        self._ids = itertools.count(1)  # what the conversation keeps in place of a chat app's message ids

    async def post(self, chat_key: str, participant: Participant, text: str, reply_to: int | None = None) -> int | None:
        self.on_speak(participant.id)
        return next(self._ids)

    async def post_photo(self, chat_key, participant, path, caption, reply_to=None) -> int | None:
        self.on_speak(participant.id)
        return next(self._ids)

    async def react(self, chat_key: str, participant: Participant, message_id: int, emoji: str) -> None:
        return None  # the house conversation has no reactions

    async def notice(self, chat_key: str, text: str) -> None:
        log.info("[家里] %s", text)

    @asynccontextmanager
    async def typing(self, chat_key: str, participant: Participant):
        self.thinking[participant.id] = time.time()
        try:
            yield
        finally:
            self.thinking.pop(participant.id, None)

    async def post_sticker(self, chat_key: str, participant: Participant, sticker) -> None:
        return None  # the transcript already says which sticker it was

    async def activity(self, chat_key: str, participant: Participant, text: str) -> None:
        return None

    async def clear_activity(self, chat_key: str, participant: Participant) -> None:
        return None
