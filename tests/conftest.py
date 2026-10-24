from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from roundtable.config import ParticipantConfig, RoundConfig
from roundtable.participants import Participant, SessionStore


class FakeParticipant(Participant):
    """Replies from a script; records every prompt it receives."""

    def __init__(self, pid: str, replies, store, work_dir, delay: float = 0.0, aliases=()):
        cfg = ParticipantConfig(id=pid, name=pid.capitalize(), kind="fake", aliases=list(aliases))
        super().__init__(cfg, store, work_dir, timeout=5)
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.delay = delay

    async def reply(self, chat_key: str, prompt: str) -> str:
        self.prompts.append(prompt)
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

    async def post_sticker(self, chat_key, participant, sticker):
        self.stickers.append((participant.id, sticker.label))

    async def post(self, chat_key, participant, text):
        self.posts.append((participant.id, text))

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
    return RoundConfig(max_turns_per_model=3, pass_consumes_turn=False, call_timeout=5)
