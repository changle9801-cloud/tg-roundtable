"""Saving stickers and GIFs from the group, and recognising library stickers when they are sent."""

from __future__ import annotations

import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.types import Animation, Chat, Message, PhotoSize, Sticker as TgSticker

from roundtable.config import ConfigError
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore
from roundtable.stickers import StickerLibrary
from roundtable.telegram_bot import describe, media_of, parse_collect_args

WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 16


def tg_message(**media) -> Message:
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=-1, type="supergroup"), **media)


def tg_sticker(unique="U1", emoji="😏", animated=False, video=False) -> TgSticker:
    return TgSticker(file_id="F-" + unique, file_unique_id=unique, type="regular", width=512, height=512,
                     is_animated=animated, is_video=video, emoji=emoji)


class FakeBot:
    def __init__(self, data=WEBP, size=None):
        self.data, self.size, self.downloads = data, size, 0

    async def get_file(self, file_id):
        return SimpleNamespace(file_path=f"stickers/{file_id}.webp", file_size=self.size)

    async def download_file(self, path):
        self.downloads += 1
        return io.BytesIO(self.data)


def test_media_of_each_kind():
    assert media_of(tg_message(sticker=tg_sticker())) == ("F-U1", "U1", ".webp")
    assert media_of(tg_message(sticker=tg_sticker(animated=True)))[2] == ".tgs"
    assert media_of(tg_message(sticker=tg_sticker(video=True)))[2] == ".webm"
    gif = Animation(file_id="A", file_unique_id="AU", width=1, height=1, duration=1, mime_type="video/mp4")
    assert media_of(tg_message(animation=gif)) == ("A", "AU", ".mp4")
    photos = [PhotoSize(file_id=f"P{i}", file_unique_id=f"PU{i}", width=i, height=i) for i in (1, 2)]
    assert media_of(tg_message(photo=photos)) == ("P2", "PU2", ".jpg")
    assert media_of(tg_message(text="hi")) is None


def test_parse_collect_args():
    assert parse_collect_args("狗头 | 阴阳怪气的时候") == ("狗头", "阴阳怪气的时候")
    assert parse_collect_args(" 狗头 ") == ("狗头", "")
    assert parse_collect_args("") == ("", "")


def test_known_stickers_are_named_for_the_models(tmp_path):
    lib = StickerLibrary(tmp_path)
    lib.add("a.webp", WEBP, "狗头", "阴阳怪气的时候", tg_id="U1")
    assert describe(tg_message(sticker=tg_sticker("U1")), lib) == "[发送了表情：狗头（阴阳怪气的时候）]"
    assert describe(tg_message(sticker=tg_sticker("U2")), lib) == "[发送了一个表情 😏]"
    assert describe(tg_message(sticker=tg_sticker("U1"))) == "[发送了一个表情 😏]"  # no library given


def test_ids_are_remembered_and_cleared_on_replace(tmp_path):
    lib = StickerLibrary(tmp_path)
    s = lib.add("a.webp", WEBP, "狗头")
    lib.remember_tg(s, "SENT1")
    lib.remember_tg(s, "SENT1")
    assert StickerLibrary(tmp_path).find_tg("SENT1").label == "狗头"
    assert s.tg_ids == ["SENT1"]
    lib.replace_file(s.id, "b.webp", WEBP)
    assert lib.find_tg("SENT1") is None
    assert lib.find_tg("") is None


async def test_collect_saves_once(tmp_path):
    runtime = Runtime(SettingsStore(tmp_path))
    bot = FakeBot()
    reply = await runtime.collect_sticker(bot, ("F-U9", "U9", ".webp"), "无语", "被气笑的时候")
    assert "已收藏为「无语」" in reply
    s = runtime.stickers.find_tg("U9")
    assert (s.label, s.description, s.kind, s.path.read_bytes()) == ("无语", "被气笑的时候", "sticker", WEBP)

    again = await runtime.collect_sticker(bot, ("F-U9", "U9", ".webp"), "别的名字", "")
    assert "已经在表情库里了，叫「无语」" in again
    assert bot.downloads == 1 and len(runtime.stickers.items) == 1


async def test_collect_rejects_big_files(tmp_path):
    runtime = Runtime(SettingsStore(tmp_path))
    with pytest.raises(ConfigError, match="10 MB"):
        await runtime.collect_sticker(FakeBot(size=11 * 1024 * 1024), ("F", "U", ".mp4"), "大", "")
