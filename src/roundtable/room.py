"""One group chat: the transcript, the round loop and turn quotas."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .config import RoundConfig
from .memory import extract_memories
from .moderator import Moderator, RuleModerator
from .participants import Participant, ParticipantError, SessionStore
from .participants.base import ActivityHook
from .prompts import build_system_prompt, build_turn_prompt, clean_reply
from .stickers import Sticker, StickerLibrary, strip_tags
from .transcript import USER, Message, find_mentions

log = logging.getLogger(__name__)

MAX_HISTORY = 400  # messages kept in memory per chat
MAX_UNSEEN = 30  # most recent messages handed to a model in one turn

MemoryHook = Callable[[Participant, list[str]], None]


@dataclass
class _Turn:
    speaker: Participant
    task: asyncio.Task
    prev_cursor: int  # restored if the call fails, so the messages count as unseen again


@dataclass
class _RoundState:
    turns: dict[str, int]  # turns left per member
    last_speaker: str | None = None
    passed: set[str] = field(default_factory=set)  # passed since the last real message
    failed: set[str] = field(default_factory=set)  # errored this round; not retried


class Sink(Protocol):
    """Where the room's output goes (Telegram, console, tests)."""

    async def post(self, chat_key: str, participant: Participant, text: str) -> None: ...

    async def notice(self, chat_key: str, text: str) -> None: ...

    def typing(self, chat_key: str, participant: Participant) -> AbstractAsyncContextManager: ...

    async def post_sticker(self, chat_key: str, participant: Participant, sticker: Sticker) -> None: ...

    async def activity(self, chat_key: str, participant: Participant, text: str) -> None:
        """Show what a slow model is doing, e.g. "正在 X 上搜索"."""

    async def clear_activity(self, chat_key: str, participant: Participant) -> None:
        """The model finished; remove the activity notice if the sink can."""


def setup_system_prompts(
    participants: Sequence[Participant],
    template: str = "",
    extra: str | Callable[[Participant], str] = "",
) -> None:
    """``extra`` is appended to every prompt; pass a function for per-member text."""
    for p in participants:
        others = [o.name for o in participants if o is not p]
        text = extra(p) if callable(extra) else extra
        p.set_system_prompt(build_system_prompt(p.name, others, p.cfg.persona, template, text))


class ChatRoom:
    def __init__(
        self,
        key: str,
        participants: Sequence[Participant],
        sink: Sink,
        round_cfg: RoundConfig,
        store: SessionStore,
        moderator: Moderator | None = None,
        stickers: StickerLibrary | None = None,
        sticker_limit: int = 0,
        log_path: Path | None = None,
        on_memory: MemoryHook | None = None,
    ):
        self.key = key
        self.participants = list(participants)
        self.sink = sink
        self.max_turns = round_cfg.max_turns_per_model
        self.pass_consumes_turn = round_cfg.pass_consumes_turn
        self.slow_after = round_cfg.slow_after
        self.max_parallel = round_cfg.max_parallel
        self.store = store
        self.stickers = stickers
        self.sticker_limit = sticker_limit  # 0: stickers off, tags are stripped
        self.moderator = moderator or RuleModerator()
        self.log_path = log_path  # transcript file (JSON lines); survives restarts
        self.on_memory = on_memory  # called with facts a model asked to remember
        self.messages: list[Message] = self._load_log()
        # Models already have the saved history in their own sessions, so nothing is unseen.
        self.cursors = {p.id: len(self.messages) for p in self.participants}
        self._task: asyncio.Task | None = None
        self._restart = False
        self._wake = asyncio.Event()  # set when the user speaks during a round
        self._activities: dict[str, str] = {}  # member id -> what it is doing right now
        self._background: set[asyncio.Task] = set()

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def update(self, participants: Sequence[Participant], round_cfg: RoundConfig, sticker_limit: int = 0) -> None:
        """Swap in new members or rules; call only while no round is running."""
        self.participants = list(participants)
        self.sticker_limit = sticker_limit
        self.max_turns = round_cfg.max_turns_per_model
        self.pass_consumes_turn = round_cfg.pass_consumes_turn
        self.slow_after = round_cfg.slow_after
        self.max_parallel = round_cfg.max_parallel
        # Newcomers start from the beginning of the kept history (capped by MAX_UNSEEN).
        self.cursors = {p.id: self.cursors.get(p.id, 0) for p in self.participants}

    def add_user_message(self, user_name: str, text: str, reply_to: str | None = None) -> None:
        mentions = find_mentions(text, self.participants, from_user=True)
        if reply_to and reply_to not in mentions:
            mentions.insert(0, reply_to)
        self._append(Message(USER, user_name, text, tuple(mentions), time.time()))
        log.info("[%s] %s: %s", self.key, user_name, _preview(text))
        if self.busy:
            # Models already thinking finish their turn; quotas start over and, if there is
            # room, someone answers the new message right away.
            self._restart = True
            self._wake.set()
        else:
            self._task = asyncio.create_task(self._run_round())

    async def stop(self) -> bool:
        if not self.busy:
            return False
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        return True

    async def reset(self) -> None:
        await self.stop()
        self.messages.clear()
        self.cursors = {p.id: 0 for p in self.participants}
        self.store.clear(self.key)
        for p in self.participants:
            p.reset(self.key)
        if self.log_path is not None:
            self.log_path.unlink(missing_ok=True)

    def reset_member(self, participant: Participant) -> None:
        """Forget one member's context in this chat; it catches up from the transcript."""
        self.store.clear_one(self.key, participant.id)
        participant.reset(self.key)
        self.cursors[participant.id] = 0

    async def wait_idle(self) -> None:
        if self._task is not None:
            with suppress(asyncio.CancelledError):
                await self._task

    # ---- round loop ----

    async def _run_round(self) -> None:
        try:
            await self._round_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("round failed in %s", self.key)
            await self.sink.notice(self.key, "总控出错了，本轮对话已中止。")

    async def _round_loop(self) -> None:
        """Pick speakers until nobody has anything to add.

        Normally one model speaks at a time. If a model takes longer than ``slow_after``
        seconds (searching, thinking hard), another may start meanwhile, up to
        ``max_parallel`` at once; the slow one's reply is posted whenever it arrives.
        """
        loop = asyncio.get_running_loop()
        state = _RoundState({p.id: self.max_turns for p in self.participants})
        pending: dict[asyncio.Task, _Turn] = {}
        last_start = float("-inf")
        focus: Message | None = None  # message whose mentions decide the next pick

        try:
            while True:
                self._wake.clear()
                if self._restart:
                    self._restart = False
                    state = _RoundState({p.id: self.max_turns for p in self.participants}, state.last_speaker)
                    # A model may have posted after the user's new message; still honour
                    # whoever the user addressed, and answer the new message right away.
                    focus = next((m for m in reversed(self.messages) if m.from_user), None)
                    last_start = float("-inf")

                speaking = {t.speaker.id for t in pending.values()}
                candidates = [
                    p for p in self.participants
                    if state.turns.get(p.id, 0) > 0
                    and p.id not in state.passed
                    and p.id not in state.failed
                    and p.id not in speaking
                    and self._unseen(p)
                ]
                room_for_more = bool(candidates) and len(pending) < self.max_parallel
                if room_for_more and (not pending or loop.time() - last_start >= self.slow_after):
                    speaker = self.moderator.pick(candidates, focus or self.messages[-1], state.last_speaker)
                    focus = None
                    turn = self._start_turn(speaker, state.turns[speaker.id])
                    pending[turn.task] = turn
                    last_start = loop.time()
                    continue
                if not pending:
                    return

                timeout = max(0.0, last_start + self.slow_after - loop.time()) if room_for_more else None
                waker = asyncio.ensure_future(self._wake.wait())  # a new user message
                done, _ = await asyncio.wait([*pending, waker], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                waker.cancel()
                for task in done:
                    if task is not waker:
                        await self._finish_turn(pending.pop(task), state)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for turn in pending.values():
                await self._end_activity(turn.speaker)

    def _start_turn(self, speaker: Participant, turns_left: int) -> _Turn:
        prev_cursor = self.cursors[speaker.id]
        prompt = build_turn_prompt(self._unseen(speaker)[-MAX_UNSEEN:], turns_left, self._activity_notes())
        self.cursors[speaker.id] = len(self.messages)

        def on_activity(text: str) -> None:
            self._activities[speaker.id] = text
            self._spawn(self.sink.activity(self.key, speaker, text))

        async def speak() -> str:
            async with self.sink.typing(self.key, speaker):
                return await self._ask(speaker, prompt, turns_left, on_activity)

        return _Turn(speaker, asyncio.create_task(speak()), prev_cursor)

    async def _finish_turn(self, turn: _Turn, state: _RoundState) -> None:
        speaker = turn.speaker
        await self._end_activity(speaker)
        try:
            raw = turn.task.result()
        except ParticipantError as e:
            log.warning("%s failed: %s", speaker.id, e)
            self.cursors[speaker.id] = min(turn.prev_cursor, len(self.messages))
            state.failed.add(speaker.id)
            await self.sink.notice(self.key, f"{speaker.name} 出错了：{e}")
            return

        text = clean_reply(raw, speaker.name)
        if text is not None:
            text, facts = extract_memories(text)
            if facts and self.on_memory is not None:
                self.on_memory(speaker, facts)
            if self.stickers is not None:
                text, stickers, transcript = self.stickers.extract(text, self.sticker_limit)
            else:
                text, stickers, transcript = strip_tags(text)
        if text is None or not transcript:
            log.info("[%s] %s: [PASS]", self.key, speaker.name)
            state.passed.add(speaker.id)
            if self.pass_consumes_turn:
                state.turns[speaker.id] -= 1
            return

        mentions = [m for m in find_mentions(text, self.participants, from_user=False) if m != speaker.id]
        # The cursor stays where it was before the call: messages that arrived while
        # the model was thinking are still unseen, and _unseen() skips its own post.
        self._append(Message(speaker.id, speaker.name, transcript, tuple(mentions), time.time()))
        log.info("[%s] %s: %s", self.key, speaker.name, _preview(transcript))
        state.turns[speaker.id] = state.turns.get(speaker.id, 1) - 1
        state.passed.clear()  # new content: those who passed may want to respond now
        state.last_speaker = speaker.id
        if text:
            await self.sink.post(self.key, speaker, text)
        for sticker in stickers:
            await self.sink.post_sticker(self.key, speaker, sticker)

    def _activity_notes(self) -> list[str]:
        names = {p.id: p.name for p in self.participants}
        return [f"{names.get(pid, pid)} {text}" for pid, text in self._activities.items()]

    async def _end_activity(self, speaker: Participant) -> None:
        if self._activities.pop(speaker.id, None) is not None:
            await self.sink.clear_activity(self.key, speaker)

    def _spawn(self, coro) -> None:
        """Fire-and-forget, keeping a reference so the task is not garbage collected."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _ask(
        self, speaker: Participant, prompt: str, turns_left: int, on_activity: ActivityHook | None = None
    ) -> str:
        """Get a model's turn, starting a new session if its saved one cannot be resumed.

        CLI sessions can disappear (the CLI was reinstalled, or the data folder moved and
        with it the CLI's working folder). The new session gets the recent transcript.
        """
        try:
            return await speaker.reply(self.key, prompt, on_activity)
        except ParticipantError:
            session = self.store.get(self.key, speaker.id)
            if not session:
                raise
            self.store.clear_one(self.key, speaker.id)
            fresh = build_turn_prompt(self.messages[-MAX_UNSEEN:], turns_left, self._activity_notes())
            try:
                text = await speaker.reply(self.key, fresh, on_activity)
            except ParticipantError:
                self.store.set(self.key, speaker.id, session)  # not a session problem after all
                raise
            log.warning("[%s] %s 的会话无法恢复，已用最近的群聊记录开始新会话", self.key, speaker.name)
            return text

    def _unseen(self, p: Participant) -> list[Message]:
        return [m for m in self.messages[self.cursors[p.id]:] if m.speaker_id != p.id]

    def _append(self, message: Message) -> None:
        self.messages.append(message)
        overflow = len(self.messages) - MAX_HISTORY
        if overflow > 0:
            del self.messages[:overflow]
            self.cursors = {pid: max(0, c - overflow) for pid, c in self.cursors.items()}
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(message.to_json(), ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning("无法写入群聊记录 %s：%s", self.log_path, e)

    def _load_log(self) -> list[Message]:
        if self.log_path is None or not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log.warning("无法读取群聊记录 %s：%s", self.log_path, e)
            return []
        messages = []
        for line in lines[-MAX_HISTORY:]:
            with suppress(ValueError, KeyError, TypeError):
                messages.append(Message.from_json(json.loads(line)))
        if len(lines) > 2 * MAX_HISTORY:  # keep the file from growing forever
            self.log_path.write_text("".join(json.dumps(m.to_json(), ensure_ascii=False) + "\n" for m in messages), encoding="utf-8")
        return messages


def _preview(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
