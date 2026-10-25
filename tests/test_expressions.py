"""Stickers and kaomoji per member, editable after upload."""

from __future__ import annotations

import random

import pytest
from aiohttp import FormData
from conftest import FakeSink
from test_web import admin, login  # noqa: F401  (fixture)

from roundtable.config import ConfigError
from roundtable.kaomoji import STARTER, KaomojiLibrary, parse_lines
from roundtable.prompts import FRIEND_RULES
from roundtable.room import ChatRoom
from roundtable.stickers import StickerLibrary

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
GIF = b"GIF89a" + b"0" * 8


@pytest.fixture
def lib(tmp_path):
    lib = StickerLibrary(tmp_path, random.Random(0))
    lib.add("a.png", PNG, "狗头")
    lib.add("b.png", PNG, "委屈", members=["deepseek"])
    return lib


def test_stickers_only_reach_their_members(tmp_path, lib):
    assert "委屈" in lib.prompt_section(1, "deepseek")
    assert "委屈" not in lib.prompt_section(1, "grok") and "狗头" in lib.prompt_section(1, "grok")
    _, sent, _ = lib.extract("[表情:委屈]", 1, "grok")
    assert sent == []  # not one of grok's
    _, sent, _ = lib.extract("[表情:委屈]", 1, "deepseek")
    assert [s.label for s in sent] == ["委屈"]
    assert StickerLibrary(tmp_path).items[1].members == ["deepseek"]  # saved


def test_members_can_be_changed_and_checked(lib):
    s = lib.items[0]
    lib.update(s.id, {"members": ["grok", " claude ", "grok"]})
    assert s.members == ["claude", "grok"]
    lib.update(s.id, {"members": []})
    assert s.members == []
    with pytest.raises(ConfigError):
        lib.update(s.id, {"members": "grok"})


def test_replace_file_keeps_everything_else(tmp_path, lib):
    s = lib.items[1]
    old_path, old_file = s.path, s.file
    lib.replace_file(s.id, "new.gif", GIF)
    assert s.file != old_file and s.kind == "animation" and s.path.read_bytes() == GIF
    assert not old_path.exists()
    again = StickerLibrary(tmp_path).items[1]
    assert (again.label, again.members, again.file) == ("委屈", ["deepseek"], s.file)
    with pytest.raises(ConfigError, match="不支持"):
        lib.replace_file(s.id, "x.exe", b"MZ")


async def test_room_drops_stickers_a_member_was_not_given(make, store, round_cfg, lib):
    grok = make("grok", ["哈 [表情:委屈]"])
    sink = FakeSink()
    room = ChatRoom("chat", [grok], sink, round_cfg, store, stickers=lib, sticker_limit=1)
    room.add_user_message("me", "hi")
    await room.wait_idle()
    assert sink.posts == [("grok", "哈")] and sink.stickers == []


def test_kaomoji_library(tmp_path):
    k = KaomojiLibrary(tmp_path)
    face = k.add("(｡•́︿•̀｡)", "委屈", ["deepseek"])
    k.add("( ´_ゝ`)", "敷衍")
    with pytest.raises(ConfigError, match="已经"):
        k.add("(｡•́︿•̀｡)")
    with pytest.raises(ConfigError, match="一行"):
        k.add("a\nb")
    assert "(｡•́︿•̀｡)（委屈）" in k.prompt_section("deepseek")
    assert "(｡•́︿•̀｡)" not in k.prompt_section("grok") and "( ´_ゝ`)" in k.prompt_section("grok")

    k.update(face.id, {"enabled": False})
    assert "(｡•́︿•̀｡)" not in k.prompt_section("deepseek")
    k.update(face.id, {"text": "(つд⊂)", "enabled": True})
    again = KaomojiLibrary(tmp_path)
    assert [(i.text, i.members) for i in again.items] == [("(つд⊂)", ["deepseek"]), ("( ´_ゝ`)", [])]
    k.delete(face.id)
    assert k.prompt_section("deepseek").count("（") == 1
    assert KaomojiLibrary(tmp_path / "empty").prompt_section("x") == ""


def test_kaomoji_bulk_add():
    assert parse_lines("(つд⊂) | 哭\n\n( ´_ゝ`)\n(￣▽￣)｜看戏") == [("(つд⊂)", "哭"), ("( ´_ゝ`)", ""), ("(￣▽￣)", "看戏")]


def test_starter_set_skips_duplicates(tmp_path):
    k = KaomojiLibrary(tmp_path)
    k.add(STARTER[0][0])
    assert k.add_many(STARTER) == len(STARTER) - 1
    assert k.add_many(STARTER) == 0


def test_rules_allow_one_line_kaomoji():
    assert "颜文字" in FRIEND_RULES and "一行的颜文字可以用" in FRIEND_RULES


async def test_admin_api_for_kaomoji_and_sticker_members(admin):  # noqa: F811
    await login(admin)
    headers = {"Origin": admin.origin}
    settings = (await (await admin.get("/api/state")).json())["settings"]
    settings["participants"] = [{"id": "a", "name": "阿杰", "kind": "claude_cli"}, {"id": "b", "name": "小美", "kind": "claude_cli"}]
    assert (await admin.put("/api/settings", json=settings, headers=headers)).status == 200

    r = await (await admin.post("/api/kaomoji", json={"text": "(｡•́︿•̀｡)", "mood": "委屈", "members": ["a"]}, headers=headers)).json()
    kid = r["kaomoji"][0]["id"]
    r = await (await admin.post("/api/kaomoji", json={"lines": "(つд⊂) | 哭\n(｡•́︿•̀｡)"}, headers=headers)).json()
    assert r["added"] == 1
    r = await (await admin.post("/api/kaomoji", json={"starter": True}, headers=headers)).json()
    assert r["added"] == len(STARTER) - 2
    resp = await admin.post("/api/kaomoji", json={"text": ""}, headers=headers)
    assert resp.status == 400

    prompt_a = admin.runtime.participants[0].system_prompt
    prompt_b = admin.runtime.participants[1].system_prompt
    assert "(｡•́︿•̀｡)（委屈）" in prompt_a and "(｡•́︿•̀｡)" not in prompt_b

    await admin.put(f"/api/kaomoji/{kid}", json={"members": ["b"]}, headers=headers)
    assert "(｡•́︿•̀｡)" in admin.runtime.participants[1].system_prompt
    r = await (await admin.delete(f"/api/kaomoji/{kid}", headers=headers)).json()
    assert all(k["id"] != kid for k in r["kaomoji"])

    form = FormData()
    form.add_field("file", PNG, filename="狗头.png", content_type="image/png")
    sticker = (await (await admin.post("/api/stickers", data=form, headers=headers)).json())["stickers"][0]
    r = await (await admin.put(f"/api/stickers/{sticker['id']}", json={"members": ["b"], "description": "阴阳怪气"}, headers=headers)).json()
    assert r["stickers"][0]["members"] == ["b"]
    assert "狗头" not in admin.runtime.participants[0].system_prompt
    assert "狗头（阴阳怪气）" in admin.runtime.participants[1].system_prompt

    form = FormData()
    form.add_field("file", GIF, filename="动图.gif", content_type="image/gif")
    resp = await admin.put(f"/api/stickers/{sticker['id']}/file", data=form, headers=headers)
    assert resp.status == 200, await resp.text()
    after = (await resp.json())["stickers"][0]
    assert after["kind"] == "animation" and after["file"] != sticker["file"] and after["members"] == ["b"]
    assert await (await admin.get(f"/api/stickers/{sticker['id']}/file")).read() == GIF

    state = await (await admin.get("/api/state")).json()
    assert state["settings"]["stickers"]["kaomoji"] is True and "kaomoji" in state
