"""The running app: members, rooms and Telegram bots, rebuilt live when settings change."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter, deque
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from .config import ConfigError, telegram_problems
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
    build_system_prompt,
    build_turn_prompt,
    abilities_section,
    ask_search_instructions,
    now_line,
    render_role,
    tools_section,
    search_instructions,
)
from .room import ChatRoom
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
from .life import LifeEngine, LifeStore
from .chronicle import MIN_MESSAGES as CHRONICLE_MIN_MESSAGES, Chronicle
from .chronicle import build_prompt as build_chronicle_prompt, parse_reply as parse_chronicle
from .feedback import FeedbackStore, plain_text
from .followups import FollowUpStore, build_followup_prompt
from .memes import MemeLibrary
from .memory import MemoryStore
from .proactive import ProactiveScheduler, seconds_quiet
from .routine import ASLEEP, BUSY, GOODNIGHT_CHANCE, MORNING_CHANCE, Routines, busy_doing, goodnight_prompt, wake_prompt
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
from .telegram_bot import TelegramSink, build_router
from .transcript import USER, Message

log = logging.getLogger(__name__)

TEST_CHAT = "__admin_test__"
BOT_COMMANDS = [
    BotCommand(command="stop", description="中止当前这一轮"),
    BotCommand(command="reset", description="清空所有模型的记忆"),
    BotCommand(command="turns", description="设置每轮发言上限"),
    BotCommand(command="status", description="查看状态和群 ID"),
    BotCommand(command="help", description="使用说明"),
]


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

        self.participants: list[Participant] = []
        self.participant_errors: dict[str, str] = {}  # member could not be started
        self.participant_warnings: dict[str, str] = {}  # started, but something is off
        self.rooms: dict[str, ChatRoom] = {}

        self.bots: dict[str, Bot] = {}  # participant id -> its bot
        self.bot_usernames: dict[str, str] = {}
        self.bot_user_ids: dict[int, str] = {}  # Telegram user id of a model bot -> participant id
        self.bot_profiles: dict[str, tuple[str | None, str | None]] = {}  # shown name and intro
        self.controller: Bot | None = None
        self.controller_username = ""
        self.privacy_warning = False
        self.reaction_warning = False  # not a group admin: Telegram does not send it reactions
        self.telegram_state = "未启动"

        self.stickers = StickerLibrary(store.data_dir)
        self.kaomoji = KaomojiLibrary(store.data_dir)
        self.seen = SeenCache()
        self.memory = MemoryStore(store.data_dir)
        self.memes = MemeLibrary(store.data_dir)
        self.followups = FollowUpStore(store.data_dir)
        self.chronicle = Chronicle(store.data_dir)
        self._chronicle_running = False
        self.routines = Routines()
        self.feedback = FeedbackStore(store.data_dir)
        self._routine_last: dict[str, str] = {}  # member id -> asleep/busy/online at the last check
        self._routine_events: list[tuple[str, str, float]] = []  # (member id, "wake"/"sleep", when)
        self.proactive = ProactiveScheduler(store.data_dir / "proactive.json")
        self.browsing = ProactiveScheduler(store.data_dir / "browse_plan.json", prefix="browse")
        self.journal = JournalStore(store.data_dir)
        self.life = LifeStore(store.data_dir)
        self.life_schedule = ProactiveScheduler(store.data_dir / "life_plan.json", prefix="browse")
        self.life_posts = ProactiveScheduler(store.data_dir / "life_shares.json")
        self.life_sessions = SessionStore(store.data_dir / "life_sessions.json")
        self._life_task: asyncio.Task | None = None
        self._life_member = ""
        self.drawings = DrawLog(store.data_dir)
        self._browsing_now: set[str] = set()
        self._background: set[asyncio.Task] = set()
        self.sink = TelegramSink(self)
        self._changed = asyncio.Event()
        self._lock = asyncio.Lock()
        self._rebuild_participants()

    # ---- members and rooms ----

    @property
    def sticker_limit(self) -> int:
        return self.config.max_stickers if self.config.stickers_enabled else 0

    def sticker_prompt(self) -> str:
        return self.stickers.prompt_section(self.sticker_limit)

    def system_prompt_for(self, pc, config=None) -> str:
        """A member's full system prompt: role card, group rules, setting, policy, extras.

        ``config`` lets the admin page preview unsaved settings.
        """
        config = config or self.config
        others = [o.name for o in config.enabled_participants if o.id != pc.id]
        return build_system_prompt(
            pc.name, others, render_role(pc), config.system_template,
            self._extra_prompt(pc, config), config.scene, config.no_coding,
        )

    def _extra_prompt(self, pc, config) -> str:
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
            self.memes.prompt_section(),
            self.chronicle.prompt_section(),
            self.followups.prompt_section(),
            self.journal.prompt_section(pc.id),
            self.life.prompt_section(pc.id, {m.id: m.name for m in config.participants}),
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
        self.refresh_prompts()
        for room in self.rooms.values():
            room.update(built, self.config.round, self.sticker_limit, self.config.no_coding, self.config.reactions_enabled)

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

    def on_reaction(self, chat: str, tg_id: int, user_name: str, added: list[str], removed: list[str]) -> None:
        """The user reacted to a message in the group (or took a reaction back)."""
        room = self.room(chat)
        target = room.message_for(self.sink.parts.get((chat, tg_id), tg_id))
        if target is None or target.from_user:
            return
        text = plain_text(target.text)
        changed = False
        for emoji in added:
            item = self.feedback.react(target.speaker_id, text, emoji)
            if item is not None:
                log.info("[%s] %s 给 %s 的「%s」点了 %s", chat, user_name, target.speaker_name, text[:30], emoji)
                changed = changed or item.kind == "disliked"
        if removed and not added:
            changed = self.feedback.unreact(target.speaker_id, text)
        if added:
            room.note_reaction(user_name, target, added[-1])
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
        return participant_class(pc)(pc, self.sessions, self.config.data_dir / "work" / pc.id, self.config.round.call_timeout)

    def chat_log_path(self, key: str) -> Path | None:
        if key.startswith("__"):  # admin tests and other internal chats are not recorded
            return None
        return self.config.data_dir / "chats" / f"{key}.jsonl"

    def chat_keys(self) -> list[str]:
        """Every chat with a transcript, a running room or saved sessions."""
        keys = set(self.rooms) | set(self.sessions.chats())
        chats_dir = self.config.data_dir / "chats"
        if chats_dir.exists():
            keys |= {p.stem for p in chats_dir.glob("*.jsonl")}
        return sorted(k for k in keys if not k.startswith("__"))

    def room(self, chat_id: int | str) -> ChatRoom:
        key = str(chat_id)
        if key not in self.rooms:
            self.rooms[key] = ChatRoom(
                key, self.participants, self.sink, self.config.round, self.sessions,
                stickers=self.stickers, sticker_limit=self.sticker_limit,
                log_path=self.chat_log_path(key), on_memory=self._on_memory, on_meme=self._on_meme, on_followup=self._on_followup, block_code=self.config.no_coding,
                on_draw=self._on_draw, reactions=self.config.reactions_enabled, routines=self.routines,
            )
        return self.rooms[key]

    async def apply(self, settings: dict | None = None, secrets: dict | None = None) -> None:
        """Save new settings and/or secrets, then rebuild everything that depends on them."""
        async with self._lock:
            if settings is not None:
                settings = normalize_settings(settings)
                ids = {p["id"] for p in settings["participants"]}
                tool_ids = {t["id"] for t in settings["tools"]}
                secrets = prune_secrets(secrets if secrets is not None else self.secrets, ids, tool_ids)
            await self._cancel_life()
            self.store.save(settings, secrets)
            self.settings, self.secrets = self.store.load()
            self.config = build_config(self.settings, self.secrets, self.store.data_dir)
            for room in self.rooms.values():
                await room.stop()
            self.memory.drop_scope({p.id for p in self.config.participants})
            self._rebuild_participants()
            await self._sync_bots()
            self._changed.set()
        log.info("配置已更新")

    async def _sync_bots(self) -> None:
        wanted = {p.id: p.cfg.bot_token for p in self.participants if p.cfg.bot_token}
        for pid in list(self.bots):
            if self.bots[pid].token != wanted.get(pid):
                bot = self.bots.pop(pid)
                self.bot_usernames.pop(pid, None)
                self.bot_profiles.pop(pid, None)
                await bot.session.close()
        warnings = {}
        for pid, token in wanted.items():
            if pid not in self.bots:
                try:
                    self.bots[pid] = Bot(token)
                except Exception as e:  # malformed token
                    warnings[pid] = f"bot token 格式不对：{e}"

        async def fetch(pid: str, bot: Bot) -> None:
            try:
                me = await asyncio.wait_for(bot.get_me(), 15)
            except Exception as e:
                warnings[pid] = f"bot 连接失败：{e}"
                return
            self.bot_usernames[pid] = me.username or ""
            self.bot_user_ids[me.id] = pid
            with suppress(Exception):
                intro = await asyncio.wait_for(bot.get_my_short_description(), 15)
                self.bot_profiles[pid] = (me.first_name, intro.short_description)

        await asyncio.gather(*(fetch(pid, b) for pid, b in self.bots.items() if pid not in self.bot_usernames))
        await asyncio.gather(*(self._sync_profile(p) for p in self.participants if p.id in self.bots))
        for p in self.participants:
            if p.id not in wanted:
                warnings.setdefault(p.id, "没有设置 bot token，它的发言发不到群里")
            username = self.bot_usernames.get(p.id)
            if username and username not in p.aliases:
                p.aliases.append(username)
        self.participant_warnings = warnings

    async def _sync_profile(self, p: Participant) -> None:
        """Show the role's name and one-line intro on its Telegram bot."""
        bot = self.bots[p.id]
        name, intro = self.bot_profiles.get(p.id, (None, None))
        try:
            if name != p.name:
                await asyncio.wait_for(bot.set_my_name(name=p.name), 15)
                log.info("%s 的 bot 显示名已改为「%s」", p.id, p.name)
            if intro != p.cfg.tagline:
                await asyncio.wait_for(bot.set_my_short_description(short_description=p.cfg.tagline), 15)
        except Exception as e:  # Telegram rate-limits name changes; try again next time
            log.warning("%s 的 bot 资料没能更新：%s", p.id, e)
            return
        self.bot_profiles[p.id] = (p.name, p.cfg.tagline)

    async def start(self) -> None:
        await self._sync_bots()

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
            room = self.room(key)
            last = room.messages[-1] if room.messages else None
            out.append({
                "key": key,
                "busy": room.busy,
                "message_count": len(room.messages),
                "last_ts": last.ts if last else 0,
                "last": f"{last.speaker_name}: {last.text}" if last else "",
            })
        return sorted(out, key=lambda c: c["last_ts"], reverse=True)

    def chat_detail(self, key: str) -> dict:
        room = self.room(key)
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
            "busy": room.busy,
            "messages": [m.to_json() for m in room.messages],
            "members": members,
        }

    async def reset_member_context(self, key: str, pid: str) -> None:
        room = self.room(key)
        await room.stop()
        room.reset_member(self._member(pid))

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
        bot = self.controller or next(iter(self.bots.values()), None)
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
        room = self.rooms.get(chat_key)
        recent = [f"{m.speaker_name}: {m.text}" for m in room.messages[-6:]] if room else []
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
            try:
                await self._proactive_tick()
            except Exception:
                log.exception("主动发言检查出错")

    async def _proactive_tick(self) -> None:
        if self.journal.forget_expired():
            self.refresh_prompts()
        self._chronicle_tick()
        if self.controller is not None:
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

        if self.controller is None:  # Telegram not running: nowhere to post
            return
        for p in list(self.participants):
            if self._life_enabled(p.id) or self._asleep(p):
                continue
            if not self.proactive.due(p.cfg):
                continue
            chat = self.proactive_chat()
            room = self.room(chat) if chat else None
            quiet = seconds_quiet(room.last_activity) if room else 0
            if room is None or room.busy or quiet < p.cfg.proactive_idle_minutes * 60:
                self.proactive.postpone(p.cfg)  # not now; look again in a few minutes
                continue
            await self._proactive_attempt(p, room)

    # ---- members' continuing daily life ----

    def _life_enabled(self, pid: str) -> bool:
        return self.config.life.enabled and pid in self.config.life.members

    def _life_days(self, pid: str) -> int:
        return self.config.life.member_days.get(pid) or self.config.life.per_day

    def _life_left(self, pid: str) -> int:
        """Activities left today; being messaged by a friend uses one too."""
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None or not self._life_enabled(pid):
            return 0
        return self._life_days(pid) - self.life_schedule.plan(self._life_policy(p)).posts

    def _life_charge(self, pid: str) -> None:
        p = next((x for x in self.participants if x.id == pid), None)
        if p is not None:
            self.life_schedule.plan(self._life_policy(p)).posts += 1
            self.life_schedule.save()

    def _life_found(self, pid: str, item, result: dict) -> None:
        """Progress from a daily-life look-around also goes into the member's 见闻."""
        sources = result.get("sources") or []
        first = next((s for s in sources if isinstance(s, dict) and s.get("url")), None)
        if first is None:
            return
        entry = {
            "title": result.get("title") or first.get("title") or item.title,
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
        return (not any(room.busy for room in self.rooms.values())
                and not self._browsing_now.intersection(self.config.life.members))

    def life_status(self) -> dict:
        names = {p.id: p.name for p in self.config.participants}
        plans = {}
        for p in self.participants:
            if p.id in self.config.life.members:
                policy = self._life_policy(p)
                plans[p.id] = {**self.life_schedule.describe(policy),
                               "shares_today": self.life_posts.plan(policy).posts,
                               "limit": self._life_days(p.id)}
        return {**self.life.public(), "names": names, "settings": asdict(self.config.life),
                "running": self._life_member, "plans": plans}

    def start_life(self, pid: str) -> dict:
        if not self._life_enabled(pid):
            raise ConfigError("请先在日常页开启日常，并把这位成员选进去")
        if self._life_task is not None or not self._life_idle():
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

    def _life_context(self, pid: str) -> str:
        p = self._member(pid)
        chat = self.proactive_chat()
        room = self.room(chat) if chat else None
        recent = "\n".join(f"{m.speaker_name}：{m.text[:600]}" for m in room.messages[-10:]) if room else "暂无新消息"
        interests = p.cfg.browse_topics or p.cfg.proactive_topics or "按自己的兴趣选择"
        return (f"你的长期兴趣：{interests}\n最近的公开群聊（不是给你的新任务）：\n{recent}\n"
                f"现在是否适合发到群里：{'可以自行决定' if self._life_share_ready(p, room) else '暂不合适，可以先留着，下次再决定'}。")

    async def _life_ask(self, pid: str, prompt: str) -> str:
        import uuid
        p = self._member(pid)
        # Separate work and sessions prevent background JSON/private dialogue from
        # entering the actor's public CLI/API conversation or racing its prompt file.
        actor = participant_class(p.cfg)(p.cfg, self.life_sessions,
                                        self.config.data_dir / "work" / "life" / pid,
                                        self.config.round.call_timeout)
        actor.tools = tools_for(p.cfg, self.config.tools)
        actor.set_system_prompt(self.system_prompt_for(p.cfg))
        key = "__life_" + uuid.uuid4().hex
        try:
            return await actor.reply(key, prompt)
        finally:
            self.life_sessions.clear_one(key, pid)
            actor.reset(key)

    async def _run_life(self, pid: str) -> None:
        try:
            p = self._member(pid)
            names = {m.id: m.name for m in self.config.participants}
            engine = LifeEngine(self.life, self._life_ask, self._life_share, self._life_context,
                                names, self.refresh_prompts, charge=self._life_charge, found=self._life_found)
            can_explore = bool(p.tools) or (p.cfg.web_search and supports_web_search(p.cfg))
            # Friends it can message: the others in daily life with activities left today.
            awake = {m.id for m in self.participants if not self._asleep(m)}
            peers = [m for m in self.config.life.members if m != pid and m in awake and self._life_left(m) > 0]
            action = await engine.step(pid, peers, can_explore)
            log.info("%s 完成一次日常活动：%s", p.name, action)
        except asyncio.CancelledError:
            self.life.event(pid, "interrupted", "本次活动已中断，已经发生的进展和私聊仍然保留")
            raise
        except Exception as error:
            log.warning("%s 日常活动失败：%s", pid, error)
            self.life.event(pid, "error", str(error))
        finally:
            self._life_task = None
            self._life_member = ""
            self.refresh_prompts()

    def _life_share_ready(self, p: Participant, room: ChatRoom | None) -> bool:
        if not self._life_enabled(p.id) or self.controller is None or room is None or room.busy:
            return False
        policy = self._life_policy(p)
        plan = self.life_posts.plan(policy)
        return (self.life_posts.in_hours(policy) and plan.posts < policy.proactive_per_day
                and plan.attempts < policy.proactive_per_day * 2
                and seconds_quiet(room.last_activity) >= policy.proactive_idle_minutes * 60)

    async def _life_share(self, pid: str, message: str, sources=()) -> str | None:
        p = self._member(pid)
        chat = self.proactive_chat()
        room = self.room(chat) if chat else None
        if not self._life_share_ready(p, room):
            return None
        if sources and room.shared_recently(sources):
            return None
        policy = self._life_policy(p)
        # Sharing is selected by the actor; reserve a bounded attempt before posting.
        self.life_posts.record(policy, None)
        future = room.start_proactive(p, lambda unseen: "", max(1, p.cfg.proactive_replies), prepared_text=message, sources=sources)
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

    def _on_draw(self, room: ChatRoom, drawer: Participant, request: tuple[str, str], requester) -> None:
        pc = drawer.cfg
        if not (pc.can_draw and supports_drawing(pc)):
            return  # the tag is ignored for members that cannot draw
        if drawer.id in self.drawings.busy:
            self._spawn(room.deliver_drawing(drawer, "手上这张还没画完，排个队。", None, requester))
            return
        if self.drawings.remaining(pc) <= 0:
            self._spawn(room.deliver_drawing(drawer, "今天画不动了，明天再说。", None, requester))
            return
        self.drawings.busy.add(drawer.id)
        self._spawn(self._draw(room, drawer, *request, requester))

    async def _draw(self, room: ChatRoom, drawer: Participant, label: str, description: str, requester) -> None:
        asker = requester.speaker_name if requester is not None else ""
        await room.sink.activity(room.key, drawer, f"正在画表情包：{label}")
        log.info("[%s] %s 开始画「%s」：%s", room.key, drawer.name, label, description)
        ok = False
        try:
            path = await drawer.draw(build_draw_prompt(label, description, self.config.enabled_participants))
            label = self.stickers.unique_label(label)
            note = f"{drawer.name} 给 {asker} 画的：{description}" if asker else f"{drawer.name} 画的：{description}"
            sticker = self.stickers.add(f"drawing{path.suffix}", path.read_bytes(), label, note[:300])
            ok = True
            self.refresh_prompts()  # everyone can use the new sticker now
            text = f"画好了 @{asker}，拿去用。" if asker and asker != drawer.name else "画好了。"
            log.info("[%s] %s 画好了「%s」", room.key, drawer.name, label)
            await room.sink.clear_activity(room.key, drawer)
            await room.deliver_drawing(drawer, text, sticker, requester)
        except Exception as e:
            log.warning("%s 画图失败：%s", drawer.id, e)
            await room.sink.clear_activity(room.key, drawer)
            await room.deliver_drawing(drawer, "这张翻车了，没画出来。", None, requester)
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
            room = self.room(item.chat)
            asker = self._followup_asker(item)
            if asker is None:  # everyone is asleep: when someone is up; no members at all: drop it
                self.followups.later(item) if self.participants else self.followups.finish(item, asked=False)
                continue
            future = None if room.busy else room.start_proactive(
                asker, lambda unseen, item=item: build_followup_prompt(item, unseen, now_line()), 1)
            if future is None:  # the group is busy: a bit later
                self.followups.later(item)
                continue
            log.info("[%s] %s 想起来要问：%s", room.key, asker.name, item.text)
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
        room = self.room(chat) if chat else None
        for p, before, after in self.routines.changes(self.participants, self._routine_last):
            if before == ASLEEP:
                self._routine_events.append((p.id, "wake", time.time()))
            elif after == ASLEEP and room is not None and seconds_quiet(room.last_activity) < 1800:
                if random.random() < GOODNIGHT_CHANCE:
                    self._routine_events.append((p.id, "sleep", time.time()))
        self._routine_events = [e for e in self._routine_events if time.time() - e[2] < 7200]
        if room is None or room.busy or not self._routine_events:
            return
        pid, kind, _ = self._routine_events.pop(0)
        p = next((m for m in self.participants if m.id == pid), None)
        if p is None:
            return
        if kind == "wake":
            missed = [m for m in room._unseen(p) if m.from_user]
            # Nothing missed: only sometimes a "morning", and not from Claude (it shares the owner's quota).
            if not missed and (p.cfg.kind == "claude_cli" or random.random() >= MORNING_CHANCE or not room.messages):
                return
            make = lambda unseen: wake_prompt(unseen, now_line(), bool(missed))  # noqa: E731
        else:
            make = lambda unseen: goodnight_prompt(unseen, now_line())  # noqa: E731
        future = room.start_proactive(p, make, 1 if kind == "wake" else 0)
        if future is None:
            return
        log.info("[%s] %s %s", room.key, p.name, "睡醒了，看看群" if kind == "wake" else "要去睡了")
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
        room = self.room(chat)
        now = now or datetime.now()
        oldest = (now - timedelta(days=3)).date().isoformat()  # catch up on a few missed days at most
        days = Counter(datetime.fromtimestamp(m.ts).date().isoformat() for m in room.messages if m.ts)
        candidates = [d for d, n in days.items() if n >= CHRONICLE_MIN_MESSAGES and d >= oldest]
        day = self.chronicle.day_to_write(candidates, now)
        if day is not None:
            self._chronicle_running = True
            self._spawn(self._write_chronicle(room, day))

    def _chronicle_writer(self) -> Participant | None:
        """The cheapest member to write the day up: an API one, then Codex or Grok, Claude last."""
        rank = {"api": 0, "codex_cli": 1, "grok_cli": 2, "claude_cli": 3}
        members = sorted(self.participants, key=lambda p: rank.get(p.cfg.kind, 4))
        return members[0] if members else None

    async def _write_chronicle(self, room: ChatRoom, day: str) -> list:
        import uuid

        try:
            lines = [f"{datetime.fromtimestamp(m.ts):%H:%M} {m.speaker_name}{'（用户）' if m.from_user else ''}: {m.text[:300]}"
                     for m in room.messages if m.ts and datetime.fromtimestamp(m.ts).date().isoformat() == day]
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
        return await self._write_chronicle(self.room(chat), day)

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

    async def _proactive_attempt(self, p: Participant, room: ChatRoom) -> str | None:
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
        future = room.start_proactive(p, make_prompt, pc.proactive_replies, sources=sources)
        if future is None:
            self.proactive.postpone(pc)
            return None
        log.info("[%s] %s 主动发言中……", room.key, p.name)
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
        if self.controller is None:
            raise ConfigError("Telegram 还没连上，发不出去")
        chat = self.proactive_chat()
        if chat is None:
            raise ConfigError("还不知道要发到哪个群：先在群里聊一句，或在「群聊规则」里填主动发言的群 ID")
        room = self.room(chat)
        if room.busy:
            raise ConfigError("群里正在聊，等这一轮结束再试")
        posted = await self._proactive_attempt(p, room)
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
                "bot_username": self.bot_usernames.get(pc.id, ""),
                "routine": self._routine_label(running[pc.id]) if pc.id in running else "",
            })
        rooms = [
            {
                "key": room.key,
                "busy": room.busy,
                "message_count": len(room.messages),
                "recent": [{"speaker": m.speaker_name, "text": m.text} for m in room.messages[-12:]],
            }
            for room in self.rooms.values()
            if room.key != TEST_CHAT
        ]
        return {
            "telegram": {
                "state": self.telegram_state,
                "controller": self.controller_username,
                "privacy_warning": self.privacy_warning,
                "reaction_warning": self.reaction_warning,
            },
            "members": members,
            "rooms": rooms,
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
            "logs": list(self.log_buffer.lines)[-150:] if self.log_buffer else [],
        }

    # ---- Telegram ----

    async def _wait_change(self, timeout: float | None = None) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._changed.wait(), timeout)
        self._changed.clear()

    async def telegram_loop(self) -> None:
        """Keep the controller bot polling; restart it when its token or the setup changes."""
        while True:
            problems = telegram_problems(self.config)
            if problems:
                self.telegram_state = "未配置：" + "；".join(problems)
                await self._wait_change()
                continue

            token = self.config.telegram.controller_token
            try:
                bot = Bot(token)
            except Exception as e:
                self.telegram_state = f"总控 bot token 格式不对：{e}"
                await self._wait_change()
                continue
            try:
                me = await asyncio.wait_for(bot.get_me(), 20)
            except Exception as e:
                self.telegram_state = f"总控 bot 连接失败，30 秒后重试：{e}"
                await bot.session.close()
                await self._wait_change(timeout=30)
                continue

            self.controller, self.controller_username = bot, me.username or ""
            self.privacy_warning = not me.can_read_all_group_messages
            if self.privacy_warning:
                log.warning("总控 bot @%s 开着 privacy mode，收不到群里的普通消息", me.username)
            self.reaction_warning = False
            chat = self.proactive_chat()
            if chat:
                with suppress(Exception):
                    member = await asyncio.wait_for(bot.get_chat_member(int(chat), me.id), 20)
                    self.reaction_warning = member.status not in ("administrator", "creator")
                if self.reaction_warning:
                    log.warning("总控 bot @%s 不是群管理员，收不到你在群里点的赞和踩", me.username)
            with suppress(Exception):
                await bot.set_my_commands(BOT_COMMANDS)

            dp = Dispatcher()
            dp.include_router(build_router(self))
            polling = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, allowed_updates=["message", "message_reaction"])
            )
            self.telegram_state = "运行中"
            log.info("总控 bot @%s 开始接收消息", me.username)
            try:
                while True:
                    changed = asyncio.create_task(self._wait_change())
                    done, _ = await asyncio.wait({polling, changed}, return_when=asyncio.FIRST_COMPLETED)
                    if polling in done:
                        changed.cancel()
                        self.telegram_state = f"接收消息中断，5 秒后重连：{polling.exception()}"
                        await asyncio.sleep(5)
                        break
                    if self.config.telegram.controller_token != token or telegram_problems(self.config):
                        break
            finally:
                if not polling.done():
                    with suppress(Exception):
                        await dp.stop_polling()
                    with suppress(BaseException):
                        await polling
                self.controller = None
                await bot.session.close()

    async def shutdown(self) -> None:
        await self._cancel_life()
        for room in self.rooms.values():
            await room.stop()
        for bot in self.bots.values():
            await bot.session.close()


__all__ = ["LogBuffer", "ParticipantError", "Runtime"]
