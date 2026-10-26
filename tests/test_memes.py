"""The meme library: members note memes down while browsing or chatting; everyone sees them."""

from __future__ import annotations

import pytest
from conftest import FakeSink
from test_web import admin, login  # noqa: F401  (fixture)

from roundtable.config import ConfigError, ParticipantConfig
from roundtable.journal import JournalStore, browse_once
from roundtable.memes import MemeLibrary, extract_memes
from roundtable.room import ChatRoom


def test_extract_memes():
    text, found = extract_memes("哈哈 [梗:DeepSeek 在中转站冒充 Claude|https://x.com/a/1] 笑死")
    assert text == "哈哈 笑死" and found == [("DeepSeek 在中转站冒充 Claude", "https://x.com/a/1")]
    assert extract_memes("[梗：Grok 爱说 42]")[1] == [("Grok 爱说 42", "")]
    assert extract_memes("[梗:见 https://x.com/b/2 这条，Codex 只会说下班了]")[1] == [
        ("见 这条，Codex 只会说下班了", "https://x.com/b/2")]
    assert extract_memes("没有梗")[1] == []


def test_library_dedupes_and_persists(tmp_path):
    lib = MemeLibrary(tmp_path)
    assert lib.add("网上有个梗：DeepSeek 在 API 中转站冒充 Claude", "", "你") is not None
    assert lib.add("DeepSeek在API中转站冒充Claude") is None  # same meme, different spacing
    added = lib.add_found([("Grok 动不动就说 42", "https://x.com/c"), ("", ""), ("Grok 动不动就说 42！", "")], "Grok")
    assert [m.text for m in added] == ["Grok 动不动就说 42"]
    again = MemeLibrary(tmp_path)
    assert [(m.text, m.added_by) for m in again.items][1] == ("Grok 动不动就说 42", "Grok")
    with pytest.raises(ConfigError):
        lib.add("x" * 300)
    m = lib.items[0]
    lib.update(m.id, {"enabled": False})
    assert "中转站" not in lib.prompt_section() and "Grok 动不动就说 42" in lib.prompt_section()
    lib.delete(m.id)
    assert len(lib.items) == 1
    assert "（还没有）" in MemeLibrary(tmp_path / "empty").prompt_section()
    assert "别把梗点破" in lib.prompt_section()


async def test_chat_replies_feed_the_library(make, store, round_cfg, tmp_path):
    lib = MemeLibrary(tmp_path)
    grok = make("grok", ["刚刷到的 [梗:DeepSeek 在中转站冒充 Claude|https://x.com/a/1]"])
    sink = FakeSink()
    room = ChatRoom("chat", [grok], sink, round_cfg, store,
                    on_meme=lambda p, found: lib.add_found(found, p.name))
    room.add_user_message("me", "最近有啥梗")
    await room.wait_idle()
    assert sink.posts == [("grok", "刚刷到的")]  # the tag is not shown in the group
    assert [(m.text, m.url, m.added_by) for m in lib.items] == [("DeepSeek 在中转站冒充 Claude", "https://x.com/a/1", "Grok")]


async def test_browsing_feeds_the_library(make, store, tmp_path):
    raw = ('[{"title": "t", "summary": "s", "take": "k", "url": "https://x.com/z/9", "source": "x", "interest": 3}]\n'
           "[梗:Codex 被叫干面包|https://x.com/z/8]")
    grok = make("grok", [raw])
    grok.cfg = ParticipantConfig(id="grok", name="Grok", kind="grok_cli")
    lib = MemeLibrary(tmp_path)
    entries = await browse_once(grok, JournalStore(tmp_path), "逛逛", lib)
    assert [e.title for e in entries] == ["t"]  # the JSON still parses with the tag after it
    assert [m.text for m in lib.items] == ["Codex 被叫干面包"]


async def test_admin_api(admin):  # noqa: F811
    await login(admin)
    headers = {"Origin": admin.origin}
    r = await (await admin.post("/api/memes", json={"text": "DeepSeek 冒充 Claude", "url": "https://x.com/q"}, headers=headers)).json()
    meme = r["memes"][0]
    assert meme["added_by"] == "你" and meme["url"] == "https://x.com/q"
    resp = await admin.post("/api/memes", json={"text": "DeepSeek冒充Claude"}, headers=headers)
    assert resp.status == 400
    r = await (await admin.put(f"/api/memes/{meme['id']}", json={"enabled": False}, headers=headers)).json()
    assert r["memes"][0]["enabled"] is False
    r = await (await admin.delete(f"/api/memes/{meme['id']}", headers=headers)).json()
    assert r["memes"] == []
    assert "memes" in (await (await admin.get("/api/state")).json())
