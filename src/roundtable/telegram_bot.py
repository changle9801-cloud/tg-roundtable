"""Telegram transport: one controller bot listens, one bot per model speaks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile
from aiogram.types import Message as TgMessage

from .participants import Participant
from .prompts import looks_like_code
from .stickers import Sticker

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

TG_LIMIT = 4000  # Telegram caps messages at 4096 characters
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}

HELP = """\
直接在群里说话，各个模型会轮流接话（每个模型每轮最多 {turns} 次）。
用 @名字、直接叫名字，或回复某个模型的消息，可以让它优先回答。

/stop  中止当前这一轮
/reset  清空所有模型的记忆，重新开始
/turns N  设置每个模型每轮最多发言几次
/status  查看状态和群 ID"""


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

    async def post(self, chat_key: str, participant: Participant, text: str) -> None:
        bot = self.runtime.bots.get(participant.id)
        if bot is None:
            await self.notice(chat_key, f"{participant.name} 没有可用的 bot，发言没能发出去。")
            return
        try:
            for chunk in split_text(text):
                await bot.send_message(int(chat_key), chunk)
        except TelegramAPIError as e:
            log.warning("%s could not post: %s", participant.id, e)
            await self.notice(chat_key, f"{participant.name} 的 bot 发不出消息（{e}），请确认它已经被拉进这个群。")

    async def notice(self, chat_key: str, text: str) -> None:
        controller = self.runtime.controller
        if controller is None:
            log.warning("[%s] %s", chat_key, text)
            return
        with suppress(TelegramAPIError):
            await controller.send_message(int(chat_key), f"⚙️ {text}")

    async def post_sticker(self, chat_key: str, participant: Participant, sticker: Sticker) -> None:
        bot = self.runtime.bots.get(participant.id)
        if bot is None:
            return
        key = (participant.id, sticker.id)  # file ids are per bot
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
            return

    async def activity(self, chat_key: str, participant: Participant, text: str) -> None:
        bot = self.runtime.bots.get(participant.id)
        if bot is None:
            return
        key = (chat_key, participant.id)
        previous = self.activity_messages.get(key)
        with suppress(TelegramAPIError):
            if previous is not None:  # a second search in the same turn: update the notice
                await bot.edit_message_text(f"🔍 {text}", chat_id=int(chat_key), message_id=previous)
            else:
                msg = await bot.send_message(int(chat_key), f"🔍 {text}", disable_notification=True)
                self.activity_messages[key] = msg.message_id

    async def clear_activity(self, chat_key: str, participant: Participant) -> None:
        message_id = self.activity_messages.pop((chat_key, participant.id), None)
        bot = self.runtime.bots.get(participant.id)
        if message_id is not None and bot is not None:
            with suppress(TelegramAPIError):
                await bot.delete_message(int(chat_key), message_id)

    @asynccontextmanager
    async def typing(self, chat_key: str, participant: Participant):
        bot = self.runtime.bots.get(participant.id)

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


def describe(msg: TgMessage) -> str:
    """Text the models see for a user message; media is described, not passed on."""
    if msg.text:
        return msg.text
    caption = f" {msg.caption}" if msg.caption else ""
    if msg.sticker:
        return f"[发送了一个表情 {msg.sticker.emoji or ''}]".replace(" ]", "]")
    if msg.animation:
        return f"[发送了一个动图]{caption}"
    if msg.photo:
        return f"[发送了一张图片]{caption}"
    return ""


def build_router(runtime: Runtime) -> Router:
    """Handlers for the controller bot. Settings are read live from the runtime."""
    r = Router()
    group = F.chat.type.in_(GROUP_TYPES)

    def allowed(msg: TgMessage) -> bool:
        tg = runtime.config.telegram
        return (
            msg.from_user is not None
            and not msg.from_user.is_bot
            and msg.from_user.id in tg.allowed_user_ids
            and (not tg.allowed_chat_ids or msg.chat.id in tg.allowed_chat_ids)
        )

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
        room = runtime.room(msg.chat.id)
        names = "、".join(p.name for p in runtime.participants) or "（无）"
        state = "讨论中" if room.busy else "空闲"
        await msg.answer(
            f"群 ID：{msg.chat.id}\n成员：{names}\n每轮发言上限：{room.max_turns}\n"
            f"当前状态：{state}\n记忆中的消息数：{len(room.messages)}"
        )

    @r.message(group, Command("stop"))
    async def stop(msg: TgMessage):
        if allowed(msg):
            stopped = await runtime.room(msg.chat.id).stop()
            await msg.answer("已中止本轮。" if stopped else "当前没有进行中的对话。")

    @r.message(group, Command("reset"))
    async def reset(msg: TgMessage):
        if allowed(msg):
            await runtime.room(msg.chat.id).reset()
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

    @r.message(group)
    async def chat(msg: TgMessage):
        if not allowed(msg):
            return
        text = describe(msg)
        if not text or text.startswith("/"):
            return
        if runtime.config.no_coding and looks_like_code(text):
            # Pasted code never reaches the models; this group is for everyday chat.
            await msg.reply("⚙️ 这个群只用来日常闲聊，代码相关的就不在这儿处理啦。")
            return
        reply_to = None
        replied = msg.reply_to_message
        if replied and replied.from_user:
            reply_to = runtime.bot_user_ids.get(replied.from_user.id)
        runtime.room(msg.chat.id).add_user_message(msg.from_user.first_name, text, reply_to)

    return r
