"""One group chat: the transcript, the round loop and turn quotas."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import deque
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .config import RoundConfig
from .drawing import extract_draw_requests
from .followups import FUTURE_WORDS, extract_followups
from .memes import extract_memes
from .memory import extract_memories
from .moderator import Moderator, RuleModerator
from .participants import Participant, ParticipantError, SessionStore
from .participants.base import ActivityHook
from .prompts import build_system_prompt, build_turn_prompt, clean_reply, looks_like_code, render_role
from .reactions import extract_reaction
from .repetition import repeated_bits
from .routine import ASLEEP, BUSY, Routines
from .vision import MAX_IMAGES, can_see_images
from .stickers import Sticker, StickerLibrary, strip_tags
from .transcript import USER, Message, find_mentions, find_recheck_targets
from .sources import source_key, source_list
from . import urge
from .urge import Urge

log = logging.getLogger(__name__)

MAX_HISTORY = 400  # messages kept in memory per chat
MAX_UNSEEN = 30  # most recent messages handed to a model in one turn
ANSWERING_SECONDS = 600  # the user speaks right after a member: likely talking to that member,
ANSWERING_STREAK = 2  # who usually answers first, unless it already did this many times in a row
# "你们…", "大家…": the user talks to the whole group, so more members chime in.
TO_ALL = re.compile(r"你们|大家|各位|所有人|都在")
DECISIONS_KEPT = 60  # recent dice rolls and their reasons, for the status page


def _draws_everyone(message: Message) -> bool:
    """The user talks to everyone ("你们", "大家") or shows a picture: more friends join in."""
    return bool(TO_ALL.search(message.text) or message.images)
MAX_TG_IDS = 500  # Telegram message ids remembered per chat, for reactions and quotes

MemoryHook = Callable[[Participant, list[str]], None]
MemeHook = Callable[[Participant, list[tuple[str, str]]], None]
FollowUpHook = Callable[[str, Participant, list], None]  # chat, member, [(when, what)]
# (room, drawer, (label, description), message that asked for it or None)
DrawHook = Callable[["ChatRoom", Participant, tuple[str, str], "Message | None"], None]


@dataclass
class _Turn:
    speaker: Participant
    task: asyncio.Task
    prev_cursor: int  # restored if the call fails, so the messages count as unseen again
    seen: list[Message] = field(default_factory=list)  # what the speaker was shown this turn
    recheck: bool = False
    recheck_request: Message | None = None


@dataclass
class _RoundState:
    turns: dict[str, int]  # turns left per member
    last_speaker: str | None = None
    passed: set[str] = field(default_factory=set)  # passed since the last real message
    failed: set[str] = field(default_factory=set)  # errored this round; not retried
    started: int = 0  # replies started since the message that opened the round
    spoke: set[str] = field(default_factory=set)  # answered the opening message already
    said: dict[str, int] = field(default_factory=dict)  # lines each said this round
    queue_for: int | None = None  # the message the line below was rolled for
    queue: list[tuple[float, str]] = field(default_factory=list)  # (how much, who) wants to speak about it
    floor: list[tuple[float, str]] = field(default_factory=list)  # everyone, for when nobody wants to
    # Owed an answer: @-mentioned and not yet spoken, or given an extra turn (e.g. to use a new sticker).
    granted: set[str] = field(default_factory=set)
    called: set[str] = field(default_factory=set)  # @-ed by the user: wakes up a sleeping member
    wake_tried: bool = False  # nobody was up: the dice were rolled for waking someone
    wake_checked: set[int] = field(default_factory=set)  # members' messages already rolled for waking a friend
    talked_back: set[str] = field(default_factory=set)  # one comeback each per round, so two can't go on forever
    to_all: bool = False  # the user's message spoke to everyone ("你们", "大家") or showed a picture
    discussion_author: str | None = None
    discussion_peer: str | None = None
    discussion_left: int = 2
    discussion_next: str | None = None
    recheck: list[str] = field(default_factory=list)
    recheck_request: Message | None = None
    recheck_declined: set[str] = field(default_factory=set)


class Sink(Protocol):
    """Where the room's output goes (Telegram, console, tests)."""

    async def post(self, chat_key: str, participant: Participant, text: str, reply_to: int | None = None) -> int | None:
        """Post ``text`` (quoting message ``reply_to`` if given); returns the sent message's id if known."""

    async def post_photo(self, chat_key, participant, path, caption, reply_to=None) -> int | None: ...

    async def react(self, chat_key: str, participant: Participant, message_id: int, emoji: str) -> None:
        """Put an emoji reaction on a message, if the sink can."""

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
    scene: str = "",
    no_coding: bool = False,
) -> None:
    """``extra`` is appended to every prompt; pass a function for per-member text."""
    for p in participants:
        others = [o.name for o in participants if o is not p]
        text = extra(p) if callable(extra) else extra
        p.set_system_prompt(
            build_system_prompt(p.name, others, render_role(p.cfg), template, text, scene, no_coding)
        )


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
        on_meme: MemeHook | None = None,
        on_followup: FollowUpHook | None = None,
        block_code: bool = False,
        on_draw: DrawHook | None = None,
        reactions: bool = False,
        routines: Routines | None = None,
    ):
        self.key = key
        self.participants = list(participants)
        self.sink = sink
        self.max_turns = round_cfg.max_turns_per_model
        self.pass_consumes_turn = round_cfg.pass_consumes_turn
        self.slow_after = round_cfg.slow_after
        self.max_parallel = round_cfg.max_parallel
        self.follow_up_chance = round_cfg.follow_up_chance
        self.follow_up_decay = round_cfg.follow_up_decay
        self.store = store
        self.stickers = stickers
        self.sticker_limit = sticker_limit  # 0: stickers off, tags are stripped
        self.moderator = moderator or RuleModerator()
        self.log_path = log_path  # transcript file (JSON lines); survives restarts
        self.on_memory = on_memory  # called with facts a model asked to remember
        self.on_meme = on_meme  # called with memes a model noted down for the meme library
        self.on_followup = on_followup  # called with things a model wants to ask about later
        self.block_code = block_code  # drop replies that contain code
        self.on_draw = on_draw  # called when a member takes on a drawing request
        self.reactions = reactions  # members may react to messages with an emoji
        self.routines = routines  # who is asleep or busy right now; None: everyone is always up
        self._tg_ids: dict[tuple[str, float], int] = {}  # (speaker, ts) -> Telegram message id
        self.messages: list[Message] = self._load_log()
        # Models already have the saved history in their own sessions, so nothing is unseen.
        self.cursors = {p.id: len(self.messages) for p in self.participants}
        self._task: asyncio.Task | None = None
        self._restart = False
        self._wake = asyncio.Event()  # set when the user speaks during a round
        self._activities: dict[str, str] = {}  # member id -> what it is doing right now
        self._extra_turns: dict[str, int] = {}  # granted while a round runs, added at its next step
        self._background: set[asyncio.Task] = set()
        self.decisions: deque[dict] = deque(maxlen=DECISIONS_KEPT)  # why someone did or didn't speak

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def update(
        self,
        participants: Sequence[Participant],
        round_cfg: RoundConfig,
        sticker_limit: int = 0,
        block_code: bool = False,
        reactions: bool = False,
    ) -> None:
        """Swap in new members or rules; call only while no round is running."""
        self.participants = list(participants)
        self.sticker_limit = sticker_limit
        self.block_code = block_code
        self.reactions = reactions
        self.max_turns = round_cfg.max_turns_per_model
        self.pass_consumes_turn = round_cfg.pass_consumes_turn
        self.slow_after = round_cfg.slow_after
        self.max_parallel = round_cfg.max_parallel
        self.follow_up_chance = round_cfg.follow_up_chance
        self.follow_up_decay = round_cfg.follow_up_decay
        # Newcomers start from the beginning of the kept history (capped by MAX_UNSEEN).
        self.cursors = {p.id: self.cursors.get(p.id, 0) for p in self.participants}

    def add_user_message(
        self, user_name: str, text: str, reply_to: str | None = None, tg_id: int = 0, images: Sequence[str] = ()
    ) -> None:
        mentions = find_mentions(text, self.participants, from_user=True)
        if reply_to and reply_to not in mentions:
            mentions.insert(0, reply_to)
        stamp = time.time()
        recheck = find_recheck_targets(text, self.participants, self.messages, reply_to=reply_to, now=stamp)
        message = Message(USER, user_name, text, tuple(mentions), stamp, tuple(images), recheck_targets=tuple(recheck))
        self._remember_tg(message, tg_id)
        self._append(message)
        log.info("[%s] %s: %s", self.key, user_name, _preview(text))
        if recheck:
            log.info("[%s] 澄清优先核对：%s", self.key, ", ".join(recheck))
        if self.busy:
            # Models already thinking finish their turn; quotas start over and, if there is
            # room, someone answers the new message right away.
            self._restart = True
            self._wake.set()
        else:
            self._task = asyncio.create_task(self._run_round())

    def resume(self, within: float = 300) -> None:
        """Pick up a round that was cut off (settings were saved mid-reply): whoever has not seen
        the latest messages may still answer them."""
        if not self.busy and self.messages and time.time() - self.messages[-1].ts < within:
            self._task = asyncio.create_task(self._run_round(last_speaker=self.messages[-1].speaker_id))

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

    async def _run_round(self, turns: dict[str, int] | None = None, last_speaker: str | None = None) -> None:
        try:
            await self._round_loop(turns, last_speaker)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("round failed in %s", self.key)
            await self.sink.notice(self.key, "总控出错了，本轮对话已中止。")

    async def _round_loop(self, turns: dict[str, int] | None = None, last_speaker: str | None = None,
                          discussion: bool = False) -> None:
        """Pick speakers until nobody has anything to add.

        Normally one model speaks at a time. If a model takes longer than ``slow_after``
        seconds (searching, thinking hard), another may start meanwhile, up to
        ``max_parallel`` at once; the slow one's reply is posted whenever it arrives.
        ``turns`` overrides the quotas (a short reaction round after a proactive post);
        a new user message always starts over with the normal quotas.
        """
        loop = asyncio.get_running_loop()
        state = _RoundState(turns or self._fresh_turns(), last_speaker)
        if discussion:
            state.discussion_author = last_speaker
        if self.messages:
            state.granted.update(self.messages[-1].mentions)
            if not self.messages[-1].from_user:  # a member's post opens the round: she has had her say
                state.spoke.add(self.messages[-1].speaker_id)
                state.said[self.messages[-1].speaker_id] = 1
            if self.messages[-1].from_user:
                state.called.update((*self.messages[-1].mentions, *self.messages[-1].recheck_targets))
                state.to_all = _draws_everyone(self.messages[-1])
                state.recheck = list(self.messages[-1].recheck_targets)
                state.recheck_request = self.messages[-1]
        pending: dict[asyncio.Task, _Turn] = {}
        last_start = float("-inf")
        focus: Message | None = None  # message whose mentions decide the next pick

        try:
            while True:
                self._wake.clear()
                if self._extra_turns:  # e.g. a sticker was just delivered to someone
                    for pid, n in self._extra_turns.items():
                        state.turns[pid] = state.turns.get(pid, 0) + n
                        state.granted.add(pid)
                    self._extra_turns.clear()
                if self._restart:
                    self._restart = False
                    state = _RoundState(self._fresh_turns(), state.last_speaker)
                    # A model may have posted after the user's new message; still honour
                    # whoever the user addressed, and answer the new message right away.
                    focus = next((m for m in reversed(self.messages) if m.from_user), None)
                    if focus is not None:
                        state.granted.update(focus.mentions)
                        state.called.update((*focus.mentions, *focus.recheck_targets))
                        state.to_all = _draws_everyone(focus)
                        state.recheck = list(focus.recheck_targets)
                        state.recheck_request = focus
                    last_start = float("-inf")

                speaking = {t.speaker.id for t in pending.values()}
                latest = focus or self.messages[-1]
                forced = set(latest.mentions) | state.granted | set(state.recheck)
                candidates = [
                    p for p in self.participants
                    if state.turns.get(p.id, 0) > 0
                    and p.id not in state.passed
                    and p.id not in state.failed
                    and (p.id not in state.recheck_declined or p.id in forced)
                    and p.id not in speaking
                    and self._unseen(p)
                ]
                candidates = self._awake(candidates, state, latest)
                forced |= state.granted  # a friend just woken by this message answers it
                if state.discussion_next and not pending:
                    target = next((p for p in candidates if p.id == state.discussion_next), None)
                    if target is None:
                        return
                    candidates = [target]
                room_for_more = bool(candidates) and len(pending) < self.max_parallel
                if state.discussion_author and pending:
                    room_for_more = False  # each reply must see the preceding one
                if any((turn.recheck and turn.recheck_request is state.recheck_request)
                       or turn.speaker.id in state.recheck for turn in pending.values()):
                    room_for_more = False  # let the person being corrected check and answer first
                speaker = None
                if room_for_more and (not pending or loop.time() - last_start >= self.slow_after):
                    owed = [c for c in candidates if c.id in forced]  # whoever was @-ed goes first
                    priority = next((p for pid in state.recheck for p in owed if p.id == pid), None)
                    if priority or owed:
                        speaker = priority or self.moderator.pick(owed, latest, state.last_speaker)
                    elif state.discussion_next:
                        speaker = candidates[0]  # the other one in a shared link's exchange
                    else:
                        speaker = self._eager(candidates, latest, state)
                    room_for_more = speaker is not None  # else nobody wants to say anything about it
                if speaker is not None:
                    focus = None
                    state.started += 1
                    state.spoke.add(speaker.id)
                    state.said[speaker.id] = state.said.get(speaker.id, 0) + 1
                    state.granted.discard(speaker.id)
                    recheck_request = state.recheck_request if speaker.id in state.recheck else None
                    if speaker.id in state.recheck:
                        state.recheck.remove(speaker.id)
                    turn = self._start_turn(speaker, state.turns[speaker.id], recheck_request=recheck_request,
                                            discussion_followup=bool(state.discussion_next))
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
                self.cursors[turn.speaker.id] = min(turn.prev_cursor, len(self.messages))  # cut off: not answered
                await self._end_activity(turn.speaker)

    def _roll(self, chance: Urge, what: str) -> bool:
        """Roll the dice for one chance, and keep why (debug log, status page)."""
        rng = getattr(self.moderator, "rng", random)
        roll = rng.random()
        hit = roll < chance.value
        self.decisions.append({"ts": time.time(), "what": what, "why": chance.explain(), "roll": round(roll, 2), "hit": hit})
        log.debug("[%s] %s：%s 掷 %.2f → %s", self.key, what, chance.explain(), roll, "是" if hit else "否")
        return hit

    def _busy(self, p: Participant) -> bool:
        return self.routines is not None and self.routines.status(p) == BUSY

    def _awake(self, candidates: list[Participant], state: _RoundState, latest: Message) -> list[Participant]:
        """Drop sleeping members, unless the user @-ed them; if nobody is up, maybe one wakes."""
        if self.routines is None:
            return candidates
        awake = [c for c in candidates if c.id in state.called or self.routines.status(c) != ASLEEP]
        if not latest.from_user and not state.discussion_author and id(latest) not in state.wake_checked:
            state.wake_checked.add(id(latest))
            awake += self._woken_by(latest, [c for c in candidates if c not in awake], state)
        if awake or not candidates or state.wake_tried or state.started or not latest.from_user:
            return awake
        state.wake_tried = True  # the user says something and everyone is asleep
        rng = getattr(self.moderator, "rng", random)
        if self._roll(urge.woken_by_user(), "吵醒"):
            sleeper = rng.choice(candidates)
            self.routines.wake(sleeper)
            log.info("[%s] %s 被吵醒了", self.key, sleeper.name)
            return [sleeper]
        return []

    def _woken_by(self, message: Message, sleepers: list[Participant], state: _RoundState) -> list[Participant]:
        """A member talks to a friend who is asleep (@ or by name): the phone may wake them."""
        named = set(find_mentions(message.text, sleepers, from_user=True))
        woken = []
        for p in sleepers:
            if p.id == message.speaker_id or p.id not in named:
                continue
            if self._roll(urge.woken_by_friend(p, at=p.id in message.mentions), "吵醒"):
                self.routines.wake(p)
                state.granted.add(p.id)  # woken to answer it
                woken.append(p)
                log.info("[%s] %s 被 %s 吵醒了", self.key, p.name, message.speaker_name)
        return woken

    def _eager(self, candidates: list[Participant], latest: Message, state: _RoundState) -> Participant | None:
        """Whoever wants to say something about the latest message, most eager first (docs/urge-to-speak.md).

        Everyone who may speak rolls once per message, and those who want to line up. When a new
        message lands, everyone rolls again for it. The message that opened the round always gets
        an answer: if nobody wants to, the most eager one speaks anyway.
        """
        if state.queue_for != id(latest):
            state.queue_for = id(latest)
            state.queue, state.floor = self._line_up(candidates, latest, state)
        speaker = self._most_eager(state.queue, candidates, latest, state)
        if speaker is not None:
            state.queue = [(n, pid) for n, pid in state.queue if pid != speaker.id]
            return speaker
        if not state.started:
            speaker = self._most_eager(state.floor, candidates, latest, state)
            if speaker is not None:
                log.debug("[%s] 没人想说，%s 先回一句", self.key, speaker.name)
            return speaker
        return None

    def _most_eager(self, scored: list[tuple[float, str]], candidates: list[Participant], latest: Message,
                    state: _RoundState) -> Participant | None:
        """The one who wants to most; between equals the moderator picks (and avoids the last speaker)."""
        by_id = {c.id: c for c in candidates}
        here = [(n, pid) for n, pid in scored if pid in by_id]
        if not here:
            return None
        top = max(n for n, _ in here)
        return self.moderator.pick([by_id[pid] for n, pid in here if n >= top - 1e-9], latest, state.last_speaker)

    def _line_up(self, candidates: list[Participant], latest: Message,
                 state: _RoundState) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
        """Roll for everyone who may speak: ((how much, who) for those who want to, the same for everyone)."""
        i = next((k for k in range(len(self.messages) - 1, -1, -1) if self.messages[k] is latest), -1)
        before = self.messages[i - 1] if i > 0 and not latest.from_user else None
        named = set() if latest.from_user else set(find_mentions(latest.text, candidates, from_user=True))
        answering = self._answering(latest)
        text = " ".join([latest.text, *[str(s.get(k, "")) for s in latest.sources for k in ("title", "excerpt")]]).casefold()
        liveliness = self.follow_up_chance / urge.DEFAULT_LIVELINESS
        wants, everyone = [], []
        for p in candidates:
            if p.id == latest.speaker_id:
                continue
            comeback = not latest.from_user and p.id not in state.talked_back
            follows = comeback and before is not None and not before.from_user and before.speaker_id == p.id
            chance = urge.wants_to_speak(
                p, pending=p.id not in state.spoke, replies=state.started, answering=p.id == answering,
                to_all=state.to_all, named=comeback and p.id in named, follows=follows,
                interested=urge.interested(p.cfg, text), said=state.said.get(p.id, 0), busy=self._busy(p),
                liveliness=liveliness, crowding=self.follow_up_decay)
            everyone.append((chance.raw, p.id))
            if self._roll(chance, "想说话"):
                wants.append((chance.raw, p.id))
                if comeback and (follows or p.id in named):
                    state.talked_back.add(p.id)
        return wants, everyone

    def _answering(self, latest: Message) -> str | None:
        """Who the user is most likely talking to without an @ or a reply: whoever spoke just
        before the user's latest message(s), a moment ago. Usually that one answers first."""
        if not latest.from_user or latest.mentions:
            return None
        i = next((k for k in range(len(self.messages) - 1, -1, -1) if self.messages[k] is latest), -1)
        while i >= 0 and self.messages[i].from_user:
            i -= 1
        if i < 0 or latest.ts - self.messages[i].ts > ANSWERING_SECONDS:
            return None
        # Already answered first several times running: let someone else have a go.
        who, streak, k = self.messages[i].speaker_id, 0, i
        while streak < ANSWERING_STREAK:
            while k >= 0 and not self.messages[k].from_user:
                k -= 1
            if k < 0 or self.messages[k + 1].speaker_id != who:
                break
            streak += 1
            while k >= 0 and self.messages[k].from_user:
                k -= 1
        return None if streak >= ANSWERING_STREAK else who

    def _fresh_turns(self) -> dict[str, int]:
        """Turns per member for a new round: its own limit, or the group's."""
        return {p.id: getattr(p.cfg, "max_turns", 0) or self.max_turns for p in self.participants}

    def _start_turn(self, speaker: Participant, turns_left: int, *, recheck_request: Message | None = None,
                    discussion_followup: bool = False) -> _Turn:
        prev_cursor = self.cursors[speaker.id]
        own = [m.text for m in self.messages if m.speaker_id == speaker.id]
        others = [m.text for m in self.messages[-MAX_UNSEEN:] if m.speaker_id != speaker.id]
        seen = self._catch_up(speaker)
        prompt = build_turn_prompt(seen, turns_left, self._activity_notes(), repeats=repeated_bits(own, others), me=speaker.id)
        if discussion_followup and not recheck_request:
            prompt += ("\n（这段分享可以停在这里。若对方只是表示明白、接受更正或说会处理，直接回复 [PASS]，"
                       "不用再确认一次、安慰一句或换个说法总结。对方有新问题、新看法，"
                       "或你确实想到值得聊的新内容时，再接着说。）")
        if recheck_request:
            prompt += ("\n（这条澄清涉及你刚才的说法或记忆，先核对相关消息和你自己的真实记忆，再直接回应这句："
                       + recheck_request.text[:600]
                       + "。按实际记录确认或纠正；缺少依据就说明不确定，不要预先认错或否认。用平常聊天的口吻说就好。）")
        if self.on_followup is not None and any(m.from_user and FUTURE_WORDS.search(m.text) for m in seen):
            prompt += "\n（刚才有人提到了以后的安排；值得之后问问的话，记得在回复末尾加 [跟进:时间|要问的事]。）"
        images = self._images_for(speaker, seen)
        if images:
            prompt += "\n（群里发的图片附在这条消息里了，你能直接看到。）"
        if self.routines is not None:
            if self.routines.status(speaker) == ASLEEP:  # the user @-ed it
                self.routines.wake(speaker)
            notes = [self.routines.group_notes(self.participants, speaker), self.routines.note_for(speaker)]
            prompt += "".join("\n" + n for n in notes if n)
        self.cursors[speaker.id] = len(self.messages)

        def on_activity(text: str) -> None:
            self._activities[speaker.id] = text
            self._spawn(self.sink.activity(self.key, speaker, text))

        async def speak() -> str:
            async with self.sink.typing(self.key, speaker):
                return await self._ask(speaker, prompt, turns_left, on_activity, images)

        return _Turn(speaker, asyncio.create_task(speak()), prev_cursor, seen,
                     recheck=bool(recheck_request), recheck_request=recheck_request)

    async def _finish_turn(self, turn: _Turn, state: _RoundState) -> None:
        speaker = turn.speaker
        # A new correction arrived while this older answer was still in progress.
        # That old attempt must not consume the newly granted chance to recheck it.
        newer_recheck = (turn.recheck_request is not state.recheck_request
                         and (speaker.id in state.recheck or turn.recheck))
        await self._end_activity(speaker)
        try:
            raw = turn.task.result()
        except ParticipantError as e:
            log.warning("%s failed: %s", speaker.id, e)
            self.cursors[speaker.id] = min(turn.prev_cursor, len(self.messages))
            if not newer_recheck:
                state.failed.add(speaker.id)
            if state.discussion_author:
                state.turns = {pid: 0 for pid in state.turns}
            await self.sink.notice(self.key, f"{speaker.name} 出错了：{e}")
            return

        if await self._publish(speaker, raw, turn.seen) is None:
            if newer_recheck:
                return
            if state.discussion_author:
                state.turns = {pid: 0 for pid in state.turns}
            state.passed.add(speaker.id)
            if turn.recheck:
                state.recheck_declined.add(speaker.id)
            if self.pass_consumes_turn:
                state.turns[speaker.id] -= 1
            return
        if newer_recheck:
            return
        state.turns[speaker.id] = state.turns.get(speaker.id, 1) - 1
        state.passed.clear()  # new content: those who passed may want to respond now
        state.last_speaker = speaker.id
        if state.discussion_author:
            if state.discussion_peer is None:
                state.discussion_peer = speaker.id
                state.discussion_next = state.discussion_author
            else:
                state.discussion_left -= 1
                state.discussion_next = (state.discussion_peer if speaker.id == state.discussion_author
                                         else state.discussion_author)
                if state.discussion_left <= 0:
                    state.turns = {pid: 0 for pid in state.turns}
        # Whoever it @-ed is owed an answer, even if others speak first.
        own = next((m for m in reversed(self.messages) if m.speaker_id == speaker.id), None)
        if own is not None:
            state.granted.update(pid for pid in own.mentions if pid != speaker.id)
            if state.discussion_author and any(
                pid not in (state.discussion_author, state.discussion_peer) for pid in own.mentions
            ):
                # An explicit hand-off takes precedence over the optional pair exchange.
                state.discussion_author = state.discussion_next = None

    async def _publish(self, speaker: Participant, raw: str, seen: Sequence[Message] = (), sources=(),
                       *, external_to_session: bool = False, image_path=None, image_description='') -> str | None:
        """Clean a reply and post it. Returns the transcript text, or None if nothing was posted.

        ``seen`` is what the speaker was answering: a reaction goes on the message it answered,
        and the reply quotes that message when the chat has moved on since.
        """
        text = clean_reply(raw, speaker.name)
        draws: list[tuple[str, str]] = []
        reaction = None
        if text is not None:
            text, facts = extract_memories(text)
            if facts and self.on_memory is not None:
                self.on_memory(speaker, facts)
            text, memes = extract_memes(text)
            if memes and self.on_meme is not None:
                self.on_meme(speaker, memes)
            text, plans = extract_followups(text)
            if plans and self.on_followup is not None:
                self.on_followup(self.key, speaker, plans)
            text, reaction = extract_reaction(text)
            text, draws = extract_draw_requests(text)
            if draws and not text:
                text = "行，等我一会儿。"  # it only wrote the tag; still acknowledge the request
            if self.stickers is not None:
                text, stickers, transcript = self.stickers.extract(text, self.sticker_limit, speaker.id)
            else:
                text, stickers, transcript = strip_tags(text)
        if text and self.block_code and looks_like_code(text):
            # Everyday-chat group: a reply with code is dropped, whatever the prompt said.
            log.warning("[%s] %s 的回复里有代码，已拦下：%s", self.key, speaker.name, _preview(text))
            text = None
        mentions = [m for m in find_mentions(text, self.participants, from_user=False) if m != speaker.id] if text else []
        target = self._answered(speaker, seen, mentions)
        target_id = self._tg_id(target)
        if not (self.reactions and target_id):
            reaction = None
        if reaction and text is not None:
            note = f"[给 {target.speaker_name} 的「{_preview(target.text, 16)}」点了 {reaction}]"
            transcript = f"{transcript} {note}".strip() if transcript else note
        if text is None or not transcript:
            log.info("[%s] %s: [PASS]", self.key, speaker.name)
            return None

        attached = source_list(sources)
        for source in attached:
            if source["url"] not in text:
                text = (text + "\n" + source["url"]).strip()
                transcript = (transcript + "\n" + source["url"]).strip()

        requester = self._requester_for(speaker) if draws else None
        if image_path:
            if not Path(image_path).is_file():
                return None
            transcript += '\n[发送了一张真实游戏截图：' + image_description[:900] + ']'
        # The cursor stays where it was before the call: messages that arrived while
        # the model was thinking are still unseen, and _unseen() skips its own post.
        message = Message(speaker.id, speaker.name, transcript, tuple(mentions), time.time(),
                          images=(str(image_path),) if image_path else (),
                          sources=tuple(attached), external_to_session=external_to_session)
        if reaction:
            await self.sink.react(self.key, speaker, target_id, reaction)
        if text:
            # Quote the answered message when other messages came in after it.
            moved_on = target is not None and any(m.ts > target.ts and m is not message for m in self.messages)
            if image_path:
                sent = await self.sink.post_photo(self.key, speaker, image_path, text,
                                                  reply_to=target_id if moved_on else None)
                if sent is None:
                    return None  # Undelivered images never become public knowledge.
            else:
                sent = await self.sink.post(self.key, speaker, text, reply_to=target_id if moved_on else None)
            self._remember_tg(message, sent)
        # Failed delivery must not become shared knowledge or suppress a later retry.
        self._append(message)
        log.info("[%s] %s: %s", self.key, speaker.name, _preview(transcript))
        for sticker in stickers:
            await self.sink.post_sticker(self.key, speaker, sticker)
        if draws and self.on_draw is not None:
            self.on_draw(self, speaker, draws[0], requester)  # one picture at a time
        return transcript

    def _images_for(self, speaker: Participant, seen: Sequence[Message]) -> list[str]:
        """Pictures to hand to a member who can see: the new ones, or, when it is asked for help, the latest."""
        if not can_see_images(speaker.cfg):
            return []
        paths = [p for m in seen for p in m.images]
        if not paths and seen and speaker.id in seen[-1].mentions:
            # Someone asks it about a picture it may have seen before: show it again.
            recent = next((m for m in reversed(self.messages[-12:]) if m.images), None)
            paths = list(recent.images) if recent else []
        return [p for p in paths[-MAX_IMAGES:] if Path(p).exists()]

    def _answered(self, speaker: Participant, seen: Sequence[Message], mentions: Sequence[str]) -> Message | None:
        """The message a reply answers: the latest one from someone it @-mentions, else the latest it saw."""
        others = [m for m in reversed(seen) if m.speaker_id != speaker.id]
        for pid in mentions:
            hit = next((m for m in others if m.speaker_id == pid), None)
            if hit is not None:
                return hit
        return others[0] if others else None

    def _tg_id(self, message: Message | None) -> int:
        return self._tg_ids.get((message.speaker_id, message.ts), 0) if message is not None else 0

    def message_for(self, tg_id: int) -> Message | None:
        """The transcript message posted as Telegram message ``tg_id``."""
        key = next((k for k, v in self._tg_ids.items() if v == tg_id), None)
        if key is None:
            return None
        return next((m for m in reversed(self.messages) if (m.speaker_id, m.ts) == key), None)

    def note_reaction(self, user_name: str, target: Message, emoji: str) -> None:
        """The user reacted to a message: the members see it next time they look at the group."""
        self._append(Message(USER, user_name, f"[给 {target.speaker_name} 的「{_preview(target.text, 20)}」点了 {emoji}]", (), time.time()))

    def _remember_tg(self, message: Message, tg_id: int | None) -> None:
        if not tg_id:
            return
        self._tg_ids[(message.speaker_id, message.ts)] = tg_id
        while len(self._tg_ids) > MAX_TG_IDS:
            del self._tg_ids[next(iter(self._tg_ids))]

    def _requester_for(self, drawer: Participant) -> Message | None:
        """Who asked for the drawing: the latest message addressing the drawer, else the latest from someone else."""
        others = [m for m in reversed(self.messages) if m.speaker_id != drawer.id]
        return next((m for m in others if drawer.id in m.mentions), others[0] if others else None)

    async def deliver_drawing(self, drawer: Participant, text: str, sticker: Sticker | None, requester: Message | None) -> None:
        """The drawer posts the finished sticker; a member who asked for it gets a turn to use it."""
        to_member = requester is not None and not requester.from_user and requester.speaker_id != drawer.id
        mentions = (requester.speaker_id,) if to_member else ()
        transcript = f"{text} [表情:{sticker.label}]" if sticker else text
        await self.sink.post(self.key, drawer, text)
        self._append(Message(drawer.id, drawer.name, transcript, mentions, time.time(), external_to_session=True))
        log.info("[%s] %s: %s", self.key, drawer.name, _preview(transcript))
        if sticker is not None:
            await self.sink.post_sticker(self.key, drawer, sticker)
        if not (to_member and sticker):
            return
        if self.busy:
            self._extra_turns[requester.speaker_id] = self._extra_turns.get(requester.speaker_id, 0) + 1
            self._wake.set()
        else:
            turns = {p.id: 0 for p in self.participants}
            turns[requester.speaker_id] = 1
            self._task = asyncio.create_task(self._run_round(turns, last_speaker=drawer.id))

    # ---- proactive posts ----

    @property
    def last_activity(self) -> float:
        """Time of the latest message (0 if none is known)."""
        return self.messages[-1].ts if self.messages else 0.0

    def start_proactive(
        self, speaker: Participant, make_prompt: Callable[[list[Message]], str], reply_turns: int,
        prepared_text: str | None = None,
        sources=(),
        discussion: bool = False,
        image_path=None, image_description='',
        show_images_since: float | None = None,
    ) -> asyncio.Future | None:
        """Let a member speak unprompted, then give the others ``reply_turns`` each to react.

        ``show_images_since``: hand over the unseen pictures sent since then (catching up after sleep).
        Returns a future with the posted text (None if the member passed), or None if a
        round is already running.
        """
        if self.busy:
            return None
        result = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run_proactive(speaker, make_prompt, reply_turns, result, prepared_text, sources,
                                                           discussion, image_path, image_description,
                                                           show_images_since))
        return result

    async def _run_proactive(self, speaker, make_prompt, reply_turns: int, result: asyncio.Future,
                             prepared_text: str | None = None, sources=(), discussion: bool = False,
                             image_path=None, image_description='', show_images_since: float | None = None) -> None:
        try:
            prev_cursor = self.cursors[speaker.id]
            try:
                # No typing indicator: most checks end in a quiet [PASS].
                if prepared_text is None:
                    unseen = self._catch_up(speaker)
                    prompt = make_prompt(unseen)
                    images = [] if show_images_since is None else self._images_for(
                        speaker, [m for m in unseen if m.ts >= show_images_since])
                    if images:
                        prompt += "\n（你睡着时群里发的图片附在这条消息里了，你能直接看到。）"
                    self.cursors[speaker.id] = len(self.messages)
                    raw = await self._ask(speaker, prompt, 1, images=images)
                else:
                    # A background author has not read the public session's unseen messages.
                    raw = prepared_text
            except ParticipantError as e:
                self.cursors[speaker.id] = prev_cursor
                log.warning("[%s] %s 主动发言失败：%s", self.key, speaker.id, e)
                result.set_result(None)
                return
            if sources and self.shared_recently(sources):
                result.set_result(None)
                return
            posted = await self._publish(speaker, raw, sources=sources,
                                         external_to_session=prepared_text is not None,
                                         image_path=image_path, image_description=image_description)
            result.set_result(posted)
            if posted is None or reply_turns < 1:
                return
            limits = self._fresh_turns()
            turns = {p.id: min(limits[p.id], max(2, reply_turns)) if discussion else reply_turns
                     for p in self.participants}
            turns[speaker.id] = 1  # may answer what the others say about its post
            await self._round_loop(turns, last_speaker=speaker.id, discussion=discussion)
        except asyncio.CancelledError:
            if not result.done():
                result.cancel()
            raise
        except Exception as e:
            log.exception("proactive post failed in %s", self.key)
            if not result.done():
                result.set_exception(e)

    def shared_recently(self, sources, seconds: int = 86400) -> bool:
        """Avoid different readers repeatedly bringing the same link to this room."""
        keys = {source_key(s["url"]) for s in source_list(sources)}
        since = time.time() - seconds
        for message in reversed(self.messages):
            if message.ts < since:
                continue
            if any(source_key(s["url"]) in keys for s in message.sources):
                return True
        return False

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
        self,
        speaker: Participant,
        prompt: str,
        turns_left: int,
        on_activity: ActivityHook | None = None,
        images: Sequence[str] = (),
    ) -> str:
        """Get a model's turn, starting a new session if its saved one cannot be resumed.

        CLI sessions can disappear (the CLI was reinstalled, or the data folder moved and
        with it the CLI's working folder). The new session gets the recent transcript.
        """
        extra = {"images": [Path(p) for p in images]} if images else {}
        pending = []
        recent_external = [m for m in self.messages[-MAX_UNSEEN:]
                           if m.speaker_id == speaker.id and m.external_to_session][-3:]
        for message in recent_external:
            key = hashlib.sha256(f"{message.ts}\n{message.text}".encode()).hexdigest()
            if not self.store.context_seen(self.key, speaker.id, key):
                pending.append((key, message))
        if pending:
            records = [{"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.ts)),
                        "speaker": "你自己", "text": m.text, "sources": list(m.sources)} for _, m in pending]
            prompt += ("\n\n[已发出的本人消息]\n这些话已经以你的身份实际发到群里，由你的后台活动发出。"
                       "接着眼前的回复聊即可，不要把它们当成别人说的或再发一遍。来源只代表记录中说明的阅读范围。\n"
                       + json.dumps(records, ensure_ascii=False) + "\n[/已发出的本人消息]")
        try:
            text = await speaker.reply(self.key, prompt, on_activity, **extra)
        except ParticipantError:
            session = self.store.get(self.key, speaker.id)
            if not session:
                raise
            self.store.clear_one(self.key, speaker.id)
            fresh = build_turn_prompt(self.messages[-MAX_UNSEEN:], turns_left, self._activity_notes(), me=speaker.id)
            fresh += "\n\n[本轮请求]\n" + prompt + "\n[/本轮请求]"
            try:
                text = await speaker.reply(self.key, fresh, on_activity, **extra)
            except ParticipantError:
                self.store.set(self.key, speaker.id, session)  # not a session problem after all
                raise
            log.warning("[%s] %s 的会话无法恢复，已用最近的群聊记录开始新会话", self.key, speaker.name)
        self.store.mark_context_seen(self.key, speaker.id, [key for key, _ in pending])
        return text

    def _unseen(self, p: Participant) -> list[Message]:
        return [m for m in self.messages[self.cursors[p.id]:] if m.speaker_id != p.id]

    def _catch_up(self, p: Participant) -> list[Message]:
        """What a member reads before it speaks. Starting over (after a reset), its own earlier
        lines come along too, so it knows what it already said."""
        if self.cursors[p.id] == 0:
            return self.messages[-MAX_UNSEEN:]
        return self._unseen(p)[-MAX_UNSEEN:]

    def _append(self, message: Message) -> None:
        self.messages.append(message)
        overflow = len(self.messages) - MAX_HISTORY
        if overflow > 0:
            del self.messages[:overflow]
            self.cursors = {pid: max(0, c - overflow) for pid, c in self.cursors.items()}
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                data = message.to_json()
                if self._tg_id(message):
                    data["tg_id"] = self._tg_id(message)
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
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
                data = json.loads(line)
                messages.append(Message.from_json(data))
                self._remember_tg(messages[-1], data.get("tg_id"))
        if len(lines) > 2 * MAX_HISTORY:  # keep the file from growing forever
            self.log_path.write_text("".join(json.dumps({**m.to_json(), **({"tg_id": self._tg_id(m)} if self._tg_id(m) else {})},
                                                        ensure_ascii=False) + "\n" for m in messages), encoding="utf-8")
        return messages


def _preview(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
