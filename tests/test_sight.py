"""Members who can see get the pictures; the others ask a friend who can."""

from __future__ import annotations

import random

from conftest import FakeSink

from roundtable.config import ParticipantConfig
from roundtable.moderator import RuleModerator
from roundtable.participants import SessionStore
from roundtable.participants.codex_cli import CodexCLI
from roundtable.room import ChatRoom
from roundtable.runtime import Runtime
from roundtable.settings import SettingsStore, build_config, normalize_settings

MEMBERS = [
    {"id": "claude", "name": "Claude", "kind": "claude_cli", "appearance": "橘色长发的学姐"},
    {"id": "codex", "name": "Codex", "kind": "codex_cli", "appearance": "像素小人"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "api", "provider": "deepseek", "vision": True,
     "appearance": "蓝发女仆鲸鱼娘"},
    {"id": "grok", "name": "Grok", "kind": "grok_cli"},
]


def prompts_for(tmp_path, mode="help"):
    rt = Runtime(SettingsStore(tmp_path))
    rt.config = build_config(normalize_settings({"participants": MEMBERS, "vision": {"member": mode}}), {}, tmp_path)
    return {pc.id: rt.system_prompt_for(pc) for pc in rt.config.participants}


def test_who_sees_and_who_asks(tmp_path):
    p = prompts_for(tmp_path)
    assert "群里发的图片你看不到" in p["claude"] and "@Codex、@DeepSeek" in p["claude"]
    assert "群里发的图片你能直接看到" in p["deepseek"] and "群里发的图片你能直接看到" in p["codex"]
    assert "- DeepSeek：看图" in p["claude"]  # in the friends' abilities list
    assert "群里成员的样子" in p["grok"] and "- Claude：橘色长发的学姐" in p["grok"]


def test_describe_mode_and_off(tmp_path):
    p = prompts_for(tmp_path, "auto")
    assert "群里发的图片你看不到" not in p["claude"]  # Codex describes every picture
    assert "群里发的图片你能直接看到" in p["deepseek"]
    assert "看图（群里发的图片都是先由这位看过再说给大家）" in p["claude"]
    off = prompts_for(tmp_path / "off", "off")
    assert all("看图：" not in text and "群里成员的样子" not in text for text in off.values())


class InOrder(RuleModerator):
    def pick(self, candidates, last_message, last_speaker_id):
        mentioned = [c for c in candidates if c.id in last_message.mentions]
        return (mentioned or candidates)[0] if candidates else None


async def test_pictures_reach_only_those_who_can_see(make, store, round_cfg, tmp_path):
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"jpg")
    ds = make("ds", ["哈哈，Claude 被洗掉色了", "就是 DeepSeek 冒充 Claude 啦"])
    ds.cfg.kind, ds.cfg.vision = "api", True
    blind = make("blind", ["看不了图 @Ds 帮我说说？"])
    room = ChatRoom("chat", [ds, blind], FakeSink(), round_cfg, store, InOrder(random.Random(0)))
    room.add_user_message("阿明", "[发送了一张图片] 看到个有意思的图", images=[str(image)])
    await room.wait_idle()
    assert ds.images[0] == [str(image)] and "附在这条消息里" in ds.prompts[0]
    assert blind.images and not any(blind.images)  # never handed a picture
    # Asked for help after it already saw the picture: it gets the picture again.
    assert ds.images[1] == [str(image)]


async def test_missing_files_are_not_handed_over(make, store, round_cfg, tmp_path):
    ds = make("ds", ["嗯"])
    ds.cfg.kind, ds.cfg.vision = "api", True
    room = ChatRoom("chat", [ds], FakeSink(), round_cfg, store)
    room.add_user_message("阿明", "[发送了一张图片]", images=[str(tmp_path / "gone.jpg")])
    await room.wait_idle()
    assert ds.images == [[]] and "附在这条消息里" not in ds.prompts[0]


def test_codex_attaches_pictures(tmp_path):
    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", command=["codex"])
    codex = CodexCLI(pc, SessionStore(tmp_path / "s.json"), tmp_path, timeout=30)
    image = tmp_path / "a.jpg"
    for session in (None, "thread-1"):
        cmd = codex.build_command(session, [image])
        i = cmd.index("--image")
        assert cmd[i + 1] == str(image) and cmd[i + 2].startswith("-")
        assert cmd[-1] == "-"
    assert "--image" not in codex.build_command("thread-1")
