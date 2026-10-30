"""The running app: members, conversations and Telegram bots, rebuilt live when settings change."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from collections import Counter, deque
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path


from .config import ConfigError
from .participants import (
    HistoryParticipant,
    Participant,
    ParticipantError,
    SessionStore,
    participant_class,
    supports_web_search,
)
from .prompts import (
    build_browse_prompt,
    build_proactive_prompt,
    build_share_prompt,
    build_personal_share_prompt,
    build_system_prompt,
    build_turn_prompt,
    abilities_section,
    ask_search_instructions,
    now_line,
    render_role,
    chat_style_context,
    tools_section,
    search_instructions,
)
from .conversation import Conversation
from .recall import chat_query
from .settings import SettingsStore, build_config, normalize_settings, prune_secrets
from .drawing import (
    DrawLog,
    Drawing,
    appearance_section,
    build_draw_prompt,
    drawer_instructions,
    requester_instructions,
    supports_drawing,
)
from .journal import JournalStore, browse_once
from . import owner
from .home import ACCESSORIES, Home
from .life import LifeEngine, LifeStore, object_reply
from .life_work import LifeWorkbench
from .games.spire import GameService, highlights, make_game_actor
from .games.social import GameConversation
from .games.story import signals as game_signals, worth_remembering
from .game_memories import (
    CHOICES, GameMemoryStore, decision_prompt, fallback_decision, memory_prompt, parse_decision, parse_memories,
    template_memories,
)
from .sources import source_key
from .chronicle import MIN_MESSAGES as CHRONICLE_MIN_MESSAGES, Chronicle
from .chronicle import build_prompt as build_chronicle_prompt, parse_reply as parse_chronicle
from .events import EventLog
from .feedback import FeedbackStore, plain_text
from .followups import FollowUpStore, build_followup_prompt
from .memes import MEME_INSTRUCTIONS, MemeLibrary
from .memory import MIN_USER_MESSAGES, MemoryStore, consolidation_prompt, parse_consolidation
from .proactive import ProactiveScheduler, seconds_quiet
from .routine import (
    ASLEEP, BUSY, GOODNIGHT_CHANCE, MORNING_CHANCE, Routines, busy_doing, fell_asleep, goodnight_prompt, wake_prompt,
)
from .kaomoji import KaomojiLibrary
from .tools import ToolServer, list_tools, tools_for
from .reactions import reaction_instructions
from .vision import (
    IMAGE_KEEP_SECONDS,
    SeenCache,
    appearance_lines,
    can_see_images,
    look_prompt,
    sight_instructions,
    supports_vision,
)
from .stickers import MAX_FILE_BYTES as MAX_STICKER_BYTES, StickerLibrary, parse_set_name
from .apps.telegram import TelegramApp
from .transcript import USER, Message, label

log = logging.getLogger(__name__)

TEST_CHAT = "__admin_test__"


class LogBuffer(logging.Handler):
    """Keeps recent log lines for the admin page."""

    def __init__(self, size: int = 300):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        with suppress(Exception):
            self.lines.append(self.format(record))


class Runtime:
    def __init__(self, store: SettingsStore, log_buffer: LogBuffer | None = None):
        self.store = store
        self.sessions = SessionStore(store.data_dir / "sessions.json")
        self.log_buffer = log_buffer
        self.notices: list[str] = []  # shown on the status page
        self.settings, self.secrets = store.load()
        self.config = build_config(self.settings, self.secrets, store.data_dir)
        owner.set_name(self.config.owner_name)

        self.participants: list[Participant] = []
        self.participant_errors: dict[str, str] = {}  # member could not be started
        self.participant_warnings: dict[str, str] = {}  # started, but something is off
        self.conversations: dict[str, Conversation] = {}


        self.stickers = StickerLibrary(store.data_dir)
        self.kaomoji = KaomojiLibrary(store.data_dir)
        self.seen = SeenCache()
        self.memory = MemoryStore(store.data_dir)
        self.memes = MemeLibrary(store.data_dir)
        self.followups = FollowUpStore(store.data_dir)
        self.chronicle = Chronicle(store.data_dir)
        self._chronicle_running = False
        self._memory_running = False
        self.routines = Routines()
        self.feedback = FeedbackStore(store.data_dir)
        self.events = EventLog(store.data_dir)  # problems for the status page and the owner
        self._routine_last: dict[str, str] = {}  # member id -> asleep/busy/online at the last check
        self._routine_events: list[tuple[str, str, float]] = []  # (member id, "wake"/"sleep", when)
        self.proactive = ProactiveScheduler(store.data_dir / "proactive.json")
        self.browsing = ProactiveScheduler(store.data_dir / "browse_plan.json", prefix="browse")
        self.journal = JournalStore(store.data_dir)
        self.life = LifeStore(store.data_dir)
        self.life_workbench = LifeWorkbench(store.data_dir, lambda pid, prompt: self._life_ask(pid, prompt),
                                           read_video=self._life_video)
        self.games = GameService(store.data_dir, self._game_actor, self._game_social)
        self.game_memories = GameMemoryStore(store.data_dir)
        self.games.profile = self.game_memories.profile
        self._game_last_share = 0.0
        self._game_task: asyncio.Task | None = None
        self.life_schedule = ProactiveScheduler(store.data_dir / "life_personal_plan.json", prefix="browse")
        self.life_private = ProactiveScheduler(store.data_dir / "life_private_plan.json", prefix="browse")
        # Old plans mixed initiated activities (attempts) with passive replies (extra posts).
        # Migrate once, preserving timestamps and the old file for recovery.
        legacy_life = store.data_dir / "life_plan.json"
        if legacy_life.exists():
            legacy = ProactiveScheduler(legacy_life, prefix="browse")
            if not self.life_schedule.path.exists():
                self.life_schedule.plans = {pid: replace(plan, posts=plan.attempts) for pid, plan in legacy.plans.items()}
                self.life_schedule.save()
            if not self.life_private.path.exists():
                self.life_private.plans = {pid: replace(plan, posts=max(0, plan.posts - plan.attempts), attempts=0)
                                           for pid, plan in legacy.plans.items()}
                self.life_private.save()
        self.life_posts = ProactiveScheduler(store.data_dir / "life_shares.json")
        self.life_sessions = SessionStore(store.data_dir / "life_sessions.json")
        self._life_task: asyncio.Task | None = None
        self._life_member = ""
        self.drawings = DrawLog(store.data_dir)
        self.home = Home(store.data_dir)
        self._browsing_now: set[str] = set()
        self._background: set[asyncio.Task] = set()
        self.telegram = TelegramApp(self)  # the first chat app on the house computer
        self.sink = self.telegram.sink
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._rebuild_participants()

    # ---- members and conversations ----

    @property
    def sticker_limit(self) -> int:
        return self.config.max_stickers if self.config.stickers_enabled else 0

    def sticker_prompt(self) -> str:
        return self.stickers.prompt_section(self.sticker_limit)

    def system_prompt_for(self, pc, config=None, *, background: bool = False) -> str:
        """A member's full system prompt: role card, group rules, setting, policy, extras.

        ``config`` lets the admin page preview unsaved settings.
        """
        config = config or self.config
        others = [o.name for o in config.enabled_participants if o.id != pc.id]
        return build_system_prompt(
            pc.name, others, render_role(pc), config.system_template,
            self._extra_prompt(pc, config, background=background), config.scene, config.no_coding,
        )

    def _extra_prompt(self, pc, config, *, background: bool = False) -> str:
        """Search, memory, sticker and kaomoji sections appended to a member's system prompt."""
        if pc.web_search and supports_web_search(pc):
            search = search_instructions(covers_x=pc.kind == "grok_cli")
        else:  # it can still ask a friend who can search
            helpers = [(m.name, m.kind == "grok_cli") for m in config.enabled_participants
                       if m.id != pc.id and m.web_search and supports_web_search(m)]
            search = ask_search_instructions(helpers)
        limit = config.max_stickers if config.stickers_enabled else 0
        others = [(m.name, self._abilities(m, config)) for m in config.enabled_participants if m.id != pc.id]
        abilities = abilities_section(others, self._abilities(pc, config))
        drawing = ""
        drawers = [m for m in config.enabled_participants if m.can_draw and supports_drawing(m)]
        is_drawer = pc.can_draw and supports_drawing(pc)
        if is_drawer:
            looks = appearance_section(config.enabled_participants)
            drawing = drawer_instructions(self.drawings.remaining(pc)) + ("\n" + looks if looks else "")
        elif drawers and limit:
            drawing = requester_instructions(drawers[0].name)
        sight = ""
        if config.vision_member != "off":
            sees = can_see_images(pc)
            if config.vision_member == "help" or sees:
                helpers = [m.name for m in config.enabled_participants if m.id != pc.id and can_see_images(m)]
                sight = sight_instructions(sees, helpers)
        mine = [(s.name, s.description, pc.tool_notes.get(s.id, "")) for s in tools_for(pc, config.tools)]
        parts = [
            abilities,
            search,
            tools_section(mine),
            sight,
            drawing,
            self.memes.prompt_section() if background else MEME_INSTRUCTIONS,
            self.chronicle.prompt_section() if background else "",
            self.followups.prompt_section(),
            self.journal.prompt_section(pc.id),
            (self.life.prompt_section(pc.id, {m.id: m.name for m in config.participants}) if background
             else self.life.chat_summary(pc.id)),
            self.game_memories.prompt_section(pc.id),
            self.home.prompt_section(pc.id) if hasattr(self, "home") else "",
            self.memory.prompt_section(pc.id, config.memory_learn),
            self.feedback.prompt_section(pc.id),
            self.stickers.prompt_section(limit, pc.id),
            self.kaomoji.prompt_section(pc.id) if config.kaomoji_enabled else "",
            reaction_instructions() if config.reactions_enabled else "",
        ]
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _abilities(m, config) -> list[str]:
        """What a member can do for the group beyond chatting."""
        out = []
        if m.web_search and supports_web_search(m):
            out.append("联网搜索（也能搜 X/推特上的帖子）" if m.kind == "grok_cli" else "联网搜索")
        if m.can_draw and supports_drawing(m) and config.stickers_enabled:
            out.append("画表情包")
        out += [s.name for s in tools_for(m, config.tools)]
        mode = config.vision_member
        if mode != "off":
            describer = None if mode == "help" else next(
                (v for v in config.enabled_participants if supports_vision(v) and mode in ("auto", v.id)), None)
            if describer is not None and describer.id == m.id:
                out.append("看图（群里发的图片都是先由这位看过再说给大家）")
            elif can_see_images(m):
                out.append("看图")
        return out

    def preview_prompt(self, draft_settings: dict, member_id: str) -> str:
        config = build_config(normalize_settings(draft_settings), self.secrets, self.store.data_dir)
        pc = next((p for p in config.participants if p.id == member_id), None)
        if pc is None:
            raise ConfigError(f"没有 ID 为 {member_id} 的成员")
        return self.system_prompt_for(pc, config)

    def _rebuild_participants(self) -> None:
        built, errors = [], {}
        for pc in self.config.enabled_participants:
            try:
                built.append(self._make(pc))
            except ConfigError as e:
                errors[pc.id] = str(e)
        for p in built:
            p.tools = tools_for(p.cfg, self.config.tools)
        self.participants = built
        self.participant_errors = errors
        self.home.ensure({pc.id: pc.name for pc in self.config.enabled_participants})
        self.refresh_prompts()
        for convo in self.conversations.values():
            convo.update(built, self.config.round, self.sticker_limit, self.config.no_coding, self.config.reactions_enabled)

    def refresh_prompts(self) -> None:
        """Settings, stickers or memory changed: rebuild every system prompt (next turn on)."""
        for p in self.participants:
            p.set_system_prompt(self.system_prompt_for(p.cfg))

    stickers_changed = refresh_prompts

    def _on_followup(self, chat: str, participant: Participant, plans: list) -> None:
        added = [item for when, what in plans if (item := self.followups.add(what, when, chat, participant.id))]
        if added:
            log.info("%s 记下要跟进：%s", participant.name, "；".join(f"{a.text}" for a in added))
            self.refresh_prompts()

    def is_guest(self, user_id: int) -> bool:
        """Someone the owner brought into the group: allowed to talk, but not the owner (the first allowed id)."""
        ids = self.config.telegram.allowed_user_ids
        return bool(ids) and user_id != ids[0]

    def on_reaction(self, chat: str, tg_id: int, user_name: str, added: list[str], removed: list[str],
                    guest: bool = False) -> None:
        """A human reacted to a message in the group (or took a reaction back); only the owner's teach the members."""
        convo = self.conversation(chat)
        target = convo.message_for(self.sink.parts.get((chat, tg_id), tg_id))
        if target is None or target.from_user:
            return
        text = plain_text(target.text)
        changed = False
        if guest:  # a guest's taste is not the owner's: the convo still sees the reaction
            if added:
                convo.note_reaction(user_name, target, added[-1], guest=True)
            return
        for emoji in added:
            item = self.feedback.react(target.speaker_id, text, emoji)
            if item is not None:
                log.info("[%s] %s 给 %s 的「%s」点了 %s", chat, user_name, target.speaker_name, text[:30], emoji)
                changed = changed or item.kind == "disliked"
        if removed and not added:
            changed = self.feedback.unreact(target.speaker_id, text)
        if added:
            convo.note_reaction(user_name, target, added[-1])
        if changed:
            self.refresh_prompts()

    async def use_feedback(self, fid: str) -> None:
        """Turn a liked line into the member's sample line (示例台词)."""
        item = self.feedback.get(fid)
        if item.kind != "liked":
            raise ConfigError("只有点赞的话可以加为示例台词")
        settings = json.loads(json.dumps(self.settings))
        member = next((m for m in settings["participants"] if m["id"] == item.member), None)
        if member is None:
            raise ConfigError("这个成员已经不在了")
        lines = [line for line in (member.get("examples") or "").splitlines() if line.strip()]
        if item.text not in lines:
            member["examples"] = "\n".join([*lines, item.text])
            await self.apply(settings)
        self.feedback.mark(fid, "used")

    def _on_meme(self, participant: Participant, found: list[tuple[str, str]]) -> None:
        added = self.memes.add_found(found, participant.name)
        if added:
            log.info("%s 在梗库里记下了：%s", participant.name, "；".join(m.text for m in added))
            self.refresh_prompts()

    def _on_memory(self, participant: Participant, facts: list[str]) -> None:
        added = self.memory.suggest(facts, participant.id, self.config.memory_learn)
        if any(f.status == "active" for f in added):
            self.refresh_prompts()

    def _make(self, pc) -> Participant:
        actor = participant_class(pc)(pc, self.sessions, self.config.data_dir / "work" / pc.id, self.config.round.call_timeout)
        actor.turn_context = lambda prompt: self._chat_context(pc, prompt)
        return actor

    def _chat_context(self, pc, prompt: str) -> str:
        query = chat_query(prompt)
        names = [name for p in self.config.participants for name in (p.id, p.name, *p.aliases)]
        return "\n\n".join(part for part in (
            self.memes.recall(query, names),
            self.memory.recall(pc.id, query, names),
            self.chronicle.recall(query, names),
            self.life.private_recall(pc.id, prompt, {m.id: m.name for m in self.config.participants}),
            self.life.artifact_recall(pc.id, prompt),
            self.life.progress_recall(pc.id, prompt),
            self.life.diary.recall(pc.id, query),
            self.game_memories.recall(pc.id, query, names) or self.life.game_recall(pc.id, query),
            self.life.game_board(pc.id, query, {m.id: m.name for m in self.config.participants}),
            Runtime._game_chat_context(self, pc.id, query),
            self.home.tell(pc.id) if hasattr(self, "home") else "",
            chat_style_context(pc),
        ) if part)

    def chat_log_path(self, key: str) -> Path | None:
        if key.startswith("__"):  # admin tests and other internal chats are not recorded
            return None
        return self.config.data_dir / "chats" / f"{key}.jsonl"

    def chat_keys(self) -> list[str]:
        """Every chat with a transcript, a running convo or saved sessions."""
        keys = set(self.conversations) | set(self.sessions.chats())
        chats_dir = self.config.data_dir / "chats"
        if chats_dir.exists():
            keys |= {p.stem for p in chats_dir.glob("*.jsonl")}
        return sorted(k for k in keys if not k.startswith("__"))

    def conversation(self, chat_id: int | str) -> Conversation:
        key = str(chat_id)
        if key not in self.conversations:
            self.conversations[key] = Conversation(
                key, self.participants, self.sink, self.config.round, self.sessions,
                stickers=self.stickers, sticker_limit=self.sticker_limit,
                log_path=self.chat_log_path(key), on_memory=self._on_memory, on_meme=self._on_meme, on_followup=self._on_followup, block_code=self.config.no_coding,
                on_draw=self._on_draw, reactions=self.config.reactions_enabled, routines=self.routines,
                has_experience=lambda pid, text: bool(self.life.diary.relevant(pid, text)
                                                      or self.game_memories.recall(pid, text)),
            )
            # Old life shares predate the explicit origin field. Match public records,
            # without rewriting the transcript or exposing any private activity.
            convo = self.conversations[key]
            convo.messages = [replace(m, external_to_session=True) if any(
                s["member"] == m.speaker_id and s["text"] == m.text and abs(s["ts"] - m.ts) <= 120
                for s in self.life.shares if s.get("external_to_session", True)
            ) else m for m in convo.messages]
        return self.conversations[key]

    async def apply(self, settings: dict | None = None, secrets: dict | None = None) -> None:
        """Save new settings and/or secrets, then rebuild everything that depends on them."""
        async with self._lock:
            if settings is not None:
                settings = normalize_settings(settings)
                ids = {p["id"] for p in settings["participants"]}
                tool_ids = {t["id"] for t in settings["tools"]}
                secrets = prune_secrets(secrets if secrets is not None else self.secrets, ids, tool_ids)
            await self._cancel_life()
            await self.stop_game()
            self.store.save(settings, secrets)
            self.settings, self.secrets = self.store.load()
            self.config = build_config(self.settings, self.secrets, self.store.data_dir)
            owner.set_name(self.config.owner_name)
            cut_off = [convo for convo in self.conversations.values() if await convo.stop()]
            self.memory.drop_scope({p.id for p in self.config.participants})
            self._rebuild_participants()
            self.participant_warnings = await self.telegram.sync()
            for convo in cut_off:
                convo.resume()  # a reply that was on its way is not lost
            self._changed.set()
        log.info("配置已更新")

    async def start(self) -> None:
        self.events.started()
        self.participant_warnings = await self.telegram.sync()

    # ---- admin actions ----

    def _participant_config(self, pid: str):
        pc = next((p for p in self.config.participants if p.id == pid), None)
        if pc is None:
            raise ConfigError(f"没有 ID 为 {pid} 的成员")
        return pc

    async def test_participant(self, pid: str) -> dict:
        """Send one message to a member (even a disabled one) outside any real chat."""
        pc = self._participant_config(pid)
        p = self._make(replace(pc, enabled=True))
        p.set_system_prompt(self.system_prompt_for(pc))
        prompt = build_turn_prompt([Message(USER, "管理员", "你好，这是一条测试消息，请用一句话回复。")], 1)
        started = time.monotonic()
        try:
            text = await p.reply(TEST_CHAT, prompt)
        finally:
            self.sessions.clear(TEST_CHAT)
            p.reset(TEST_CHAT)
        return {"text": text, "seconds": round(time.monotonic() - started, 1)}

    # ---- chats and contexts ----

    def _member(self, pid: str) -> Participant:
        p = next((p for p in self.participants if p.id == pid), None)
        if p is None:
            raise ConfigError("这个成员没有启用，或者启动失败了")
        return p

    def chat_overview(self) -> list[dict]:
        out = []
        for key in self.chat_keys():
            convo = self.conversation(key)
            last = convo.messages[-1] if convo.messages else None
            out.append({
                "key": key,
                "busy": convo.busy,
                "message_count": len(convo.messages),
                "last_ts": last.ts if last else 0,
                "last": f"{last.speaker_name}: {last.text}" if last else "",
            })
        return sorted(out, key=lambda c: c["last_ts"], reverse=True)

    def chat_detail(self, key: str) -> dict:
        convo = self.conversation(key)
        members = []
        for p in self.participants:
            entry = {"id": p.id, "name": p.name, "kind": p.cfg.kind}
            if isinstance(p, HistoryParticipant):
                entry["history"] = p.load_history(key)
            else:
                entry["session_id"] = self.sessions.get(key, p.id) or ""
            members.append(entry)
        return {
            "key": key,
            "busy": convo.busy,
            "messages": [m.to_json() for m in convo.messages],
            "members": members,
        }

    async def migrate_chat(self, old: int, new: int) -> bool:
        """The group was upgraded to a supergroup and got a new id: move settings, history and context over."""
        old_key, new_key = str(old), str(new)
        allowed = self.config.telegram.allowed_chat_ids
        if old_key == new_key or (allowed and old not in allowed):
            return False
        convo = self.conversations.pop(old_key, None)
        if convo is not None:
            await convo.stop()
        self.conversations.pop(new_key, None)  # rebuilt from the moved transcript
        chats = self.config.data_dir / "chats"
        if (chats / f"{old_key}.jsonl").exists():
            target = chats / f"{new_key}.jsonl"
            moved = (chats / f"{old_key}.jsonl").read_text(encoding="utf-8")
            later = target.read_text(encoding="utf-8") if target.exists() else ""
            target.write_text(moved + later, encoding="utf-8")
            (chats / f"{old_key}.jsonl").unlink()
        self.sessions.rename(old_key, new_key)
        for history in (self.config.data_dir / "work").glob(f"*/history/{old_key}.json"):
            with suppress(OSError):
                history.replace(history.with_name(f"{new_key}.json"))
        for item in self.followups.items:
            if item.chat == old_key:
                item.chat = new_key
        self.followups.save()
        settings = json.loads(json.dumps(self.settings))
        tg = settings["telegram"]
        tg["allowed_chat_ids"] = [new if c == old else c for c in tg.get("allowed_chat_ids", [])]
        if int(settings.get("proactive", {}).get("chat_id") or 0) == old:
            settings["proactive"]["chat_id"] = new
        log.warning("群 %s 升级成了超级群 %s，设置、聊天记录和大家的上下文都搬过去了", old, new)
        await self.apply(settings)
        return True

    async def reset_member_context(self, key: str, pid: str) -> None:
        convo = self.conversation(key)
        await convo.stop()
        convo.reset_member(self._member(pid))

    def delete_history_entry(self, key: str, pid: str, index: int) -> None:
        p = self._member(pid)
        if not isinstance(p, HistoryParticipant):
            raise ConfigError("订阅 CLI 的上下文保存在 CLI 自己的会话里，只能整体清空")
        history = p.load_history(key)
        if not 0 <= index < len(history):
            raise ConfigError("没有这条记录")
        del history[index]
        p.save_history(key, history)

    async def import_sticker_set(self, value: str, limit: int = 120) -> int:
        """Download a Telegram sticker set into the library, labelled by each sticker's emoji."""
        name = parse_set_name(value)
        bot = self.telegram.controller or next(iter(self.telegram.bots.values()), None)
        if bot is None:
            raise ConfigError("需要至少一个能用的 bot 才能下载贴纸包")
        try:
            sticker_set = await bot.get_sticker_set(name)
        except Exception as e:
            raise ConfigError(f"找不到贴纸包 {name}：{e}") from None
        added = 0
        for st in sticker_set.stickers[:limit]:
            file = await bot.get_file(st.file_id)
            data = await bot.download_file(file.file_path)
            ext = "." + file.file_path.rsplit(".", 1)[-1] if "." in file.file_path else ".webp"
            self.stickers.add(f"sticker{ext}", data.read(), st.emoji or "表情", f"来自贴纸包 {sticker_set.title}",
                              tg_id=st.file_unique_id)
            added += 1
        self.stickers_changed()
        log.info("导入贴纸包 %s：%d 个", name, added)
        return added

    def viewer(self) -> Participant | None:
        """The member who describes every picture for everyone, in that mode; otherwise None."""
        choice = self.config.vision_member
        if choice in ("off", "help"):
            return None
        return next((p for p in self.participants if supports_vision(p.cfg) and choice in ("auto", p.id)), None)

    async def list_tool(self, raw: dict, env: dict | None = None, headers: dict | None = None) -> list[dict]:
        """Admin "列出工具": connect to a (possibly unsaved) server and list what it offers."""
        from .tools import normalize_tool

        t = normalize_tool(raw)
        stored = (self.secrets.get("tools") or {}).get(t["id"]) or {}
        server = ToolServer(**t, env={**(stored.get("env") or {}), **(env or {})},
                            headers={**(stored.get("headers") or {}), **(headers or {})})
        return await list_tools(server)

    async def keep_image(self, bot, media: tuple[str, str, str]) -> Path | None:
        """Save a picture sent in the group for a couple of days, so members who can see get it."""
        file_id, unique_id, ext = media
        folder = self.config.data_dir / "images"
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - IMAGE_KEEP_SECONDS
        for old in folder.iterdir():
            with suppress(OSError):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
        path = folder / f"{unique_id}{ext}"
        if not path.exists():
            try:
                file = await bot.get_file(file_id)
                data = await bot.download_file(file.file_path)
                path.write_bytes(data.read())
            except Exception as e:  # the message still goes through, just without the picture
                log.warning("图片没能下载：%s", e)
                return None
        return path

    async def look_at(self, chat_key: str, path: Path, unique_id: str, what: str, caption: str) -> str | None:
        """A short description of a picture, for the describe-for-everyone mode; ``None`` if nobody can look."""
        cached = self.seen.get(unique_id)
        if cached is not None:
            return cached
        viewer = self.viewer()
        if viewer is None:
            return None
        convo = self.conversations.get(chat_key)
        recent = [f"{m.speaker_name}: {m.text}" for m in convo.messages[-6:]] if convo else []
        prompt = look_prompt(what, caption, appearance_lines(self.config.enabled_participants), recent)
        await self.sink.activity(chat_key, viewer, "正在看图")
        try:
            text = await viewer.describe_image(path, prompt)
        except Exception as e:  # the message still goes through, just without a description
            log.warning("%s 看图失败：%s", viewer.name, e)
            return None
        finally:
            await self.sink.clear_activity(chat_key, viewer)
        self.seen.put(unique_id, text)
        log.info("%s 看图：%s", viewer.name, text)
        return text

    async def collect_sticker(self, bot, media: tuple[str, str, str], label: str, description: str) -> str:
        """Save a sticker, GIF or picture someone sent in the group; returns the reply for the group."""
        file_id, unique_id, ext = media
        known = self.stickers.find_tg(unique_id)
        if known is not None:
            return f"这个表情已经在表情库里了，叫「{known.label}」。"
        file = await bot.get_file(file_id)
        if file.file_size and file.file_size > MAX_STICKER_BYTES:
            raise ConfigError("文件超过 10 MB")
        data = await bot.download_file(file.file_path)
        s = self.stickers.add(f"collected{ext}", data.read(), label, description, tg_id=unique_id)
        self.stickers_changed()
        log.info("收藏表情「%s」", s.label)
        return f"⭐ 已收藏为「{s.label}」，所有人都能用。可以在管理页面的「表情」里改名、补说明，或只分给某几个成员。"

    async def list_models(self, pid: str) -> list[str]:
        pc = self._participant_config(pid)
        if pc.kind != "api":
            raise ConfigError("只有 API 类型的成员可以查询模型列表")
        # Listing is how a model gets picked, so it must work before one is filled in.
        p = self._make(replace(pc, model=pc.model or "-"))
        assert isinstance(p, HistoryParticipant)
        return await p.list_models()

    # ---- proactive posts ----

    def proactive_chat(self) -> str | None:
        """The configured group, else the only allowed one, else the most recently active."""
        if self.config.proactive_chat_id:
            return str(self.config.proactive_chat_id)
        if len(self.config.telegram.allowed_chat_ids) == 1:
            return str(self.config.telegram.allowed_chat_ids[0])
        groups = [c for c in self.chat_overview() if c["key"].startswith("-") and c["last_ts"]]
        return groups[0]["key"] if groups else None

    async def proactive_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self.events.beat()
            try:
                await self._alert_owner()
                await self._proactive_tick()
            except Exception:
                log.exception("主动发言检查出错")

    async def _alert_owner(self) -> None:
        """Several problems, or an error: tell the owner privately (the user may not watch the logs)."""
        text = self.events.alert_text()
        if not text or not self.telegram.connected:
            return
        self.events.mark_alerted()  # even if it can't be sent: not again every 30 seconds
        for uid in self.config.telegram.allowed_user_ids:
            with suppress(Exception):  # never opened a private chat with the bot
                await self.telegram.controller.send_message(uid, text)

    async def _proactive_tick(self) -> None:
        if self.journal.forget_expired():
            self.refresh_prompts()
        self._chronicle_tick()
        self._memory_tick()
        if self.telegram.connected:
            await self._followup_tick()
            await self._routine_tick()
        for p in list(self.participants):
            if self._asleep(p):  # no browsing, daily life or posts in its sleep
                continue
            if self._life_enabled(p.id):
                if self._life_task is None and self.life_schedule.due(self._life_policy(p)) and self._life_idle():
                    self.start_life(p.id)
                continue
            if p.id not in self._browsing_now and self.browsing.due(p.cfg):
                self._browsing_now.add(p.id)
                task = asyncio.create_task(self._browse(p))
                task.add_done_callback(lambda _t, pid=p.id: self._browsing_now.discard(pid))

        if not self.telegram.connected:  # no chat app running: nowhere to post
            return
        for p in list(self.participants):
            if self._life_enabled(p.id) or self._asleep(p):
                continue
            if not self.proactive.due(p.cfg):
                continue
            chat = self.proactive_chat()
            convo = self.conversation(chat) if chat else None
            quiet = seconds_quiet(convo.last_activity) if convo else 0
            if convo is None or convo.busy or quiet < p.cfg.proactive_idle_minutes * 60:
                self.proactive.postpone(p.cfg)  # not now; look again in a few minutes
                continue
            await self._proactive_attempt(p, convo)

    # ---- members' continuing daily life ----

    def _life_enabled(self, pid: str) -> bool:
        return self.config.life.enabled and pid in self.config.life.members

    def _life_days(self, pid: str) -> int:
        return self.config.life.member_days.get(pid, self.config.life.per_day)

    def _life_left(self, pid: str) -> float:
        """Personal activities left today, independent of passive private replies."""
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None or not self._life_enabled(pid):
            return 0
        limit = self._life_days(pid)
        return float("inf") if limit == 0 else limit - self.life_schedule.plan(self._life_policy(p)).posts

    def _life_private_limit(self, pid: str) -> int:
        return self.config.life.passive_member_days.get(pid, self.config.life.passive_per_day)

    def _life_private_left(self, pid: str) -> float:
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None or not self._life_enabled(pid):
            return 0
        limit = self._life_private_limit(pid)
        return float("inf") if limit == 0 else limit - self.life_private.plan(self._life_policy(p)).posts

    def _life_charge(self, pid: str) -> None:
        p = next((x for x in self.participants if x.id == pid), None)
        if p is not None:
            if self._life_private_left(pid) <= 0:
                raise ConfigError("这位朋友今天的被动私聊次数已用完")
            self.life_private.plan(self._life_policy(p)).posts += 1
            self.life_private.save()

    def _life_found(self, pid: str, item, result: dict) -> None:
        """Progress from a daily-life look-around also goes into the member's 见闻."""
        sources = result.get("sources") or []
        first = next((s for s in sources if isinstance(s, dict) and s.get("url")), None)
        if first is None:
            return
        entry = {
            "title": result.get("title") or first.get("title") or (item.title if item else "随手看到的内容"),
            "summary": result.get("progress") or "",
            "take": result.get("take") or "",
            "url": first["url"],
            "read_level": first.get("read_level") or "",
            "interest": 4 if result.get("follow_up") == "share" else 3,
        }
        if self.journal.add(self._member(pid).cfg, [entry]):
            self.refresh_prompts()

    def _life_policy(self, p: Participant):
        policy = self.config.life
        return replace(p.cfg, browse=True, browse_per_day=self._life_days(p.id), browse_hours=policy.hours,
                       proactive=True, proactive_per_day=policy.shares_per_day,
                       proactive_hours=policy.hours, proactive_idle_minutes=policy.quiet_minutes)

    def _life_idle(self) -> bool:
        return (not self.games.running and not any(convo.busy for convo in self.conversations.values())
                and not self._browsing_now.intersection(self.config.life.members))

    def diary_entries(self) -> list[dict]:
        """Every member's diary: daily-life entries plus game memories, newest first."""
        # Game entries remain owned by GameMemoryStore: edits/hiding apply here too.
        diary = self.life.diary.view()
        for m in self.game_memories.memories:
            if m.status == "hidden":
                continue
            provenance = f"旁观 {m.watched} 玩游戏时：" if m.watched else "自己玩游戏时："
            diary.append({"id": "game:" + m.id, "member": m.member, "ref": m.id, "kind": "game",
                          "title": m.subject, "event": provenance + m.story, "thoughts": provenance + m.story,
                          "feelings": {m.feeling: m.intensity}, "topics": m.tags, "sources": [], "at": m.at,
                          "day": datetime.fromtimestamp(m.at).date().isoformat(), "strength": round(m.strength(time.time()), 3)})
        return sorted(diary, key=lambda e: e["at"], reverse=True)

    # ---- the home ----

    def _residents(self) -> dict[str, str]:
        return {p.id: p.name for p in self.config.enabled_participants}

    def home_view(self) -> dict:
        chat = self.proactive_chat()
        convo = self.conversations.get(chat) if chat else None
        phone = {}
        for m in convo.messages if convo else ():
            if not m.from_user:
                phone[m.speaker_id] = max(phone.get(m.speaker_id, 0.0), m.ts)
        return self.home.view(self._residents(), phone)

    def home_text(self) -> str:
        """`/家` in the group: who is where, in fixed words anyone there may see."""
        return self.home.guest_lines(self._residents())

    def diary_book(self, pid: str, day: str = "") -> dict:
        """Her diary on the bedroom desk. The cover lists days; opening a day is reading it, and she will know."""
        if pid not in self._residents():
            raise ConfigError("没有这位成员")
        entries = [e for e in self.diary_entries() if e["member"] == pid]
        days = sorted({e["day"] for e in entries}, reverse=True)
        if not day:
            return {"member": pid, "days": [{"day": d, "count": sum(e["day"] == d for e in entries)} for d in days]}
        if day not in days:
            raise ConfigError("这一天没有日记")
        self.home.read_diary(pid, day)
        return {"member": pid, "day": day, "entries": [e for e in entries if e["day"] == day],
                "reads": [r for r in self.home.diary_reads if r["member"] == pid][-10:]}

    async def choose_look(self, pid: str) -> dict:
        """She picks her own colour and accessory for the house, once when the owner asks."""
        if pid not in self._residents():
            raise ConfigError("没有这位成员")
        now = self.home.looks.get(pid, {})
        options = "、".join(f"{key}（{name}）" for key, name in ACCESSORIES.items())
        prompt = (now_line() + f"\n这不是群聊。{owner.name()}在家里给每个人做了一个方块小人，你可以给自己挑外形。"
                  f"\n现在是颜色 {now.get('color')}、配饰 {ACCESSORIES.get(now.get('accessory'), '无')}。"
                  f"\n配饰只能从这些里选一个：{options}。颜色写成 #rrggbb，按你自己的喜好挑。"
                  '\n只输出 JSON：{"color":"#rrggbb","accessory":"清单里的英文 ID","why":"一句话，为什么这么挑"}')
        choice = object_reply(await self._life_ask(pid, prompt))
        try:
            look = self.home.set_look(pid, str(choice.get("color", "")), str(choice.get("accessory", "")))
        except ValueError as e:
            raise ConfigError(f"她挑的不行：{e}") from None
        why = str(choice.get("why") or "")[:200]
        self.life.event(pid, "look", f"给自己挑了外形：{ACCESSORIES[look['accessory']]}，{look['color']}" + (f"。{why}" if why else ""))
        return {**look, "why": why}

    def life_status(self) -> dict:
        names = {p.id: p.name for p in self.config.participants}
        diary = self.diary_entries()
        plans = {}
        for p in self.participants:
            if p.id in self.config.life.members:
                policy = self._life_policy(p)
                plans[p.id] = {**self.life_schedule.describe(policy),
                               "shares_today": self.life_posts.plan(policy).posts,
                               "passive_today": self.life_private.plan(policy).posts,
                               "passive_limit": self._life_private_limit(p.id),
                               "limit": self._life_days(p.id)}
        return {**self.life.public(), "names": names, "settings": asdict(self.config.life), "games": self.games.status(),
                "running": self._life_member, "plans": plans, "diary": diary}

    def start_life(self, pid: str) -> dict:
        if not self._life_enabled(pid):
            raise ConfigError("请先在日常页开启日常，并把这位成员选进去")
        if self._life_task is not None or self.games.running or not self._life_idle():
            raise ConfigError("角色或群聊正在忙，稍后再试")
        p = self._member(pid)
        policy = self._life_policy(p)
        if self._life_left(pid) <= 0:
            raise ConfigError("这位成员今天的日常活动次数已用完")
        # Reserve the activity before making any model calls, including manual runs.
        self.life_schedule.record(policy, "activity")
        self._life_member = pid
        self._life_task = asyncio.create_task(self._run_life(pid))
        return self.life_status()

    def _game_actor(self, pid: str, folder: Path):
        return make_game_actor(self._member(pid).cfg, folder, self.config.round.call_timeout)

    def _game_chat_context(self, pid, query):
        games = getattr(self, 'games', None)
        if not games or games.running != pid or not games.social:
            return ''
        # Public replies get the player's current game facts, never the private conversation.
        return ('[你正在玩的真实游戏]\n' + games.social.latest_scene
                + '\n这是当前进度，可以聊眼前局面。私下和朋友聊过的原话不能自动当作群里都知道。[/当前游戏]')

    def _game_peers(self, player):
        if not self.games.config['companions']:
            return []
        return [p.id for p in self.participants if p.id != player and self._life_enabled(p.id)
                and not self._asleep(p) and self._life_private_left(p.id) > 0]

    def _game_social(self, player, folder, requested_peer=None):
        actors = {}
        async def ask(pid, prompt):
            if pid not in actors:
                pc = self._member(pid).cfg
                work = folder / 'conversation' / pid
                actor = participant_class(pc)(replace(pc, web_search=False, max_history=8),
                          SessionStore(work / 'sessions.json'), work, min(90, self.config.round.call_timeout))
                actor.set_system_prompt(self.system_prompt_for(pc) + '\n这次是在朋友身边看游戏，按本轮要求返回 JSON；不使用工具。')
                actor.turn_context = lambda prompt, pc=pc: self._chat_context(pc, prompt)
                actors[pid] = actor
            return await actors[pid].reply('game-company', prompt)
        def present(pid):
            return any(p.id == pid and not self._asleep(p) and self._life_enabled(pid) for p in self.participants)
        def feed(since):
            chat = self.proactive_chat()
            convo = self.conversation(chat) if chat else None
            return [{'speaker': m.speaker_name, 'text': m.text[:1000]} for m in convo.messages if m.ts >= since] if convo else []
        async def wait_public():
            chat = self.proactive_chat()
            convo = self.conversation(chat) if chat else None
            if convo:
                count = len(convo.messages)
                for _ in range(100):
                    if not convo.busy or len(convo.messages) > count:
                        break
                    await asyncio.sleep(0.2)
        return GameConversation(player, folder, {p.id: p.name for p in self.config.participants},
            self.life, ask, lambda: self._game_peers(player), self._life_charge,
            publish=self._game_share, can_share=lambda: self._game_share_ready(player),
            public_feed=feed, present=present, changed=self.refresh_prompts,
            requested_peer=requested_peer, wait_public=wait_public)

    def _game_share_ready(self, pid):
        chat = self.proactive_chat()
        convo = self.conversation(chat) if chat else None
        if (not self.games.config['share_to_group'] or not self._life_enabled(pid)
                or not self.telegram.connected or convo is None or convo.busy):
            return False
        policy = self._life_policy(self._member(pid))
        plan = self.life_posts.plan(policy)
        return (self.life_posts.in_hours(policy) and plan.posts < policy.proactive_per_day
                and plan.attempts < policy.proactive_per_day * 2 and time.time() - self._game_last_share >= 120)

    async def _game_share(self, pid, caption, image, scene):
        if not self._game_share_ready(pid):
            return None
        image = Path(image)
        run_id = self.games.latest.get('run_id') or self.games.latest.get('id')
        expected = self.games.root / 'runs' / (run_id or '__missing__') / 'screenshots'
        if (self.games.running != pid or not image.resolve().is_relative_to(expected.resolve())
                or image.is_symlink() or image.suffix != '.png' or not image.is_file()):
            raise ConfigError('只允许分享本次游戏生成的截图')
        convo, speaker = self.conversation(self.proactive_chat()), self._member(pid)
        policy = self._life_policy(speaker)
        self.life_posts.record(policy, None)
        at = time.time()
        future = convo.start_proactive(speaker, lambda _: '', max(1, speaker.cfg.proactive_replies),
            prepared_text=caption, discussion=True, image_path=image, image_description=scene)
        if future is None:
            return None
        posted = await asyncio.shield(future)
        if not posted:
            return None
        self.life_posts.plan(policy).posts += 1
        self.life_posts.save()
        self._game_last_share = time.time()
        return {'text': posted, 'at': at}

    def _record_game(self, receipt: dict) -> None:
        if receipt.get('publication_target') == 'local_preview':
            return  # Preview chats must not become memories of real Telegram messages.
        pid, run_id = receipt['member'], receipt['id']
        if any(a.get('game_run') == run_id for a in self.life.artifacts):
            return
        if not receipt['actions']:
            self.life.event(pid, 'blocked', '游戏没有执行动作：' + receipt['stopped_because'])
            return
        events = self.games.events(receipt)
        moments = highlights(events)
        last = receipt['last']
        title = f"《杀戮尖塔》游玩回执：{receipt['actions']} 个实际动作"
        facts = (f"你实际操作了 {receipt['actions']} 步，耗时 {round(receipt['seconds'])} 秒；"
                 f"最后记录在第 {last.get('floor')} 层，生命 {last.get('current_hp')}/{last.get('max_hp')}。"
                 f"已确认打赢的战斗楼层：{receipt['battles_won'] or '尚无'}。"
                 f"停止原因：{receipt['stopped_because']}；"
                 + ("已保留游戏自己的房间检查点，可继续。" if receipt['checkpoint_available'] else "本次没有确认可继续的存档。"))
        content = (f"# {title}\n\n{facts}\n\n" + '\n'.join('- ' + line for line in moments)
                   + f"\n\n回执 ID：{run_id}\n操作记录 SHA-256：{receipt.get('trace_sha256', '')}\n")
        artifact = self.life_workbench.save(pid, 'game', title, content)
        artifact['game_run'] = run_id
        artifact['game_started_at'] = receipt.get('started_at', artifact['created_at'])
        artifact['game_facts'] = facts
        artifact['game_highlights'] = moments
        self.life.record_artifacts(pid, {'artifacts': [artifact]})
        self.life.event(pid, 'play', facts)
        climb, ended = self.game_memories.attach(receipt)
        if ended:
            self._spawn(self._remember_climb(climb))  # also decides what next
        else:
            self.game_memories.done(pid)  # it played again: the last plan no longer holds
        self.refresh_prompts()

    async def _remember_climb(self, climb_id: str) -> list:
        """A climb ended: read what happened from its play sessions and remember it, with feelings."""
        store = self.game_memories
        climb = store.climbs.get(climb_id)
        if not climb or climb.get('remembered'):
            return []
        climb['remembered'] = True
        store.save()
        member = climb['member']
        try:
            receipts = {r['id']: r for r in self.games.receipts(member)}
            events, watched = [], {}
            for rid in climb['receipts']:
                if rid not in receipts:
                    continue
                try:
                    part = self.games.events(receipts[rid])
                except ConfigError as e:
                    log.warning('游戏记忆跳过了一段游玩 %s：%s', rid, e)
                    continue
                events += part
                for peer in receipts[rid].get('companions', []):
                    watched.setdefault(peer, []).extend(part)
            sig = game_signals(events, climb.get('game_over'))
            if not worth_remembering(sig):
                return []
            sig['stood_out'] = store.record_climb(member, sig)
            player = self._member(member)
            added = await self._write_game_memories(player, climb_id, sig)
            for peer, seen in watched.items():
                watcher = next((p for p in self.participants if p.id == peer and p.id != member), None)
                peer_sig = game_signals(seen)
                if watcher is not None and worth_remembering(peer_sig):
                    added += await self._write_game_memories(watcher, climb_id, peer_sig, watched=player.name)
            for text in store.nominations(member, player.name):
                meme = self.memes.add(text, '', '游戏记忆')
                if meme is not None:  # waits for the owner to switch it on in the meme library
                    self.memes.update(meme.id, {'enabled': False})
            log.info('%s 记住了这局《杀戮尖塔》：%s', player.name, '；'.join(f'{m.feeling} {m.story}' for m in added) or '没什么')
            await self._decide_next(player, sig)
            self.refresh_prompts()
            return added
        except Exception as e:  # noqa: BLE001 - a failed write-up must not stop the game loop
            log.warning('游戏记忆没写成：%s', e)
            return []

    async def _cheap_ask(self, prompt: str, system: str, purpose: str = 'game_memory') -> str | None:
        """One tool-less answer from the cheapest member, in its own sessions; never Claude."""
        writer = self._chronicle_writer()
        if writer is None or writer.cfg.kind == 'claude_cli':
            return None
        actor = participant_class(writer.cfg)(writer.cfg, SessionStore(self.config.data_dir / f'{purpose}_sessions.json'),
                                              self.config.data_dir / 'work' / purpose / writer.id,
                                              self.config.round.call_timeout)
        actor.set_system_prompt(system)
        key = '__game_memory_' + uuid.uuid4().hex
        try:
            return await actor.reply(key, prompt)
        except ParticipantError as e:
            log.warning('游戏记忆的模型调用失败，改用模板：%s', e)
            return None
        finally:
            actor.reset(key)

    async def _write_game_memories(self, who: Participant, climb_id: str, sig: dict, watched: str = '') -> list:
        """1–3 memory blocks in the member's voice, by the cheapest model; Claude never writes them."""
        recent = [m for m in self.game_memories.items(who.id)][-5:]
        raw = await self._cheap_ask(memory_prompt(who.name, who.cfg.persona, sig, recent, watched),
                                    '你帮朋友群里的成员记下他们玩游戏的经历和感受。只按要求输出 JSON。')
        if raw is not None and re.fullmatch(r'\s*\[\s*\]\s*', raw):
            return []  # nothing worth remembering, by its own judgement
        blocks = parse_memories(raw or '', sig, who.id, climb_id, watched)
        if not blocks:
            blocks = template_memories(sig, who.id, climb_id, watched)
        return self.game_memories.add(blocks)

    async def _decide_next(self, player: Participant, sig: dict) -> dict:
        """After a climb the member itself decides what next; that plan is kept and followed."""
        store = self.game_memories
        raw = await self._cheap_ask(decision_prompt(player.name, player.cfg.persona, sig, store.mood(player.id),
                                                    store.opinion_lines(player.id)),
                                    '你替朋友群里的成员说出她打完游戏后自己的打算。只按要求输出 JSON。')
        intent = store.decide(player.id, parse_decision(raw or '', sig) or fallback_decision(sig))
        log.info('%s 打完这局的打算：%s（%s）', player.name, CHOICES[intent['choice']], intent['why'])
        self.life.event(player.id, 'plan', f"打完《杀戮尖塔》：{CHOICES[intent['choice']]}，{intent['why']}")
        if intent['say']:
            self._spawn(self._say_after_game(player, intent['say']))
        if intent['choice'] == 'again':
            self._spawn(self._play_again(player.id))
        return intent

    async def _say_after_game(self, player: Participant, text: str) -> None:
        """Its own line about the climb (a question for friends, a wish for another game) in the group."""
        chat = self.proactive_chat()
        if not chat or self._asleep(player):
            log.info('%s 想在群里说「%s」，但现在不方便（没有群或在睡觉），没发', player.name, text)
            return
        convo = self.conversation(chat)
        for _ in range(12):  # a quiet moment, up to a minute
            if not convo.busy:
                break
            await asyncio.sleep(5)
        future = convo.start_proactive(player, lambda unseen: '', 1, prepared_text=text)
        if future is not None:
            with suppress(Exception):
                await asyncio.wait_for(future, self.config.round.call_timeout + 60)

    async def _play_again(self, pid: str) -> None:
        """It wanted another round right away: play, if its daily life and the owner's settings allow now."""
        for _ in range(60):
            if self._life_task is None and not self.games.running and (self._game_task is None or self._game_task.done()):
                break
            await asyncio.sleep(2)
        else:
            return
        if not self.games.available(pid, automatic=True) or not self._life_enabled(pid) or self._life_left(pid) <= 0:
            log.info('%s 想马上再来一局，但现在不方便（电脑有人在用、没开自主游玩或今天的活动用完了），先记着', pid)
            return
        self.life_schedule.record(self._life_policy(self._member(pid)), 'activity')
        self._game_task = asyncio.create_task(self._play_game(pid, automatic=True))

    async def _play_game(self, pid: str, *, automatic=False, peer=None) -> dict:
        self._member(pid)  # Recheck enabled membership before launching anything.
        self.home.begin(pid, "play", "《杀戮尖塔》")
        try:
            result = await self.games.play(pid, automatic=automatic, peer=peer)
        except asyncio.CancelledError:
            if self.games.latest.get('member') == pid and 'ended_at' in self.games.latest:
                self._record_game(self.games.latest)
            raise
        finally:
            self.home.end(pid)
        self._record_game(result)
        return result

    def start_game(self, pid: str, peer=None) -> dict:
        self._member(pid)
        if peer is not None and peer not in self._game_peers(pid):
            raise ConfigError('这位朋友现在不方便一起玩，可以让成员自己邀请其他朋友')
        if self._game_task is not None and not self._game_task.done():
            raise ConfigError('已经有成员在玩游戏')
        if self._life_task is not None or not self.games.available(pid):
            raise ConfigError('成员正在活动，或游戏未授权、次数已用完')
        if self._life_enabled(pid):
            if self._life_left(pid) <= 0:
                raise ConfigError('这位成员今天的个人活动次数已用完')
            self.life_schedule.record(self._life_policy(self._member(pid)), 'activity')
        self._game_task = asyncio.create_task(self._play_game(pid, peer=peer))
        return self.games.status()

    async def stop_game(self) -> None:
        life_was_playing = self.games.running and self._life_task is not None
        self.games.stop()
        if self._game_task is not None:
            if not self._game_task.done() and not self.games.running:
                self._game_task.cancel()  # Stop requested before the queued task could launch.
            with suppress(asyncio.CancelledError, ConfigError):
                await self._game_task
            self._game_task = None
        if life_was_playing:
            with suppress(asyncio.CancelledError):
                await self._life_task

    def _life_context(self, pid: str) -> str:
        p = self._member(pid)
        chat = self.proactive_chat()
        convo = self.conversation(chat) if chat else None
        recent = "\n".join(f"{m.speaker_name}：{m.text[:600]}" for m in convo.messages[-10:]) if convo else "暂无新消息"
        interests = p.cfg.browse_topics or p.cfg.proactive_topics or "按自己的兴趣选择"
        friends = "\n".join(
            f"- {m.name}（ID {m.id}）：喜欢 {m.browse_topics or m.proactive_topics or '随缘聊天'}；"
            f"可以 {'、'.join(self._abilities(m, self.config)) or '聊天，依据朋友的转述接话'}"
            for m in self.config.enabled_participants if m.id != pid
        )
        return (f"你的长期兴趣：{interests}\n朋友公开的喜好和能力：\n{friends}\n"
                "个人行动可让应用代读具体公开网页，或保存笔记、收藏、创作文字。成果归自己保留，分享仍需另行决定。"
                "没有搜索能力时可请朋友找链接，不能假装已经查到。个人活动与被朋友找去私聊分开计数。\n"
                f"最近的公开群聊（不是给你的新任务）：\n{recent}\n"
                f"现在是否适合发到群里：{'可以自行决定' if self._life_share_ready(p, convo) else '暂不合适，可以先留着，下次再决定'}。\n"
                + self.life.diary.recall(pid, "最近看了什么")
                + ("\n" + told if (told := self.home.tell(pid)) else ""))

    def _life_moved(self, pid: str, action: str, decision: dict, item) -> None:
        """A daily-life step chose what to do: she goes to the room where it happens."""
        title = item.title if item else ""
        if action == "work":
            plan = decision.get("work") if isinstance(decision.get("work"), dict) else {}
            self.home.begin(pid, "write" if plan.get("operation") == "write" else "read", title)
        elif action == "private":
            peer = str(decision.get("peer") or "")
            friend = next((p for p in self.participants if p.id == peer), None)
            home = friend is not None and self.routines.status(friend) not in (ASLEEP, BUSY)
            self.home.begin(pid, "private", title or str(decision.get("reason") or "")[:60], peer=peer, peer_home=home)
        elif action == "idle":
            self.home.begin(pid, "idle", str(decision.get("reason") or ""))
        elif action != "play":  # a game moves her when it really starts
            self.home.begin(pid, action, title or ("随便看看" if action == "browse" else ""))

    def _life_can_browse(self, pid: str) -> bool:
        p = self._member(pid)
        return bool(p.tools) or (p.cfg.web_search and supports_web_search(p.cfg))

    async def _life_ask(self, pid: str, prompt: str) -> str:
        import uuid
        p = self._member(pid)
        # Separate work and sessions prevent background JSON/private dialogue from
        # entering the actor's public CLI/API conversation or racing its prompt file.
        actor = participant_class(p.cfg)(p.cfg, self.life_sessions,
                                        self.config.data_dir / "work" / "life" / pid,
                                        self.config.round.call_timeout)
        actor.tools = tools_for(p.cfg, self.config.tools)
        actor.set_system_prompt(self.system_prompt_for(p.cfg, background=True))
        key = "__life_" + uuid.uuid4().hex
        try:
            return await actor.reply(key, prompt)
        finally:
            self.life_sessions.clear_one(key, pid)
            actor.reset(key)

    async def _life_verify(self, pid: str, result: dict) -> dict:
        return await self.life_workbench.research(pid, result, self.life.prompt_section(pid) + "\n" + self._life_context(pid))

    async def _life_video(self, pid: str, url: str) -> dict:
        from .tools import call_tool
        from .video_reading import video_receipt
        server = next((s for s in tools_for(self._member(pid).cfg, self.config.tools)
                       if "watch_video" in s.allowed), None)
        if server is None:
            return {"status": "unavailable", "reason": "这位成员没有获准使用看视频工具，可以找有工具的朋友"}
        try:
            result = await call_tool(server, "watch_video", {"url": url, "frames": 0})
        except ConfigError as error:
            return {"status": "unavailable", "reason": str(error)}
        return video_receipt(result, url)

    async def _life_work(self, pid, item, plan):
        # Continue from actual files, not merely a promise or an old summary.
        previous = []
        known = {}
        for artifact in self.life.artifacts:
            if artifact["owner"] != pid:
                continue
            if artifact["kind"] == "reading":
                try:
                    reading = self.life_workbench.read_artifact(artifact)
                except ConfigError:
                    continue
                known.update({source_key(s["url"]): s for s in artifact.get("sources", [])})
                try:
                    requested = json.loads(reading).get("requested_url", "")
                    if source_key(requested) and artifact.get("sources"):
                        known[source_key(requested)] = artifact["sources"][0]
                except (ValueError, AttributeError):
                    pass
            if artifact.get("interest_id") == item.id and artifact["kind"] != "reading":
                previous.append(artifact)
        context = self._life_context(pid)
        for artifact in previous[-2:]:
            try:
                content = self.life_workbench.read_artifact(artifact)
            except ConfigError:
                context += "\n此前的成果文件已经不可用，不能假装已重新读过。"
            else:
                context += f"\n你自己此前实际保存的作品（可以在此基础上继续）：\n{content[:10000]}"
        # A legacy sighting is only a candidate. If a chosen document needs it,
        # perform the missing reads before writing instead of trusting its old summary.
        selected = plan.get("source_urls") or []
        if plan.get("operation") == "write" and isinstance(selected, list):
            missing = list(dict.fromkeys(str(u) for u in selected if source_key(str(u)) not in known))
            candidates = self.life.known_sources(pid)
            candidates.update({source_key(e.url): {"url": e.url} for e in self.journal.of(pid)})
            if missing and len(missing) <= 2 and all(source_key(u) in candidates for u in missing):
                read = await self.life_workbench.research(pid, {"sources": [{"url": u} for u in missing]},
                                                        context + f"\n为了写「{item.title}」，先核实这些旧见闻：{item.goal}")
                self.life.record_artifacts(pid, read, item)
                if read.get("outcome") != "progress":
                    return read
                for artifact in read.get("artifacts", []):
                    body = json.loads(self.life_workbench.read_artifact(artifact))
                    for source in artifact.get("sources", []):
                        known[source_key(source["url"])] = source
                        if source_key(body.get("requested_url", "")):
                            known[source_key(body["requested_url"])] = source
        return await self.life_workbench.work(pid, item, plan, context, known)

    async def _run_life(self, pid: str) -> None:
        try:
            p = self._member(pid)
            names = {m.id: m.name for m in self.config.enabled_participants}
            engine = LifeEngine(self.life, self._life_ask, self._life_share, self._life_context,
                                names, self.refresh_prompts, charge=self._life_charge, found=self._life_found,
                                can_browse=self._life_can_browse, verify=self._life_verify, work=self._life_work,
                                play=lambda member, peer=None: self._play_game(member, automatic=True, peer=peer),
                                can_play=lambda member: (self.games.available(member, automatic=True)
                                                         and not self.game_memories.resting(member)),
                                play_note=self.game_memories.play_note, moved=self._life_moved)
            can_explore = self._life_can_browse(pid)
            # Friends it can message: the others in daily life with activities left today.
            awake = {m.id for m in self.participants if not self._asleep(m)}
            peers = [m for m in self.config.life.members if m != pid and m in awake and self._life_private_left(m) > 0]
            action = await engine.step(pid, peers, can_explore)
            log.info("%s 完成一次日常活动：%s", p.name, action)
        except asyncio.CancelledError:
            self.life.event(pid, "interrupted", "本次活动已中断，已经发生的进展和私聊仍然保留")
            raise
        except Exception as error:
            log.warning("%s 日常活动失败：%s", pid, error)
            self.life.event(pid, "error", str(error))
        finally:
            self.home.end(pid)
            self._life_task = None
            self._life_member = ""
            self.refresh_prompts()

    def _life_share_ready(self, p: Participant, convo: Conversation | None) -> bool:
        if not self._life_enabled(p.id) or not self.telegram.connected or convo is None or convo.busy:
            return False
        policy = self._life_policy(p)
        plan = self.life_posts.plan(policy)
        return (self.life_posts.in_hours(policy) and plan.posts < policy.proactive_per_day
                and plan.attempts < policy.proactive_per_day * 2
                and seconds_quiet(convo.last_activity) >= policy.proactive_idle_minutes * 60)

    async def _life_share(self, pid: str, message: str, sources=()) -> str | None:
        p = self._member(pid)
        chat = self.proactive_chat()
        convo = self.conversation(chat) if chat else None
        if not self._life_share_ready(p, convo):
            return None
        if sources and convo.shared_recently(sources):
            return None
        policy = self._life_policy(p)
        # Sharing is selected by the actor; reserve a bounded attempt before posting.
        self.life_posts.record(policy, None)
        future = convo.start_proactive(
            p, lambda unseen: build_personal_share_prompt(unseen, message, sources, me=p.id),
            max(1, p.cfg.proactive_replies), sources=sources, discussion=True,
        )
        if future is None:
            return None
        posted = await future
        if posted:
            self.life_posts.plan(policy).posts += 1
            self.life_posts.save()
        return posted

    async def _cancel_life(self) -> None:
        task = self._life_task
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    # ---- drawing stickers for each other ----

    def _on_draw(self, convo: Conversation, drawer: Participant, request: tuple[str, str], requester) -> None:
        pc = drawer.cfg
        if not (pc.can_draw and supports_drawing(pc)):
            return  # the tag is ignored for members that cannot draw
        if drawer.id in self.drawings.busy:
            self._spawn(convo.deliver_drawing(drawer, "手上这张还没画完，排个队。", None, requester))
            return
        if self.drawings.remaining(pc) <= 0:
            self._spawn(convo.deliver_drawing(drawer, "今天画不动了，明天再说。", None, requester))
            return
        self.drawings.busy.add(drawer.id)
        self._spawn(self._draw(convo, drawer, *request, requester))

    async def _draw(self, convo: Conversation, drawer: Participant, label: str, description: str, requester) -> None:
        asker = requester.speaker_name if requester is not None else ""
        await convo.sink.activity(convo.key, drawer, f"正在画表情包：{label}")
        log.info("[%s] %s 开始画「%s」：%s", convo.key, drawer.name, label, description)
        ok = False
        try:
            path = await drawer.draw(build_draw_prompt(label, description, self.config.enabled_participants))
            label = self.stickers.unique_label(label)
            note = f"{drawer.name} 给 {asker} 画的：{description}" if asker else f"{drawer.name} 画的：{description}"
            sticker = self.stickers.add(f"drawing{path.suffix}", path.read_bytes(), label, note[:300])
            ok = True
            self.refresh_prompts()  # everyone can use the new sticker now
            text = f"画好了 @{asker}，拿去用。" if asker and asker != drawer.name else "画好了。"
            log.info("[%s] %s 画好了「%s」", convo.key, drawer.name, label)
            await convo.sink.clear_activity(convo.key, drawer)
            await convo.deliver_drawing(drawer, text, sticker, requester)
        except Exception as e:
            log.warning("%s 画图失败：%s", drawer.id, e)
            await convo.sink.clear_activity(convo.key, drawer)
            await convo.deliver_drawing(drawer, "这张翻车了，没画出来。", None, requester)
        finally:
            self.drawings.busy.discard(drawer.id)
            self.drawings.record(Drawing(time.time(), drawer.id, asker, label, description, ok))
            self.refresh_prompts()  # the drawer's remaining count changed

    # ---- asking about things later ----

    def _followup_asker(self, item) -> Participant | None:
        """Whoever noted it asks; otherwise someone else, Claude last (it shares the owner's quota)."""
        started = {p.id: p for p in self.participants if not self._asleep(p)}
        if item.noted_by in started:
            return started[item.noted_by]
        others = sorted(started.values(), key=lambda p: p.cfg.kind == "claude_cli")
        return others[0] if others else None

    async def _followup_tick(self) -> None:
        for item in self.followups.due()[:1]:  # at most one per check
            convo = self.conversation(item.chat)
            asker = self._followup_asker(item)
            if asker is None:  # everyone is asleep: when someone is up; no members at all: drop it
                self.followups.later(item) if self.participants else self.followups.finish(item, asked=False)
                continue
            future = None if convo.busy else convo.start_proactive(
                asker, lambda unseen, item=item: build_followup_prompt(item, unseen, now_line()), 1)
            if future is None:  # the group is busy: a bit later
                self.followups.later(item)
                continue
            log.info("[%s] %s 想起来要问：%s", convo.key, asker.name, item.text)
            try:
                posted = await asyncio.wait_for(future, self.config.round.call_timeout + 60)
            except Exception as e:
                log.warning("跟进没问成：%s", e)
                posted = None
            self.followups.finish(item, asked=posted is not None)
            self.refresh_prompts()

    # ---- sleeping, waking up ----

    def _routine_label(self, p: Participant) -> str:
        state = self.routines.status(p)
        return "💤 在睡觉" if state == ASLEEP else busy_doing(p.cfg) if state == BUSY else ""

    def _asleep(self, p: Participant) -> bool:
        return self.routines.status(p) == ASLEEP

    async def _routine_tick(self) -> None:
        """Someone just woke up or is going to bed: it may say so, or answer what it missed."""
        chat = self.proactive_chat()
        convo = self.conversation(chat) if chat else None
        for p in self.participants:
            self.home.settle(p.id, self.routines.status(p))
        for p, before, after in self.routines.changes(self.participants, self._routine_last):
            if before == ASLEEP:
                self._routine_events.append((p.id, "wake", time.time()))
            elif after == ASLEEP and convo is not None and seconds_quiet(convo.last_activity) < 1800:
                if random.random() < GOODNIGHT_CHANCE:
                    self._routine_events.append((p.id, "sleep", time.time()))
        self._routine_events = [e for e in self._routine_events if time.time() - e[2] < 7200]
        if convo is None or convo.busy or not self._routine_events:
            return
        pid, kind, _ = self._routine_events.pop(0)
        p = next((m for m in self.participants if m.id == pid), None)
        if p is None:
            return
        if kind == "wake":
            # Only what was said while it slept: after a reset, older talk is unseen too, but not missed.
            asleep = fell_asleep(p.cfg, self.routines.now())
            since = asleep.timestamp() if asleep else 0.0
            missed = [m for m in convo._unseen(p) if m.from_user and m.ts >= since]
            # Nothing missed: only sometimes a "morning", and not from Claude (it shares the owner's quota).
            if not missed and (p.cfg.kind == "claude_cli" or random.random() >= MORNING_CHANCE or not convo.messages):
                return
            make = lambda unseen: wake_prompt([m for m in unseen if m.ts >= since], now_line(), bool(missed))  # noqa: E731
        else:
            make = lambda unseen: goodnight_prompt(unseen, now_line())  # noqa: E731
        future = convo.start_proactive(p, make, 1 if kind == "wake" else 0,
                                      show_images_since=since if kind == "wake" else None)
        if future is None:
            return
        log.info("[%s] %s %s", convo.key, p.name, "睡醒了，看看群" if kind == "wake" else "要去睡了")
        try:
            await asyncio.wait_for(future, self.config.round.call_timeout + 60)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 的作息发言没成：%s", p.name, e)

    # ---- the group's own history ----

    def _chronicle_tick(self, now: datetime | None = None) -> None:
        if self._chronicle_running:
            return
        chat = self.proactive_chat()
        if not chat:
            return
        convo = self.conversation(chat)
        now = now or datetime.now()
        oldest = (now - timedelta(days=3)).date().isoformat()  # catch up on a few missed days at most
        days = Counter(datetime.fromtimestamp(m.ts).date().isoformat() for m in convo.messages if m.ts)
        candidates = [d for d, n in days.items() if n >= CHRONICLE_MIN_MESSAGES and d >= oldest]
        day = self.chronicle.day_to_write(candidates, now)
        if day is not None:
            self._chronicle_running = True
            self._spawn(self._write_chronicle(convo, day))

    # ---- learning about the user from the day's chat ----

    def _memory_tick(self, now: datetime | None = None) -> None:
        if self._memory_running or self.config.memory_learn not in ('review', 'auto'):
            return
        chat = self.proactive_chat()
        if not chat:
            return
        convo = self.conversation(chat)
        now = now or datetime.now()
        said = Counter(datetime.fromtimestamp(m.ts).date().isoformat() for m in convo.messages if m.ts and m.from_owner)
        day = self.memory.day_to_consolidate([d for d, n in said.items() if n >= MIN_USER_MESSAGES], now)
        if day is not None:
            self._memory_running = True
            self._spawn(self._consolidate_memory(convo, day))

    async def _consolidate_memory(self, convo: Conversation, day: str) -> list:
        """Read one day's chat and keep what the user said about himself, with his words as proof."""
        try:
            todays = [m for m in convo.messages if m.ts and datetime.fromtimestamp(m.ts).date().isoformat() == day]
            user_texts = [m.text for m in todays if m.from_owner]  # what guests say is not about the owner
            if not user_texts:
                self.memory.record_consolidation(day, [], self.config.memory_learn, 0.0)
                return []
            lines = [f"{datetime.fromtimestamp(m.ts):%H:%M} {label(m)}: {m.text[:300]}"
                     for m in todays][-250:]
            users = sorted({m.speaker_name for m in todays if m.from_owner})
            raw = await self._cheap_ask(consolidation_prompt(day, lines, self.memory.items, users),
                                        '你帮一个朋友群记下群主自己说过的、值得长期记住的事。只按要求输出 JSON。', 'memory')
            if raw is None:
                log.info('%s 的聊天没整理成长期记忆：没有可用的便宜模型（不用 Claude）', day)
                return []  # not marked as read: tried again later
            found = parse_consolidation(raw, user_texts, self.memory.items)
            said_at = max(m.ts for m in todays if m.from_owner)
            added = self.memory.record_consolidation(day, found, self.config.memory_learn, said_at)
            log.info('%s 的聊天整理出 %d 条长期记忆%s：%s', day, len(added),
                     '' if self.config.memory_learn == 'auto' else '（待确认）', '；'.join(f.text for f in added) or '没有')
            if added and self.config.memory_learn == 'auto':
                self.refresh_prompts()
            return added
        except Exception as e:  # noqa: BLE001 - a failed read must not stop the loop
            log.warning('%s 的聊天没整理成长期记忆：%s', day, e)
            return []
        finally:
            self._memory_running = False

    async def consolidate_memory_now(self, day: str | None = None) -> list:
        """Admin "现在整理": read a day (default: today) right away, even if read before."""
        chat = self.proactive_chat()
        if not chat:
            raise ConfigError('还没有设置主动发言的群，不知道该整理哪个群的聊天')
        if self._memory_running:
            raise ConfigError('正在整理，等一会儿再试')
        if self.config.memory_learn not in ('review', 'auto'):
            raise ConfigError('长期记忆的学习方式是「关闭」，先改成「需要我确认」或「自动记住」')
        day = day or datetime.now().date().isoformat()
        self._memory_running = True
        return await self._consolidate_memory(self.conversation(chat), day)

    def _chronicle_writer(self) -> Participant | None:
        """The cheapest member to write the day up: an API one, then Codex or Grok, Claude last."""
        rank = {"api": 0, "codex_cli": 1, "grok_cli": 2, "claude_cli": 3}
        members = sorted(self.participants, key=lambda p: rank.get(p.cfg.kind, 4))
        return members[0] if members else None

    async def _write_chronicle(self, convo: Conversation, day: str) -> list:
        import uuid

        try:
            lines = [f"{datetime.fromtimestamp(m.ts):%H:%M} {label(m)}: {m.text[:300]}"
                     for m in convo.messages if m.ts and datetime.fromtimestamp(m.ts).date().isoformat() == day]
            writer = self._chronicle_writer()
            if writer is None or not lines:
                return []
            prompt = build_chronicle_prompt(day, lines[-200:], [m.title for m in self.chronicle.moments[-30:]],
                                            [m.text for m in self.memes.items[-30:]])
            # A one-off, tool-less run in its own sessions: it must not end up in the member's chat context.
            actor = participant_class(writer.cfg)(writer.cfg, SessionStore(self.config.data_dir / "chronicle_sessions.json"),
                                                  self.config.data_dir / "work" / "chronicle" / writer.id,
                                                  self.config.round.call_timeout)
            actor.set_system_prompt("你是一个朋友群的记事本，负责记下群里的名场面。只按要求输出 JSON。")
            key = "__chronicle_" + uuid.uuid4().hex
            try:
                raw = await actor.reply(key, prompt)
            finally:
                actor.reset(key)
            moments, memes = parse_chronicle(raw)
            added = self.chronicle.record(day, moments)
            new_memes = self.memes.add_found([(t, "") for t in memes], "群里")
            log.info("%s 的大事记：%s%s", day, "；".join(m.title for m in added) or "没有特别的",
                     ("；新梗：" + "；".join(m.text for m in new_memes)) if new_memes else "")
            self.refresh_prompts()
            return added
        except Exception as e:  # noqa: BLE001 - a failed write-up must not stop the loop
            log.warning("%s 的大事记没写成：%s", day, e)
            self.chronicle.record(day, [])  # don't retry every half minute; 管理页面可以重新整理
            return []
        finally:
            self._chronicle_running = False

    async def write_chronicle_now(self, day: str | None = None) -> list:
        """Admin "现在整理": write up a day (default: today) right away, even if done before."""
        chat = self.proactive_chat()
        if not chat:
            raise ConfigError("还不知道是哪个群，先在群里聊几句")
        if self._chronicle_running:
            raise ConfigError("正在整理，稍等一下")
        day = day or datetime.now().date().isoformat()
        self.chronicle.forget_day(day)
        self._chronicle_running = True
        return await self._write_chronicle(self.conversation(chat), day)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _browse(self, p: Participant) -> list:
        """One private browsing session; what it finds goes into the member's journal."""
        pc = p.cfg
        can_search = pc.web_search and supports_web_search(pc)
        if not (can_search or p.tools):
            self.browsing.record(pc, None)
            return []
        names = [m.name for m in self.config.enabled_participants]
        tools = [(s.name, pc.tool_notes.get(s.id, "")) for s in p.tools]
        # Left alone, members stick to plain web search; every other session is about one of their tools.
        focus = random.choice(tools) if tools and (not can_search or random.random() < 0.5) else None
        prompt = build_browse_prompt(
            pc.browse_topics or pc.proactive_topics, pc.kind == "grok_cli", self.journal.known_urls(pc.id), names,
            tools, can_search, focus,
        )
        log.info("%s 开始浏览……", p.name)
        memes_before = len(self.memes.items)
        try:
            added = await browse_once(p, self.journal, prompt, self.memes)
        except ParticipantError as e:
            log.warning("%s 浏览失败：%s", p.id, e)
            added = []
        self.browsing.record(pc, "found" if added else None)
        if added:
            log.info("%s 记下了 %d 条见闻：%s", p.name, len(added), "；".join(e.title for e in added))
        if added or len(self.memes.items) != memes_before:
            self.refresh_prompts()
        return added

    async def try_browse(self, pid: str) -> list[dict]:
        """Admin "browse now"."""
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None:
            raise ConfigError("这个成员没有启用，或者启动失败了")
        if self._life_enabled(pid):
            raise ConfigError("这位成员由日常流程安排活动，请到「日常」页让它自己决定")
        if not (p.cfg.web_search and supports_web_search(p.cfg)) and not p.tools:
            raise ConfigError("要先打开它的「联网搜索」，或者在「工具」里给它分配工具，才能去浏览")
        if pid in self._browsing_now:
            raise ConfigError("它正在浏览，稍等一下")
        self._browsing_now.add(pid)
        try:
            return [asdict(e) for e in await self._browse(p)]
        finally:
            self._browsing_now.discard(pid)

    async def _proactive_attempt(self, p: Participant, convo: Conversation) -> str | None:
        pc = p.cfg
        can_search = pc.web_search and supports_web_search(pc)
        entry = self.journal.best_unshared(pc.id)

        def make_prompt(unseen):
            if entry is not None:  # share something it already saw: no search needed now
                return build_share_prompt(unseen, entry)
            return build_proactive_prompt(
                unseen, pc.proactive_topics, can_search, pc.kind == "grok_cli",
                self.proactive.recent_urls(pc) + self.journal.known_urls(pc.id),
            )

        sources = [{"url": entry.url, "title": entry.title, "excerpt": entry.summary,
                    "read_level": entry.read_level or "群友的浏览摘记，未记录具体阅读范围"}] if entry else []
        future = convo.start_proactive(p, make_prompt, pc.proactive_replies, sources=sources,
                                      discussion=bool(entry))
        if future is None:
            self.proactive.postpone(pc)
            return None
        log.info("[%s] %s 主动发言中……", convo.key, p.name)
        try:
            posted = await asyncio.wait_for(future, self.config.round.call_timeout + 60)
        except Exception as e:
            log.warning("%s 主动发言没有完成：%s", p.id, e)
            posted = None
        self.proactive.record(pc, posted)
        if posted is not None and entry is not None:
            self.journal.mark_shared(pc, entry.id)
            self.life.record_share(pc.id, posted, sources)
            self.refresh_prompts()
        return posted

    async def try_proactive(self, pid: str) -> dict:
        """Admin "try now": ignores hours, daily cap and quiet time, but not a running round."""
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None:
            raise ConfigError("这个成员没有启用，或者启动失败了")
        if not self.telegram.connected:
            raise ConfigError("Telegram 还没连上，发不出去")
        chat = self.proactive_chat()
        if chat is None:
            raise ConfigError("还不知道要发到哪个群：先在群里聊一句，或在「群聊规则」里填主动发言的群 ID")
        convo = self.conversation(chat)
        if convo.busy:
            raise ConfigError("群里正在聊，等这一轮结束再试")
        posted = await self._proactive_attempt(p, convo)
        return {"chat": chat, "posted": posted}

    async def set_max_turns(self, n: int) -> None:
        settings = {**self.settings, "round": {**self.settings["round"], "max_turns_per_model": n}}
        await self.apply(settings)

    def status(self) -> dict:
        members = []
        running = {p.id: p for p in self.participants}
        for pc in self.config.participants:
            if not pc.enabled:
                state, message = "disabled", ""
            elif pc.id in self.participant_errors:
                state, message = "error", self.participant_errors[pc.id]
            elif pc.id in self.participant_warnings:
                state, message = "warning", self.participant_warnings[pc.id]
            else:
                state, message = "ok", ""
            members.append({
                "id": pc.id,
                "name": pc.name,
                "kind": pc.kind,
                "state": state,
                "message": message,
                "bot_username": self.telegram.bot_usernames.get(pc.id, ""),
                "routine": self._routine_label(running[pc.id]) if pc.id in running else "",
            })
        convos = [
            {
                "key": convo.key,
                "busy": convo.busy,
                "message_count": len(convo.messages),
                "recent": [{"speaker": m.speaker_name, "text": m.text} for m in convo.messages[-12:]],
                "urges": convo.decision_report(),
            }
            for convo in self.conversations.values()
            if convo.key != TEST_CHAT
        ]
        return {
            "telegram": self.telegram.status(),
            "members": members,
            "conversations": convos,
            "pending_memory": self.memory.pending_count(),
            "proactive": {
                "chat": self.proactive_chat(),
                "members": {
                    p.id: self.proactive.describe(p.cfg) for p in self.participants if p.cfg.proactive
                },
            },
            "browse": {
                p.id: {**self.browsing.describe(p.cfg), "running": p.id in self._browsing_now}
                for p in self.participants if p.cfg.browse
            },
            "notices": self.notices,
            "events": self.events.recent(),
            "logs": list(self.log_buffer.lines)[-150:] if self.log_buffer else [],
        }

    # ---- Telegram ----

    async def _wait_change(self, timeout: float | None = None) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)
        self._changed.clear()

    async def shutdown(self) -> None:
        await self._cancel_life()
        await self.stop_game()
        for convo in self.conversations.values():
            await convo.stop()
        await self.telegram.shutdown()


__all__ = ["LogBuffer", "ParticipantError", "Runtime"]
