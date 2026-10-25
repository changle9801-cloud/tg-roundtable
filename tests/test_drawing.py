from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import FakeSink

from roundtable.config import ParticipantConfig
from roundtable.drawing import (
    DrawLog,
    Drawing,
    build_draw_prompt,
    drawer_instructions,
    extract_draw_requests,
)
from roundtable.participants import ParticipantError
from roundtable.participants import codex_cli
from roundtable.participants.codex_cli import CodexCLI
from roundtable.room import ChatRoom
from roundtable.stickers import StickerLibrary
from roundtable.transcript import Message

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def test_extract_draw_requests():
    text, draws = extract_draw_requests("行，等我一会儿。[画:我不是肥鱼|鲸鱼娘抱着尾巴，写着我不是肥鱼]")
    assert text == "行，等我一会儿。"
    assert draws == [("我不是肥鱼", "鲸鱼娘抱着尾巴，写着我不是肥鱼")]
    assert extract_draw_requests("[画：委屈｜一只哭的鲸鱼]")[1] == [("委屈", "一只哭的鲸鱼")]
    assert extract_draw_requests("没有标记") == ("没有标记", [])


def test_prompts():
    assert "明天" in drawer_instructions(0)
    assert "还能画 3 张" in drawer_instructions(3)
    members = [ParticipantConfig(id="ds", name="DeepSeek", kind="api", appearance="蓝发女仆鲸鱼娘")]
    prompt = build_draw_prompt("我不是肥鱼", "抱着尾巴", members)
    assert "我不是肥鱼" in prompt and "蓝发女仆鲸鱼娘" in prompt and "Q 版" in prompt


def test_daily_count(tmp_path):
    log = DrawLog(tmp_path)
    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", can_draw=True, draws_per_day=2)
    assert log.remaining(pc) == 2
    log.record(Drawing(time.time(), "codex", "DeepSeek", "a", "x", True))
    log.record(Drawing(time.time() - 3 * 86400, "codex", "DeepSeek", "b", "x", True))  # another day
    assert log.remaining(pc) == 1
    assert DrawLog(tmp_path).remaining(pc) == 1  # persisted


def test_unique_label(tmp_path):
    lib = StickerLibrary(tmp_path)
    assert lib.unique_label("肥鱼") == "肥鱼"
    lib.add("a.png", PNG, "肥鱼")
    lib.add("b.png", PNG, "肥鱼2")
    assert lib.unique_label("肥鱼") == "肥鱼3"


async def test_drawer_takes_the_request_and_tag_is_hidden(make, round_cfg, store):
    ds = make("ds", ["哼！@Codex 帮我画个表情包怼回去"])
    codex = make("codex", ["行，等我一会儿。[画:我不是肥鱼|鲸鱼娘抱尾巴]"])
    calls = []
    sink = FakeSink()
    room = ChatRoom("chat", [ds, codex], sink, round_cfg, store,
                    on_draw=lambda room, drawer, req, asker: calls.append((drawer.id, req, asker.speaker_id if asker else None)))
    room.add_user_message("me", "@ds 大肥鱼又摸鱼了")
    await room.wait_idle()

    assert ("codex", "行，等我一会儿。") in sink.posts
    assert calls == [("codex", ("我不是肥鱼", "鲸鱼娘抱尾巴"), "ds")]  # the one who @'d it


async def test_delivery_gives_the_requester_a_turn_to_use_it(make, round_cfg, store, tmp_path):
    lib = StickerLibrary(tmp_path / "lib")
    sticker = lib.add("x.png", PNG, "我不是肥鱼")
    ds = make("ds", ["[表情:我不是肥鱼] 哼"])
    codex = make("codex", [])
    sink = FakeSink()
    room = ChatRoom("chat", [ds, codex], sink, round_cfg, store, stickers=lib, sticker_limit=1)
    asker = Message("ds", "Ds", "@Codex 帮我画", ("codex",), 1.0)
    room.messages.append(asker)

    await room.deliver_drawing(codex, "画好了 @Ds，拿去用。", sticker, asker)
    await room.wait_idle()

    assert sink.posts[0] == ("codex", "画好了 @Ds，拿去用。")
    assert sink.stickers == [("codex", "我不是肥鱼"), ("ds", "我不是肥鱼")]
    assert "Codex: 画好了 @Ds，拿去用。 [表情:我不是肥鱼]" in ds.prompts[0]


async def test_delivery_during_a_round_adds_a_turn(make, round_cfg, store, tmp_path):
    lib = StickerLibrary(tmp_path / "lib")
    sticker = lib.add("x.png", PNG, "我不是肥鱼")
    ds = make("ds", ["先说一句", "[表情:我不是肥鱼]"], delay=0.05)
    codex = make("codex", [])
    sink = FakeSink()
    round_cfg.max_turns_per_model = 1  # ds has used its only turn when the sticker arrives
    room = ChatRoom("chat", [ds, codex], sink, round_cfg, store, stickers=lib, sticker_limit=1)
    room.add_user_message("me", "Ds 你好")  # addressed, so ds speaks first
    await asyncio.sleep(0.02)  # ds is mid-turn
    asker = Message("ds", "Ds", "@Codex 帮我画", ("codex",), 1.0)
    await room.deliver_drawing(codex, "画好了", sticker, asker)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ["codex", "ds"]
    assert ("ds", "我不是肥鱼") in sink.stickers  # the extra turn let it use the sticker


async def test_failure_posts_nothing_extra(make, round_cfg, store):
    codex = make("codex", [])
    sink = FakeSink()
    room = ChatRoom("chat", [codex], sink, round_cfg, store)
    await room.deliver_drawing(codex, "这张翻车了，没画出来。", None, None)
    assert sink.posts == [("codex", "这张翻车了，没画出来。")] and sink.stickers == []
    assert not room.busy


async def test_codex_draw_enables_only_the_image_tool(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    seen = {}

    async def fake_run_cli(cmd, stdin_text, cwd, timeout, env=None, on_line=None):
        seen["cmd"], seen["prompt"] = cmd, stdin_text
        folder = tmp_path / "codex-home" / "generated_images" / "thread-1"
        folder.mkdir(parents=True)
        (folder / "exec-1.png").write_bytes(PNG)
        return json.dumps({"type": "thread.started", "thread_id": "thread-1"}) + "\n"

    monkeypatch.setattr(codex_cli, "run_cli", fake_run_cli)
    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", command=["codex"])
    path = await CodexCLI(pc, store, tmp_path / "w", timeout=5).draw("画一只鲸鱼")

    assert path.read_bytes() == PNG and seen["prompt"] == "画一只鲸鱼"
    disabled = {seen["cmd"][i + 1] for i, a in enumerate(seen["cmd"]) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "computer_use", "browser_use"} <= disabled
    assert "image_generation" not in disabled
    assert 'web_search="disabled"' in seen["cmd"] and 'sandbox_mode="read-only"' in seen["cmd"]


async def test_codex_draw_without_an_image_fails(tmp_path, store, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    async def fake_run_cli(*a, **k):
        return json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n"

    monkeypatch.setattr(codex_cli, "run_cli", fake_run_cli)
    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", command=["codex"])
    with pytest.raises(ParticipantError):
        await CodexCLI(pc, store, tmp_path / "w", timeout=5).draw("x")
