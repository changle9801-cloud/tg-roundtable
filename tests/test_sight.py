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
from roundtable.transcript import Message

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
    assert "@ 一下本人" in p["deepseek"] and "别像解说一样" in p["deepseek"]
    assert "- DeepSeek：看图" in p["claude"]  # in the friends' abilities list
    assert "群里成员的样子" not in p["grok"]  # looks are kept as shared memories instead


def test_describe_mode_and_off(tmp_path):
    p = prompts_for(tmp_path, "auto")
    assert "群里发的图片你看不到" not in p["claude"]  # Codex describes every picture
    assert "群里发的图片你能直接看到" in p["deepseek"]
    assert "看图（群里发的图片都是先由这位看过再说给大家）" in p["claude"]
    off = prompts_for(tmp_path / "off", "off")
    assert all("看图：" not in text for text in off.values())


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


async def test_waking_up_shows_the_pictures_missed_in_sleep(make, store, round_cfg, tmp_path):
    old, new = tmp_path / "old.jpg", tmp_path / "new.jpg"
    for f in (old, new):
        f.write_bytes(b"jpg")
    ds = make("ds", ["哈哈，学姐的身份证"])
    ds.cfg.kind, ds.cfg.vision = "api", True
    room = ChatRoom("chat", [ds], FakeSink(), round_cfg, store)
    room._append(Message("user", "阿明", "[发送了一张图片] 昨晚的", (), 100.0, (str(old),)))
    room._append(Message("user", "阿明", "[发送了一张图片] claude 看你的身份卡", (), 200.0, (str(new),)))
    await room.start_proactive(ds, lambda unseen: "早", 1, show_images_since=150.0)
    await room.wait_idle()
    assert ds.images[0] == [str(new)] and "你睡着时群里发的图片" in ds.prompts[0]


async def test_a_picture_brings_more_friends_over(make, store, round_cfg, tmp_path):
    a, b = make("a", ["是我"]), make("b", ["哈哈"])
    room = ChatRoom("chat", [a, b], FakeSink(), round_cfg, store, InOrder(random.Random(0)))
    room.follow_up_chance, room.follow_up_decay = 0.5, 0.4
    room.moderator.rng.random = lambda: 0.7  # between the usual chance (0.5) and the boosted one (0.9)
    room.add_user_message("阿明", "@A 你看", images=[str(tmp_path / "card.jpg")])
    await room.wait_idle()
    assert [pid for pid, _ in room.sink.posts] == ["a", "b"]

    c, d = make("c", ["嗯"]), make("d", ["不该接"])
    plain = ChatRoom("chat2", [c, d], FakeSink(), round_cfg, store, InOrder(random.Random(0)))
    plain.follow_up_chance, plain.follow_up_decay = 0.5, 0.4
    plain.moderator.rng.random = lambda: 0.7
    plain.add_user_message("阿明", "@C 你看")
    await plain.wait_idle()
    assert [pid for pid, _ in plain.sink.posts] == ["c"]


async def test_missing_files_are_not_handed_over(make, store, round_cfg, tmp_path):
    ds = make("ds", ["嗯"])
    ds.cfg.kind, ds.cfg.vision = "api", True
    room = ChatRoom("chat", [ds], FakeSink(), round_cfg, store)
    room.add_user_message("阿明", "[发送了一张图片]", images=[str(tmp_path / "gone.jpg")])
    await room.wait_idle()
    assert ds.images == [[]] and "附在这条消息里" not in ds.prompts[0]


async def test_claude_cli_gets_pictures_as_a_json_message(tmp_path, monkeypatch):
    import json

    from roundtable.participants import claude_cli
    from roundtable.participants.claude_cli import ClaudeCLI
    from roundtable.vision import can_see_images

    pc = ParticipantConfig(id="claude", name="Claude", kind="claude_cli", command=["claude"])
    assert not can_see_images(pc)
    pc.vision = True
    assert can_see_images(pc)
    claude = ClaudeCLI(pc, SessionStore(tmp_path / "s.json"), tmp_path, timeout=30)
    claude.set_system_prompt("SYSTEM")
    calls = []

    async def fake_run(cmd, stdin, cwd, timeout, **kw):
        calls.append((cmd, stdin))
        return json.dumps({"type": "result", "result": "看到了", "session_id": "s1"})

    monkeypatch.setattr(claude_cli, "run_cli", fake_run)
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG")
    assert await claude.reply("chat", "看这个", images=[image]) == "看到了"
    cmd, stdin = calls[0]
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    content = json.loads(stdin)["message"]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "看这个"}

    await claude.reply("chat", "就聊天")  # no pictures: plain text as before
    cmd, stdin = calls[1]
    assert "--input-format" not in cmd and stdin == "就聊天"


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
