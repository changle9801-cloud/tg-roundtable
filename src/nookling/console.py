"""Terminal mode: try the round table without Telegram."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from .config import Config
from .participants import Participant, SessionStore, build_participants
from .conversation import Conversation, setup_system_prompts
from .stickers import Sticker, StickerLibrary

CHAT_KEY = "console"


class ConsoleSink:
    async def post_photo(self, chat_key, participant, path, caption, reply_to=None):
        print(f'\n[{participant.name}] {caption}\n〔游戏截图：{path}〕\n', flush=True)
        return 1

    async def post(self, chat_key: str, participant: Participant, text: str, reply_to: int | None = None) -> None:
        print(f"\n[{participant.name}] {text}\n", flush=True)

    async def react(self, chat_key: str, participant: Participant, message_id: int, emoji: str) -> None:
        print(f"（{participant.name} 点了 {emoji}）", flush=True)

    async def notice(self, chat_key: str, text: str) -> None:
        print(f"\n[系统] {text}\n", flush=True)

    @asynccontextmanager
    async def typing(self, chat_key: str, participant: Participant):
        print(f"（{participant.name} 正在输入…）", flush=True)
        yield

    async def post_sticker(self, chat_key: str, participant: Participant, sticker: Sticker) -> None:
        print(f"\n[{participant.name}] 〔表情：{sticker.label}〕\n", flush=True)

    async def activity(self, chat_key: str, participant: Participant, text: str) -> None:
        print(f"（{'🎨' if '画' in text else '🔍'} {participant.name} {text}）", flush=True)

    async def clear_activity(self, chat_key: str, participant: Participant) -> None:
        pass


async def run_console(cfg: Config, user_name: str = "我") -> None:
    # Models love emoji; a GBK console would crash on them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    store = SessionStore(cfg.data_dir / "sessions.json")
    participants = build_participants(cfg, store)
    stickers = StickerLibrary(cfg.data_dir)
    limit = cfg.max_stickers if cfg.stickers_enabled else 0
    setup_system_prompts(participants, cfg.system_template, stickers.prompt_section(limit), cfg.scene, cfg.no_coding)
    convo = Conversation(CHAT_KEY, participants, ConsoleSink(), cfg.round, store, stickers=stickers, sticker_limit=limit)

    names = "、".join(p.name for p in participants)
    print(f"终端模式。成员：{names}。命令：/stop /reset /quit\n")
    while True:
        try:
            line = (await asyncio.to_thread(input, "")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == "/quit":
            break
        if line == "/stop":
            print("已中止。" if await convo.stop() else "当前没有进行中的对话。")
        elif line == "/reset":
            await convo.reset()
            print("已清空记忆。")
        else:
            convo.add_user_message(user_name, line)
    await convo.stop()
