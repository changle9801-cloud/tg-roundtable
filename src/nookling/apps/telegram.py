"""Telegram transport: one controller bot listens, one bot per model speaks."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, FSInputFile, MessageReactionUpdated, ReactionTypeEmoji, ReplyParameters
from aiogram.types import Message as TgMessage

from ..config import ConfigError, telegram_problems
from ..participants import Participant
from ..prompts import looks_like_code
from ..stickers import Sticker, StickerLibrary

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

TG_LIMIT = 4000  # Telegram caps messages at 4096 characters
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

HELP = """\
直接在群里说话，各个模型会轮流接话（每个模型每轮最多 {turns} 次）。
用 @名字、直接叫名字，或回复某个模型的消息，可以让它优先回答。

/stop  中止当前这一轮
/reset  清空所有模型的记忆，重新开始
/turns N  设置每个模型每轮最多发言几次
/status  查看状态和群 ID
/家  看看她们在家里哪个房间、在干嘛（也可以写 /home）
/收藏 名称  回复一个贴纸、动图或图片，把它存进表情库，模型以后也能发
            （也可以写 /save；名称后面加「| 什么时候用」更好）"""


MAX_BUBBLES = 4  # paragraphs become separate messages, up to this many


def bubbles(text: str) -> list[str]:
    """Split a reply at blank lines into separate chat messages, like people send them."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) > MAX_BUBBLES:
        parts = parts[: MAX_BUBBLES - 1] + ["\n\n".join(parts[MAX_BUBBLES - 1 :])]
    return parts or [text]


def typing_pause(text: str, first: bool) -> float:
    """Seconds of "typing…" before a message: longer messages take longer to type.

    The first message already waited for the model, so it gets a shorter pause.
    """
    if first:
        return min(0.6 + len(text) / 30, 3.0)
    return min(1.0 + len(text) / 12, 5.0)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class TelegramSink:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.file_ids: dict[tuple[str, str], str] = {}  # (participant, sticker) -> Telegram file id
        self.activity_messages: dict[tuple[str, str], int] = {}  # (chat, participant) -> notice id
        self.parts: dict[tuple[str, int], int] = {}  # (chat, id of any bubble of a reply) -> id of its last bubble
        self.pacing = True  # human-like typing pauses between messages

    async def post(self, chat_key: str, participant: Participant, text: str, reply_to: int | None = None) -> int | None:
        bot = self.runtime.telegram.bots.get(participant.id)
        if bot is None:
            await self.notice(chat_key, f"{participant.name} 没有可用的 bot，发言没能发出去。")
            return None
        last = None
        ids = []
        try:
            for i, bubble in enumerate(bubbles(text)):
                for chunk in split_text(bubble):
                    with suppress(TelegramAPIError):
                        await bot.send_chat_action(int(chat_key), ChatAction.TYPING)
                    await asyncio.sleep(typing_pause(chunk, first=i == 0) if self.pacing else 0)
                    # Only the first message quotes; if the quoted one is gone, send without the quote.
                    quote = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to and last is None else None
                    sent = await bot.send_message(int(chat_key), chunk, reply_parameters=quote)
                    last = sent.message_id
                    ids.append(last)
        except TelegramAPIError as e:
            log.warning("%s could not post: %s", participant.id, e)
            await self.notice(chat_key, f"{participant.name} 的 bot 发不出消息（{e}），请确认它已经被拉进这个群。")
        for mid in ids:  # a reaction on any bubble counts for the whole reply
            self.parts[(chat_key, mid)] = last
        while len(self.parts) > 2000:
            del self.parts[next(iter(self.parts))]
        return last

    async def react(self, chat_key: str, participant: Participant, message_id: int, emoji: str) -> None:
        bot = self.runtime.telegram.bots.get(participant.id)
        if bot is None:
            return
        try:
            await bot.set_message_reaction(int(chat_key), message_id, [ReactionTypeEmoji(emoji=emoji)])
        except TelegramAPIError as e:  # e.g. the group only allows some reactions
            log.warning("%s could not react %s: %s", participant.id, emoji, e)

    async def post_photo(self, chat_key, participant, path, caption, reply_to=None):
        bot = self.runtime.telegram.bots.get(participant.id)
        if bot is None:
            return None
        try:
            quote = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
            sent = await bot.send_photo(int(chat_key), FSInputFile(path), caption=caption[:900], reply_parameters=quote)
            self.parts[(chat_key, sent.message_id)] = sent.message_id
            return sent.message_id
        except TelegramAPIError as error:
            log.warning('%s could not share game photo: %s', participant.id, error)
            return None

    async def notice(self, chat_key: str, text: str) -> None:
        controller = self.runtime.telegram.controller
        if controller is None:
            log.warning("[%s] %s", chat_key, text)
            return
        with suppress(TelegramAPIError):
            await controller.send_message(int(chat_key), f"⚙️ {text}")

    async def post_sticker(self, chat_key: str, participant: Participant, sticker: Sticker) -> None:
        bot = self.runtime.telegram.bots.get(participant.id)
        if bot is None:
            return
        key = (participant.id, sticker.file)  # file ids are per bot; a replaced image has a new file name
        send = {"sticker": bot.send_sticker, "animation": bot.send_animation}.get(sticker.kind, bot.send_photo)
        for attempt in (self.file_ids.get(key), FSInputFile(sticker.path)):
            if attempt is None:
                continue
            try:
                msg = await send(int(chat_key), attempt)
            except TelegramAPIError as e:
                if isinstance(attempt, str):
                    self.file_ids.pop(key, None)  # stale id: upload the file instead
                    continue
                log.warning("%s could not send sticker %s: %s", participant.id, sticker.label, e)
                return
            media = msg.sticker or msg.animation or msg.document or (msg.photo[-1] if msg.photo else None)
            if media is not None:
                self.file_ids[key] = media.file_id
                # So it is recognised by name when someone sends it back.
                self.runtime.stickers.remember_tg(sticker, media.file_unique_id)
            return

    async def activity(self, chat_key: str, participant: Participant, text: str) -> None:
        bot = self.runtime.telegram.bots.get(participant.id)
        if bot is None:
            return
        key = (chat_key, participant.id)
        previous = self.activity_messages.get(key)
        icon = "🎨" if "画" in text else "👀" if "看图" in text else "🔍"
        shown = f"{icon} {text}"
        with suppress(TelegramAPIError):
            if previous is not None:  # a second search in the same turn: update the notice
                await bot.edit_message_text(shown, chat_id=int(chat_key), message_id=previous)
            else:
                msg = await bot.send_message(int(chat_key), shown, disable_notification=True)
                self.activity_messages[key] = msg.message_id

    async def clear_activity(self, chat_key: str, participant: Participant) -> None:
        message_id = self.activity_messages.pop((chat_key, participant.id), None)
        bot = self.runtime.telegram.bots.get(participant.id)
        if message_id is not None and bot is not None:
            with suppress(TelegramAPIError):
                await bot.delete_message(int(chat_key), message_id)

    @asynccontextmanager
    async def typing(self, chat_key: str, participant: Participant):
        bot = self.runtime.telegram.bots.get(participant.id)

        async def keep_typing():
            while bot is not None:
                with suppress(TelegramAPIError):
                    await bot.send_chat_action(int(chat_key), ChatAction.TYPING)
                await asyncio.sleep(4.5)

        task = asyncio.create_task(keep_typing())
        try:
            yield
        finally:
            task.cancel()


def media_of(msg: TgMessage) -> tuple[str, str, str] | None:
    """(file id, unique id, file extension) of the sticker, GIF or picture in a message."""
    if msg.sticker:
        st = msg.sticker
        return st.file_id, st.file_unique_id, ".tgs" if st.is_animated else ".webm" if st.is_video else ".webp"
    if msg.animation:
        a = msg.animation
        return a.file_id, a.file_unique_id, ".gif" if a.mime_type == "image/gif" else ".mp4"  # GIFs usually arrive as MP4
    if msg.photo:
        p = msg.photo[-1]
        return p.file_id, p.file_unique_id, ".jpg"
    return None


def parse_collect_args(args: str) -> tuple[str, str]:
    """``名称 | 什么时候用`` -> (name, description)."""
    parts = re.split(r"[|｜]", args or "", maxsplit=1)
    return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""


LOOK_MAX_SIDE = 1280  # a smaller copy is plenty for a description


def to_look_at(msg: TgMessage, stickers: StickerLibrary | None = None) -> tuple[tuple[str, str, str], str] | None:
    """The picture to describe for the models: a photo, or a still sticker not in the library."""
    if msg.photo:
        fitting = [p for p in msg.photo if max(p.width, p.height) <= LOOK_MAX_SIDE]
        p = fitting[-1] if fitting else msg.photo[0]
        return (p.file_id, p.file_unique_id, ".jpg"), "图片"
    st = msg.sticker
    if st and not st.is_animated and not st.is_video and (stickers is None or stickers.find_tg(st.file_unique_id) is None):
        return (st.file_id, st.file_unique_id, ".webp"), "表情"
    return None


def describe(msg: TgMessage, stickers: StickerLibrary | None = None, seen: str | None = None) -> str:
    """Text the models see for a user message; media is described, not passed on.

    A sticker, GIF or picture that is in the library is named, so the models know what was meant;
    ``seen`` is what the viewer saw in a picture or sticker that is not.
    """
    if msg.text:
        return msg.text
    caption = f" {msg.caption}" if msg.caption else ""
    media = media_of(msg)
    known = stickers.find_tg(media[1]) if media and stickers is not None else None
    if known is not None:
        hint = f"（{known.description[:30]}）" if known.description else ""
        return f"[发送了表情：{known.label}{hint}]{caption}"
    seen = f"：{seen}" if seen else ""
    if msg.sticker:
        return f"[发送了一个表情 {msg.sticker.emoji or ''}{seen}]".replace(" ]", "]").replace(" ：", "：")
    if msg.animation:
        return f"[发送了一个动图]{caption}"
    if msg.photo:
        return f"[发送了一张图片{seen}]{caption}"
    return ""


def build_router(runtime: Runtime) -> Router:
    """Handlers for the controller bot. Settings are read live from the runtime."""
    r = Router()
    group = F.chat.type.in_(GROUP_TYPES)
    # Updates are handled concurrently; while a picture is being looked at, later messages
    # from the same group wait, so the models still read them in the order they were sent.
    in_order: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def allowed(msg: TgMessage) -> bool:
        tg = runtime.config.telegram
        ok = (
            msg.from_user is not None
            and not msg.from_user.is_bot
            and msg.from_user.id in tg.allowed_user_ids
            and (not tg.allowed_chat_ids or msg.chat.id in tg.allowed_chat_ids)
        )
        if not ok:  # otherwise a message nobody answers leaves no trace at all
            who = f"{msg.from_user.first_name}（{msg.from_user.id}）" if msg.from_user else "未知发送者"
            log.info("忽略了一条消息：%s 在群 %s 发的，不在允许的用户或群里", who, msg.chat.id)
        return ok

    @r.message(F.chat.type == ChatType.PRIVATE)
    async def private(msg: TgMessage):
        if allowed(msg):
            await msg.answer("请把我和各个模型的 bot 一起拉进同一个群，在群里聊天。")

    @r.message(group, Command("help", "start"))
    async def help_(msg: TgMessage):
        if allowed(msg):
            await msg.answer(HELP.format(turns=runtime.config.round.max_turns_per_model))

    @r.message(group, Command("status"))
    async def status(msg: TgMessage):
        if not allowed(msg):
            return
        convo = runtime.conversation(msg.chat.id)
        names = "、".join(p.name for p in runtime.participants) or "（无）"
        state = "讨论中" if convo.busy else "空闲"
        await msg.answer(
            f"群 ID：{msg.chat.id}\n成员：{names}\n每轮发言上限：{convo.max_turns}\n"
            f"当前状态：{state}\n记忆中的消息数：{len(convo.messages)}"
        )

    @r.message(group, Command("home", "家"))
    async def home(msg: TgMessage):
        if allowed(msg):
            await msg.answer(runtime.home_text())

    @r.message(group, Command("stop"))
    async def stop(msg: TgMessage):
        if allowed(msg):
            stopped = await runtime.conversation(msg.chat.id).stop()
            await msg.answer("已中止本轮。" if stopped else "当前没有进行中的对话。")

    @r.message(group, Command("reset"))
    async def reset(msg: TgMessage):
        if allowed(msg):
            await runtime.conversation(msg.chat.id).reset()
            await msg.answer("已清空所有模型的记忆。")

    @r.message(group, Command("turns"))
    async def turns(msg: TgMessage, command: CommandObject):
        if not allowed(msg):
            return
        try:
            n = int((command.args or "").strip())
            if not 1 <= n <= 10:
                raise ValueError
        except ValueError:
            await msg.answer("用法：/turns N（1 到 10）")
            return
        await runtime.set_max_turns(n)
        await msg.answer(f"之后每个模型每轮最多发言 {n} 次。")

    @r.message(group, Command("save", "收藏"))
    async def collect(msg: TgMessage, command: CommandObject):
        if not allowed(msg):
            return
        target = msg.reply_to_message
        media = media_of(target) if target else None
        if media is None:
            await msg.reply("用法：回复一个贴纸、动图或图片，发 /收藏 名称（后面可以加「| 什么时候用」）")
            return
        label, description = parse_collect_args(command.args or "")
        if not label:
            label = (target.sticker.emoji if target.sticker else "") or ("动图" if target.animation else "图片")
        try:
            text = await runtime.collect_sticker(msg.bot, media, label, description)
        except (ConfigError, TelegramAPIError) as e:
            text = f"收藏失败：{e}"
        await msg.reply(text)

    @r.message(F.migrate_from_chat_id)
    async def upgraded(msg: TgMessage):
        # The group became a supergroup with a new id; Telegram says so in the new one.
        if await runtime.migrate_chat(msg.migrate_from_chat_id, msg.chat.id):
            await msg.answer("群升级成了超级群，设置和大家的记忆都搬过来了，接着聊吧。")

    @r.message(F.migrate_to_chat_id)
    async def upgrading(msg: TgMessage):
        await runtime.migrate_chat(msg.chat.id, msg.migrate_to_chat_id)

    @r.message_reaction()
    async def reacted(event: MessageReactionUpdated):
        tg = runtime.config.telegram
        user = event.user
        if user is None or user.is_bot or user.id not in tg.allowed_user_ids:
            return
        if tg.allowed_chat_ids and event.chat.id not in tg.allowed_chat_ids:
            return
        emojis = lambda items: [r.emoji for r in items or () if getattr(r, "emoji", None)]  # noqa: E731
        old, new = emojis(event.old_reaction), emojis(event.new_reaction)
        runtime.on_reaction(str(event.chat.id), event.message_id, user.first_name,
                            [e for e in new if e not in old], [e for e in old if e not in new],
                            guest=runtime.is_guest(user.id))

    @r.message(group)
    async def chat(msg: TgMessage):
        if not allowed(msg):
            return
        async with in_order[msg.chat.id]:
            seen, images = None, ()
            target = to_look_at(msg, runtime.stickers)
            if target is not None and runtime.config.vision_member != "off":
                media, what = target
                path = await runtime.keep_image(msg.bot, media)
                if path is not None:
                    images = (str(path),)
                    seen = await runtime.look_at(str(msg.chat.id), path, media[1], what, msg.caption or "")
            text = describe(msg, runtime.stickers, seen)
            if not text or text.startswith("/"):
                return
            if runtime.config.no_coding and looks_like_code(text):
                # Pasted code never reaches the models; this group is for everyday chat.
                await msg.reply("⚙️ 这个群只用来日常闲聊，代码相关的就不在这儿处理啦。")
                return
            reply_to = None
            replied = msg.reply_to_message
            if replied and replied.from_user:
                reply_to = runtime.telegram.bot_user_ids.get(replied.from_user.id)
            runtime.conversation(msg.chat.id).add_user_message(
                msg.from_user.first_name, text, reply_to, tg_id=msg.message_id, images=images,
                guest=runtime.is_guest(msg.from_user.id),
            )

    return r


# ---- the app: bots, the controller's polling loop, status ----

BOT_COMMANDS = [
    BotCommand(command="stop", description="中止当前这一轮"),
    BotCommand(command="reset", description="清空所有模型的记忆"),
    BotCommand(command="turns", description="设置每轮发言上限"),
    BotCommand(command="status", description="查看状态和群 ID"),
    BotCommand(command="home", description="看看她们在家里哪个房间、在干嘛"),
    BotCommand(command="help", description="使用说明"),
]


class TelegramApp:
    """One controller bot listens in the group; each member speaks through her own bot."""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.sink = TelegramSink(runtime)
        self.bots: dict[str, Bot] = {}  # participant id -> its bot
        self.bot_usernames: dict[str, str] = {}
        self.bot_user_ids: dict[int, str] = {}  # Telegram user id of a model bot -> participant id
        self.bot_profiles: dict[str, tuple[str | None, str | None]] = {}  # shown name and intro
        self.controller: Bot | None = None
        self.controller_username = ""
        self.privacy_warning = False
        self.reaction_warning = False  # not a group admin: Telegram does not send it reactions
        self.state = "未启动"

    @property
    def connected(self) -> bool:
        return self.controller is not None

    def status(self) -> dict:
        return {"state": self.state, "controller": self.controller_username,
                "privacy_warning": self.privacy_warning, "reaction_warning": self.reaction_warning}

    async def sync(self) -> dict[str, str]:
        wanted = {p.id: p.cfg.bot_token for p in self.runtime.participants if p.cfg.bot_token}
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
        await asyncio.gather(*(self._sync_profile(p) for p in self.runtime.participants if p.id in self.bots))
        for p in self.runtime.participants:
            if p.id not in wanted:
                warnings.setdefault(p.id, "没有设置 bot token，它的发言发不到群里")
            username = self.bot_usernames.get(p.id)
            if username and username not in p.aliases:
                p.aliases.append(username)
        return warnings

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

    async def run(self) -> None:
        """Keep the controller bot polling; restart it when its token or the setup changes."""
        while True:
            problems = telegram_problems(self.runtime.config)
            if problems:
                self.state = "未配置：" + "；".join(problems)
                await self.runtime._wait_change()
                continue

            token = self.runtime.config.telegram.controller_token
            try:
                bot = Bot(token)
            except Exception as e:
                self.state = f"总控 bot token 格式不对：{e}"
                await self.runtime._wait_change()
                continue
            try:
                me = await asyncio.wait_for(bot.get_me(), 20)
            except Exception as e:
                self.state = f"总控 bot 连接失败，30 秒后重试：{e}"
                await bot.session.close()
                await self.runtime._wait_change(timeout=30)
                continue

            self.controller, self.controller_username = bot, me.username or ""
            self.privacy_warning = not me.can_read_all_group_messages
            if self.privacy_warning:
                log.warning("总控 bot @%s 开着 privacy mode，收不到群里的普通消息", me.username)
            self.reaction_warning = False
            chat = self.runtime.proactive_chat()
            if chat:
                with suppress(Exception):
                    member = await asyncio.wait_for(bot.get_chat_member(int(chat), me.id), 20)
                    self.reaction_warning = member.status not in ("administrator", "creator")
                if self.reaction_warning:
                    log.warning("总控 bot @%s 不是群管理员，收不到你在群里点的赞和踩", me.username)
            with suppress(Exception):
                await bot.set_my_commands(BOT_COMMANDS)

            dp = Dispatcher()
            dp.include_router(build_router(self.runtime))
            polling = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, allowed_updates=["message", "message_reaction"])
            )
            self.state = "运行中"
            log.info("总控 bot @%s 开始接收消息", me.username)
            try:
                while True:
                    changed = asyncio.create_task(self.runtime._wait_change())
                    done, _ = await asyncio.wait({polling, changed}, return_when=asyncio.FIRST_COMPLETED)
                    if polling in done:
                        changed.cancel()
                        self.state = f"接收消息中断，5 秒后重连：{polling.exception()}"
                        await asyncio.sleep(5)
                        break
                    if self.runtime.config.telegram.controller_token != token or telegram_problems(self.runtime.config):
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
        for bot in self.bots.values():
            await bot.session.close()
