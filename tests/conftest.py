from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from roundtable.config import ParticipantConfig, RoundConfig
from roundtable.participants import Participant, SessionStore


class FakeParticipant(Participant):
    """Replies from a script; records every prompt it receives."""

    def __init__(self, pid: str, replies, store, work_dir, delay: float = 0.0, aliases=(), activity: str = ""):
        cfg = ParticipantConfig(id=pid, name=pid.capitalize(), kind="fake", aliases=list(aliases))
        super().__init__(cfg, store, work_dir, timeout=5)
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.images: list[list[str]] = []  # pictures handed over, per call
        self.delay = delay
        self.activity = activity  # reported (like a search starting) before each delay

    async def reply(self, chat_key: str, prompt: str, on_activity=None, images=()) -> str:
        self.prompts.append(prompt)
        self.images.append([str(i) for i in images])
        if self.activity and on_activity is not None:
            on_activity(self.activity)
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies.pop(0) if self.replies else "[PASS]"
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeSink:
    def __init__(self):
        self.posts: list[tuple[str, str]] = []
        self.stickers: list[tuple[str, str]] = []
        self.notices: list[str] = []
        self.activities: list[tuple[str, str, str]] = []  # (event, member, text)
        self.reactions: list[tuple[str, int, str]] = []  # (member, message id, emoji)
        self.quotes: list[tuple[str, int | None]] = []  # (member, quoted message id) per post
        self.next_id = 1000  # Telegram-like message ids handed out by post()

    async def activity(self, chat_key, participant, text):
        self.activities.append(("show", participant.id, text))

    async def clear_activity(self, chat_key, participant):
        self.activities.append(("clear", participant.id, ""))

    async def post_sticker(self, chat_key, participant, sticker):
        self.stickers.append((participant.id, sticker.label))

    async def post(self, chat_key, participant, text, reply_to=None):
        self.posts.append((participant.id, text))
        self.quotes.append((participant.id, reply_to))
        self.next_id += 1
        return self.next_id

    async def react(self, chat_key, participant, message_id, emoji):
        self.reactions.append((participant.id, message_id, emoji))

    async def notice(self, chat_key, text):
        self.notices.append(text)

    @asynccontextmanager
    async def typing(self, chat_key, participant):
        yield


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "sessions.json")


@pytest.fixture
def make(tmp_path, store):
    def _make(pid, replies, **kw):
        return FakeParticipant(pid, replies, store, tmp_path / pid, **kw)

    return _make


@pytest.fixture
def round_cfg():
    # Everyone may chime in, so tests of other behaviour are not subject to dice rolls.
    return RoundConfig(max_turns_per_model=3, pass_consumes_turn=False, call_timeout=5,
                       follow_up_chance=1.0, follow_up_decay=1.0)
