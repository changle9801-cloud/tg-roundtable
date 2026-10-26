from __future__ import annotations

import json
import time

import pytest
from conftest import FakeSink
from test_life import Script, IDEA, observation

from roundtable.config import ConfigError
from roundtable.life import LifeStore
from roundtable.prompts import build_turn_prompt
from roundtable.room import ChatRoom
from roundtable.sources import source_list
from roundtable.transcript import Message


SOURCE = {"url": "https://x.com/repair/status/123456", "title": "修拉链的帖子", "excerpt": "作者说链齿完好时可以只换拉头。",
          "read_level": "X 公开嵌入文字，不含评论和视频画面"}


async def test_browsing_share_starts_group_chat_with_only_explicit_source(make, store, round_cfg, tmp_path):
    life = LifeStore(tmp_path)
    secret = life.new_thread("codex", "claude", "私事")
    life.add_message(secret, "claude", "不该进入公开上下文的私事")
    codex = make("codex", ["[PASS]"])
    claude = make("claude", ["看这个摘记，似乎要先检查链齿。"])
    deepseek = make("deepseek", ["我也会先检查一下。"])
    sink = FakeSink()
    room = ChatRoom("group", [codex, claude, deepseek], sink, round_cfg, store, log_path=tmp_path / "group.jsonl")
    async def share(pid, text, sources=()):
        return await room.start_proactive(codex, lambda unseen: "", 1, prepared_text=text, sources=sources)
    script = Script({"action": "explore", "interest": IDEA}, observation(sources=[SOURCE], follow_up="share", message="原来未必需要整条换掉。"))
    engine = script.engine(life)
    engine.share = share
    await engine.step("codex", "claude", True)
    await room.wait_idle()
    assert sink.posts[0] == ("codex", "原来未必需要整条换掉。\n" + SOURCE["url"])
    assert len(sink.posts) == 3
    for member in (claude, deepseek):
        assert SOURCE["excerpt"] in member.prompts[0]
        assert "不代表你已经打开原文" in member.prompts[0]
        assert "不该进入公开上下文的私事" not in member.prompts[0]
    restored = ChatRoom("group", [codex, claude, deepseek], sink, round_cfg, store, log_path=tmp_path / "group.jsonl")
    assert restored.messages[0].sources[0]["url"] == SOURCE["url"]
    assert LifeStore(tmp_path).shares[0]["sources"][0]["excerpt"] == SOURCE["excerpt"]


async def test_no_share_decision_exposes_no_source_or_private_progress(tmp_path):
    life = LifeStore(tmp_path)
    script = Script({"action": "explore", "interest": IDEA}, observation(sources=[SOURCE]))
    await script.engine(life).step("codex", "claude", True)
    assert script.public == [] and life.shares == []
    assert SOURCE["url"] not in life.prompt_section("claude")


async def test_cannot_attach_another_actors_unread_source(tmp_path):
    life = LifeStore(tmp_path)
    item = life.choose_interest("claude", IDEA)
    life.observe(item, observation(sources=[SOURCE]))
    script = Script({"action": "share", "message": "我看到了", "share_urls": [SOURCE["url"]]})
    await script.engine(life).step("codex", "claude", True)
    assert script.public == [] and life.shares == []
    assert life.events[-1]["action"] == "deferred" and "还没读过" in life.events[-1]["note"]


async def test_a_source_mentioned_without_new_json_fields_still_gets_attached(tmp_path):
    life = LifeStore(tmp_path)
    item = life.choose_interest("codex", IDEA)
    life.observe(item, observation(sources=[SOURCE]))
    script = Script({"action": "share", "message": "看看 " + SOURCE["url"]})
    seen = []
    async def share(pid, message, sources=()):
        seen.extend(sources)
        return message
    engine = script.engine(life)
    engine.share = share
    await engine.step("codex", "claude", True)
    assert seen[0]["excerpt"] == SOURCE["excerpt"]


async def test_same_source_is_not_reshared_in_same_group_after_restart(make, store, round_cfg, tmp_path):
    a, b = make("a", []), make("b", [])
    sink = FakeSink()
    log = tmp_path / "group.jsonl"
    room = ChatRoom("g", [a, b], sink, round_cfg, store, log_path=log)
    await room.start_proactive(a, lambda seen: "", 0, prepared_text="看看这个", sources=[SOURCE])
    await room.wait_idle()
    restored = ChatRoom("g", [a, b], sink, round_cfg, store, log_path=log)
    alias = {**SOURCE, "url": "https://twitter.com/repair/status/123456?s=20"}
    assert await restored.start_proactive(b, lambda seen: "", 0, prepared_text="又看到这个", sources=[alias]) is None
    await restored.wait_idle()
    assert len(sink.posts) == 1
    other = ChatRoom("other", [a, b], sink, round_cfg, store)
    assert not other.shared_recently([SOURCE])
    assert not restored.shared_recently([SOURCE], seconds=-1)


async def test_failed_delivery_or_pass_never_creates_shared_evidence(make, store, round_cfg, tmp_path):
    a = make("a", [])
    class FailedSink(FakeSink):
        async def post(self, *args, **kwargs): raise OSError("offline")
    room = ChatRoom("g", [a], FailedSink(), round_cfg, store, log_path=tmp_path / "group.jsonl")
    with pytest.raises(OSError, match="offline"):
        await room.start_proactive(a, lambda seen: "", 0, prepared_text="看看", sources=[SOURCE])
    await room.wait_idle()
    assert not room.messages and not room.shared_recently([SOURCE])
    room.sink = FakeSink()
    assert await room.start_proactive(a, lambda seen: "", 0, prepared_text="[PASS]", sources=[SOURCE]) is None
    await room.wait_idle()
    assert not room.messages and not room.sink.posts


def test_legacy_messages_and_untrusted_source_metadata_are_compatible():
    old = Message.from_json({"speaker_id": "codex", "speaker_name": "Codex", "text": "以前的聊天"})
    assert not old.sources and "sources" not in old.to_json()
    assert source_list([{"url": "javascript:alert(1)"}, {"url": "https://a:b@example.com"}]) == []
    message = Message("codex", "Codex", "看看", sources=tuple(source_list([SOURCE])))
    copied = Message.from_json(json.loads(json.dumps(message.to_json())))
    assert "不含评论" in build_turn_prompt([copied], 1)
