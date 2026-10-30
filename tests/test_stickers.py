from __future__ import annotations

import random

import pytest
from conftest import FakeSink

from nookling.config import ConfigError
from nookling.conversation import Conversation
from nookling.stickers import StickerLibrary, parse_set_name, strip_tags

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture
def lib(tmp_path):
    lib = StickerLibrary(tmp_path, random.Random(0))
    lib.add("狗头.png", PNG, "狗头", "阴阳怪气的时候")
    lib.add("a.gif", b"GIF89a", "无语")
    return lib


def test_add_persists_and_reloads(tmp_path, lib):
    again = StickerLibrary(tmp_path)
    assert [(s.label, s.kind) for s in again.items] == [("狗头", "photo"), ("无语", "animation")]
    assert again.items[0].path.read_bytes() == PNG


def test_rejects_bad_files(lib):
    with pytest.raises(ConfigError, match="不支持"):
        lib.add("x.exe", b"MZ", "x")
    with pytest.raises(ConfigError, match="空"):
        lib.add("x.png", b"", "x")
    with pytest.raises(ConfigError, match="名称"):
        lib.add("x.png", PNG, "  ")


def test_extract_splits_text_and_stickers(lib):
    plain, stickers, transcript = lib.extract("哈哈哈 [表情:狗头]", 1)
    assert plain == "哈哈哈"
    assert [s.label for s in stickers] == ["狗头"]
    assert transcript == "哈哈哈 [表情:狗头]"


def test_extract_accepts_variants_and_respects_limit(lib):
    plain, stickers, transcript = lib.extract("[表情：无语][sticker:狗头] 好吧", 1)
    assert plain == "好吧"
    assert [s.label for s in stickers] == ["无语"]
    assert transcript == "好吧 [表情:无语]"


def test_extract_drops_unknown_and_disabled(lib):
    lib.update(lib.items[1].id, {"enabled": False})
    plain, stickers, transcript = lib.extract("行 [表情:无语] [表情:不存在]", 2)
    assert (plain, stickers, transcript) == ("行", [], "行")


def test_sticker_only_reply(lib):
    plain, stickers, transcript = lib.extract("[表情:狗头]", 1)
    assert plain == "" and len(stickers) == 1 and transcript == "[表情:狗头]"


def test_strip_tags_without_library():
    assert strip_tags("好 [表情:狗头]")[0] == "好"


def test_prompt_section_lists_labels_with_descriptions(lib):
    section = lib.prompt_section(1)
    assert "狗头（阴阳怪气的时候）" in section and "无语" in section
    assert lib.prompt_section(0) == ""
    assert StickerLibrary(lib.dir.parent / "empty").prompt_section(1) == ""


def test_delete_removes_file(lib):
    s = lib.items[0]
    lib.delete(s.id)
    assert not s.path.exists()
    assert [x.label for x in lib.items] == ["无语"]


@pytest.mark.parametrize("value,name", [
    ("https://t.me/addstickers/Funny_Cats", "Funny_Cats"),
    ("t.me/addstickers/abc/", "abc"),
    ("PlainName", "PlainName"),
])
def test_parse_set_name(value, name):
    assert parse_set_name(value) == name


def test_parse_set_name_rejects_garbage():
    with pytest.raises(ConfigError):
        parse_set_name("not a name!")


async def test_room_sends_sticker_and_records_it(make, round_cfg, store, lib):
    a = make("a", ["好家伙 [表情:狗头]"])
    b = make("b", ["[PASS]"])
    sink = FakeSink()
    room = Conversation("chat", [a, b], sink, round_cfg, store, stickers=lib, sticker_limit=1)

    room.add_user_message("me", "hi @A")
    await room.wait_idle()

    assert sink.posts == [("a", "好家伙")]
    assert sink.stickers == [("a", "狗头")]
    assert any("A: 好家伙 [表情:狗头]" in p for p in b.prompts)


async def test_room_strips_tags_when_stickers_are_off(make, round_cfg, store, lib):
    a = make("a", ["[表情:狗头]", "真的"])
    sink = FakeSink()
    room = Conversation("chat", [a], sink, round_cfg, store, stickers=lib, sticker_limit=0)

    room.add_user_message("me", "hi")
    await room.wait_idle()

    # A sticker-only reply with stickers off counts as a pass.
    assert sink.posts == [] and sink.stickers == []
