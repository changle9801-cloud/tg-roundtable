"""The running app: members, rooms and Telegram bots, rebuilt live when settings change."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import asdict, replace
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
    render_role,
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
from .memory import MemoryStore
from .proactive import ProactiveScheduler, seconds_quiet
from .stickers import StickerLibrary, parse_set_name
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
        self.telegram_state = "未启动"

        self.stickers = StickerLibrary(store.data_dir)
        self.memory = MemoryStore(store.data_dir)
        self.proactive = ProactiveScheduler(store.data_dir / "proactive.json")
        self.browsing = ProactiveScheduler(store.data_dir / "browse_plan.json", prefix="browse")
        self.journal = JournalStore(store.data_dir)
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
        """Search, memory and sticker sections appended to a member's system prompt."""
        search = ""
        if pc.web_search and supports_web_search(pc):
            search = search_instructions(covers_x=pc.kind == "grok_cli")
        limit = config.max_stickers if config.stickers_enabled else 0
        drawing = ""
        drawers = [m for m in config.enabled_participants if m.can_draw and supports_drawing(m)]
        if pc.can_draw and supports_drawing(pc):
            looks = appearance_section(config.enabled_participants)
            drawing = drawer_instructions(self.drawings.remaining(pc)) + ("\n" + looks if looks else "")
        elif drawers and limit:
            drawing = requester_instructions(drawers[0].name)
        parts = [
            search,
            drawing,
            self.journal.prompt_section(pc.id),
            self.memory.prompt_section(pc.id, config.memory_learn),
            self.stickers.prompt_section(limit),
        ]
        return "\n\n".join(p for p in parts if p)

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
        self.participants = built
        self.participant_errors = errors
        self.refresh_prompts()
        for room in self.rooms.values():
            room.update(built, self.config.round, self.sticker_limit, self.config.no_coding)

    def refresh_prompts(self) -> None:
        """Settings, stickers or memory changed: rebuild every system prompt (next turn on)."""
        for p in self.participants:
            p.set_system_prompt(self.system_prompt_for(p.cfg))

    stickers_changed = refresh_prompts

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
                log_path=self.chat_log_path(key), on_memory=self._on_memory, block_code=self.config.no_coding,
                on_draw=self._on_draw,
            )
        return self.rooms[key]

    async def apply(self, settings: dict | None = None, secrets: dict | None = None) -> None:
        """Save new settings and/or secrets, then rebuild everything that depends on them."""
        async with self._lock:
            if settings is not None:
                settings = normalize_settings(settings)
                ids = {p["id"] for p in settings["participants"]}
                secrets = prune_secrets(secrets if secrets is not None else self.secrets, ids)
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
            self.stickers.add(f"sticker{ext}", data.read(), st.emoji or "表情", f"来自贴纸包 {sticker_set.title}")
            added += 1
        self.stickers_changed()
        log.info("导入贴纸包 %s：%d 个", name, added)
        return added

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
        for p in list(self.participants):
            if p.id not in self._browsing_now and self.browsing.due(p.cfg):
                self._browsing_now.add(p.id)
                task = asyncio.create_task(self._browse(p))
                task.add_done_callback(lambda _t, pid=p.id: self._browsing_now.discard(pid))

        if self.controller is None:  # Telegram not running: nowhere to post
            return
        for p in list(self.participants):
            if not self.proactive.due(p.cfg):
                continue
            chat = self.proactive_chat()
            room = self.room(chat) if chat else None
            quiet = seconds_quiet(room.last_activity) if room else 0
            if room is None or room.busy or quiet < p.cfg.proactive_idle_minutes * 60:
                self.proactive.postpone(p.cfg)  # not now; look again in a few minutes
                continue
            await self._proactive_attempt(p, room)

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

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _browse(self, p: Participant) -> list:
        """One private browsing session; what it finds goes into the member's journal."""
        pc = p.cfg
        if not (pc.web_search and supports_web_search(pc)):
            self.browsing.record(pc, None)
            return []
        prompt = build_browse_prompt(pc.browse_topics or pc.proactive_topics, pc.kind == "grok_cli", self.journal.known_urls(pc.id))
        log.info("%s 开始浏览……", p.name)
        try:
            added = await browse_once(p, self.journal, prompt)
        except ParticipantError as e:
            log.warning("%s 浏览失败：%s", p.id, e)
            added = []
        self.browsing.record(pc, "found" if added else None)
        if added:
            log.info("%s 记下了 %d 条见闻：%s", p.name, len(added), "；".join(e.title for e in added))
            self.refresh_prompts()
        return added

    async def try_browse(self, pid: str) -> list[dict]:
        """Admin "browse now"."""
        p = next((x for x in self.participants if x.id == pid), None)
        if p is None:
            raise ConfigError("这个成员没有启用，或者启动失败了")
        if not (p.cfg.web_search and supports_web_search(p.cfg)):
            raise ConfigError("要先打开它的「联网搜索」才能去浏览")
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

        future = room.start_proactive(p, make_prompt, pc.proactive_replies)
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
            with suppress(Exception):
                await bot.set_my_commands(BOT_COMMANDS)

            dp = Dispatcher()
            dp.include_router(build_router(self))
            polling = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, allowed_updates=["message"])
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
        for room in self.rooms.values():
            await room.stop()
        for bot in self.bots.values():
            await bot.session.close()


__all__ = ["LogBuffer", "ParticipantError", "Runtime"]
