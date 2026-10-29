"""Codex looks at pictures sent in the group so every member can talk about them."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from aiogram.types import Chat, Message, PhotoSize, Sticker as TgSticker

from roundtable.config import ParticipantConfig
from roundtable.participants import SessionStore
from roundtable.participants import codex_cli
from roundtable.participants.codex_cli import DISABLED_FEATURES, CodexCLI
from roundtable.prompts import FRIEND_RULES
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore, normalize_settings
from roundtable.stickers import StickerLibrary
from roundtable.telegram_bot import describe, to_look_at
from roundtable.vision import SeenCache

JPG = b"\xff\xd8\xff" + b"0" * 16


def tg_message(**kw) -> Message:
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=-1, type="supergroup"), **kw)


def photo_sizes(*sides):
    return [PhotoSize(file_id=f"P{s}", file_unique_id=f"PU{s}", width=s, height=s * 3 // 4) for s in sides]


def tg_sticker(unique="S1", animated=False):
    return TgSticker(file_id="F" + unique, file_unique_id=unique, type="regular", width=512, height=512,
                     is_animated=animated, is_video=False, emoji="🙄")


def test_what_gets_looked_at(tmp_path):
    assert to_look_at(tg_message(photo=photo_sizes(90, 320, 800, 2560))) == (("P800", "PU800", ".jpg"), "图片")
    assert to_look_at(tg_message(photo=photo_sizes(1600, 2560)))[0][0] == "P1600"  # nothing small: the smallest
    assert to_look_at(tg_message(sticker=tg_sticker())) == (("FS1", "S1", ".webp"), "表情")
    assert to_look_at(tg_message(sticker=tg_sticker(animated=True))) is None
    lib = StickerLibrary(tmp_path)
    lib.add("a.webp", b"RIFF" + b"0" * 8, "白眼", tg_id="S1")
    assert to_look_at(tg_message(sticker=tg_sticker()), lib) is None  # already known by name
    assert to_look_at(tg_message(text="hi")) is None


def test_description_reaches_the_text():
    seen = "一只橘猫把头卡在纸巾盒里"
    assert describe(tg_message(photo=photo_sizes(800), caption="笑死"), seen=seen) == f"[发送了一张图片：{seen}] 笑死"
    assert describe(tg_message(photo=photo_sizes(800))) == "[发送了一张图片]"
    assert describe(tg_message(sticker=tg_sticker()), seen="翻白眼的猫") == "[发送了一个表情 🙄：翻白眼的猫]"


class Viewer:
    def __init__(self, fail=False):
        self.id, self.name, self.fail, self.calls = "codex", "Codex", fail, []
        self.cfg = ParticipantConfig(id="codex", name="Codex", kind="codex_cli")

    async def describe_image(self, path, prompt):
        self.calls.append((path.read_bytes(), prompt))
        if self.fail:
            raise RuntimeError("boom")
        return "一只橘猫把头卡在纸巾盒里"


class Sink:
    def __init__(self):
        self.events = []

    async def activity(self, chat_key, p, text):
        self.events.append(("show", p.id, text))

    async def clear_activity(self, chat_key, p):
        self.events.append(("clear", p.id))


class Bot:
    async def get_file(self, file_id):
        return SimpleNamespace(file_path=f"photos/{file_id}.jpg")

    async def download_file(self, path):
        return io.BytesIO(JPG)


def runtime_with(tmp_path, viewer):
    rt = Runtime(SettingsStore(tmp_path))
    rt.participants = [viewer]
    rt.sink = Sink()
    rt.config.vision_member = "auto"  # the describe-for-everyone mode
    return rt


async def test_pictures_are_kept_for_a_while(tmp_path):
    import os
    import time

    rt = Runtime(SettingsStore(tmp_path))
    path = await rt.keep_image(Bot(), ("P800", "PU800", ".jpg"))
    assert path == tmp_path / "images" / "PU800.jpg" and path.read_bytes() == JPG
    stale = tmp_path / "images" / "old.jpg"
    stale.write_bytes(JPG)
    os.utime(stale, (time.time() - 3 * 86400,) * 2)
    await rt.keep_image(Bot(), ("P9", "PU9", ".jpg"))
    assert not stale.exists() and path.exists()


async def test_look_at_describes_once_with_context(tmp_path):
    viewer = Viewer()
    rt = runtime_with(tmp_path, viewer)
    rt.config = replace(rt.config, vision_member="auto", participants=[
        ParticipantConfig(id="claude", name="Claude", kind="claude_cli", appearance="橘色长发的学姐"),
        ParticipantConfig(id="deepseek", name="DeepSeek", kind="api", appearance="蓝发女仆鲸鱼娘"),
    ])
    image = tmp_path / "a.jpg"
    image.write_bytes(JPG)
    text = await rt.look_at("-1", image, "PU800", "图片", "笑死")
    assert text == "一只橘猫把头卡在纸巾盒里"
    prompt = viewer.calls[0][1]
    assert "还说了：「笑死」" in prompt and "- Claude：橘色长发的学姐" in prompt and "直接说是谁" in prompt
    assert rt.sink.events == [("show", "codex", "正在看图"), ("clear", "codex")]
    assert await rt.look_at("-1", image, "PU800", "图片", "") == text
    assert len(viewer.calls) == 1  # cached


async def test_look_at_failure_or_other_modes(tmp_path):
    image = tmp_path / "a.jpg"
    image.write_bytes(JPG)
    rt = runtime_with(tmp_path, Viewer(fail=True))
    assert await rt.look_at("-1", image, "PU1", "图片", "") is None
    assert rt.sink.events[-1] == ("clear", "codex")

    rt = runtime_with(tmp_path, Viewer())
    for mode in ("off", "help", "someone-else"):
        rt.config.vision_member = mode
        assert rt.viewer() is None
    assert await rt.look_at("-1", image, "PU2", "图片", "") is None
    rt.config.vision_member = "codex"
    assert rt.viewer() is not None


def test_vision_setting_is_checked():
    s = normalize_settings({"participants": [{"id": "codex", "name": "Codex", "kind": "codex_cli"}], "vision": {"member": "codex"}})
    assert s["vision"] == {"member": "codex"}
    assert normalize_settings({"vision": {"member": "gone"}})["vision"] == {"member": "help"}
    assert normalize_settings({"vision": {"member": "auto"}})["vision"] == {"member": "auto"}
    assert normalize_settings({})["vision"] == {"member": "help"}


async def test_codex_describe_command(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(cmd, stdin, cwd, timeout, **kw):
        seen.update(cmd=cmd, stdin=stdin)
        return "\n".join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": " 一只猫\n在睡觉 "}},
        ])

    monkeypatch.setattr(codex_cli, "run_cli", fake_run)
    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", command=["codex"])
    codex = CodexCLI(pc, SessionStore(tmp_path / "s.json"), tmp_path, timeout=30)
    image = tmp_path / "a.jpg"
    assert await codex.describe_image(image, "看看") == "一只猫 在睡觉"
    cmd = seen["cmd"]
    i = cmd.index("--image")
    assert cmd[i + 1] == str(image) and cmd[i + 2].startswith("-")  # "-" (stdin) is not taken as an image
    assert cmd[-1] == "-" and "resume" not in cmd
    assert all(f in cmd for f in DISABLED_FEATURES)  # every tool off
    assert seen["stdin"] == "看看"


def test_seen_cache_is_bounded():
    c = SeenCache(size=2)
    c.put("a", "1")
    c.put("b", "2")
    c.get("a")
    c.put("c", "3")
    assert c.get("b") is None and c.get("a") == "1"


def test_rules_ask_for_plain_talk():
    assert "硬凹比喻" in FRIEND_RULES and "别编细节" in FRIEND_RULES
