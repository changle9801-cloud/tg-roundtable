import json
import time

import pytest
from conftest import FakeSink
from test_life import runtime

from roundtable.participants import ParticipantError, SessionStore
from roundtable.prompts import build_personal_share_prompt
from roundtable.room import ChatRoom
from roundtable.transcript import Message


SOURCE = {"url": "https://example.com/whale", "title": "Hafgufa", "read_level": "读了介绍段落"}
OPENING = "@B 我后来查过了，生火那段得从 Hafgufa 的笔记里划掉。"


async def test_prepared_share_is_seen_by_author_once_even_after_restart(make, round_cfg, store, tmp_path):
    a = make("a", ["[PASS]", "[PASS]"])
    b = make("b", [])
    log = tmp_path / "chat.jsonl"
    room = ChatRoom("chat", [a, b], FakeSink(), round_cfg, store, log_path=log)
    room._append(Message("user", "User", "之前漏看的群消息", ts=time.time()))
    cursor = room.cursors["a"]
    assert await room.start_proactive(a, lambda _: pytest.fail("prepared text needs no prompt"), 0,
                                      prepared_text=OPENING, sources=[SOURCE])
    await room.wait_idle()
    assert room.cursors["a"] == cursor and a.prompts == []
    assert room.messages[-1].external_to_session
    loaded = SessionStore(tmp_path / "sessions.json")
    again = ChatRoom("chat", [a, b], FakeSink(), round_cfg, loaded, log_path=log)
    await again._ask(a, "B: 那我笔记得改一下", 1)
    assert OPENING in a.prompts[-1] and SOURCE["read_level"] in a.prompts[-1]
    assert "你自己" in a.prompts[-1]
    loaded = SessionStore(tmp_path / "sessions.json")
    again = ChatRoom("chat", [a, b], FakeSink(), round_cfg, loaded, log_path=log)
    await again._ask(a, "B: 聊点别的", 1)
    assert "已发出的本人消息" not in a.prompts[-1]
    await again._ask(b, "new topic", 1)
    assert "已发出的本人消息" not in b.prompts[-1]


async def test_failed_recall_is_retried_and_new_session_receives_it_again(make, round_cfg, store):
    a = make("a", [ParticipantError("offline"), "[PASS]", "[PASS]"])
    room = ChatRoom("chat", [a], FakeSink(), round_cfg, store)
    await room._publish(a, OPENING, external_to_session=True)
    with pytest.raises(ParticipantError):
        await room._ask(a, "peer acknowledged", 1)
    await room._ask(a, "peer acknowledged", 1)
    assert all(OPENING in p for p in a.prompts)
    store.set("chat", "a", "new-session")
    await room._ask(a, "peer again", 1)
    assert OPENING in a.prompts[-1]


async def test_failed_delivery_does_not_create_a_recall(make, round_cfg, store):
    a = make("a", ["[PASS]"])
    sink = FakeSink()
    async def fail(*args, **kwargs):
        raise RuntimeError("send failed")
    sink.post = fail
    room = ChatRoom("chat", [a], sink, round_cfg, store)
    with pytest.raises(RuntimeError):
        await room._publish(a, OPENING, external_to_session=True)
    assert not room.messages
    await room._ask(a, "peer", 1)
    assert OPENING not in a.prompts[-1]


async def test_external_post_arriving_during_call_is_not_acknowledged(make, round_cfg, store):
    a = make("a", ["[PASS]"])
    room = ChatRoom("chat", [a], FakeSink(), round_cfg, store)
    async def reply(*args, **kwargs):
        await room._publish(a, OPENING, external_to_session=True)
        return "[PASS]"
    original = a.reply
    a.reply = reply
    await room._ask(a, "first", 1)
    a.reply = original
    await room._ask(a, "second", 1)
    assert OPENING in a.prompts[-1]


async def test_public_share_can_pass_and_recovered_session_keeps_share_intent(make, round_cfg, store):
    a = make("a", [ParticipantError("missing session"), "[PASS]"])
    store.set("chat", "a", "lost")
    sink = FakeSink()
    room = ChatRoom("chat", [a], sink, round_cfg, store)
    result = await room.start_proactive(a, lambda unseen: build_personal_share_prompt(
        unseen, OPENING, [SOURCE], me=a.id), 0, sources=[SOURCE])
    await room.wait_idle()
    assert result is None and not sink.posts
    assert OPENING in a.prompts[-1] and SOURCE["read_level"] in a.prompts[-1]


def test_legacy_share_reconciliation_matches_public_record_only(tmp_path):
    r = runtime(tmp_path)
    stamp = time.time()
    r.life.shares = [{"member": "claude", "text": OPENING, "ts": stamp + 5},
                     {"member": "claude", "text": "new composed", "ts": stamp + 8,
                      "external_to_session": False}]
    messages = [Message("claude", "Claude", OPENING, ts=stamp),
                Message("codex", "Codex", OPENING, ts=stamp),
                Message("claude", "Claude", "new composed", ts=stamp)]
    path = r.chat_log_path("-100")
    path.parent.mkdir(parents=True)
    original = "\n".join(json.dumps(m.to_json()) for m in messages) + "\n"
    path.write_text(original, encoding="utf-8")
    room = r.room(-100)
    assert [m.external_to_session for m in room.messages] == [True, False, False]
    assert path.read_text(encoding="utf-8") == original


def test_context_receipts_follow_chat_rename_and_clear(store, tmp_path):
    store.mark_context_seen("old", "a", ["posted"])
    store.rename("old", "new")
    loaded = SessionStore(tmp_path / "sessions.json")
    assert loaded.context_seen("new", "a", "posted")
    loaded.clear_one("new", "a")
    assert not loaded.context_seen("new", "a", "posted")
    loaded.mark_context_seen("new", "a", ["posted"])
    loaded.clear("new")
    assert not SessionStore(tmp_path / "sessions.json").context_seen("new", "a", "posted")


async def test_bounded_recall_does_not_surface_older_omitted_posts_on_later_turns(make, round_cfg, store):
    a = make("a", ["[PASS]", "[PASS]"])
    room = ChatRoom("chat", [a], FakeSink(), round_cfg, store)
    for i in range(5):
        room._append(Message("a", "A", f"旧代发 {i}", ts=time.time() + i, external_to_session=True))
    await room._ask(a, "peer", 1)
    assert "旧代发 4" in a.prompts[-1] and "旧代发 0" not in a.prompts[-1]
    await room._ask(a, "peer again", 1)
    assert "旧代发" not in a.prompts[-1]
