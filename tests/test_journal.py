from __future__ import annotations

import json

import pytest

from roundtable.config import ConfigError, ParticipantConfig
from roundtable.journal import BROWSE_CHAT, DAY, JournalStore, browse_once, parse_findings
from roundtable.prompts import build_browse_prompt, build_share_prompt

NOW = 1_800_000_000.0


def grok(**kw):
    return ParticipantConfig(id="grok", name="Grok", kind="grok_cli", **kw)


def item(url="https://x.com/a/1", title="棕矮星的月亮", interest=3, **kw):
    return {"title": title, "summary": "讲了什么", "take": "笑死", "url": url, "interest": interest, **kw}


def test_add_dedupes_and_validates(tmp_path):
    j = JournalStore(tmp_path)
    pc = grok()
    added = j.add(pc, [item(), item(), item(url="javascript:alert(1)"), item(title="")], now=NOW)
    assert [e.url for e in added] == ["https://x.com/a/1"]
    assert added[0].source == "x"
    assert j.add(pc, [item()], now=NOW) == []  # already in the journal
    assert JournalStore(tmp_path).of("grok")[0].title == "棕矮星的月亮"  # persisted


def test_at_most_three_per_session(tmp_path):
    j = JournalStore(tmp_path)
    added = j.add(grok(), [item(url=f"https://e.com/{i}") for i in range(6)], now=NOW)
    assert len(added) == 3


def test_memorable_and_shared_entries_are_kept_longer(tmp_path):
    j = JournalStore(tmp_path)
    pc = grok(journal_days=3, journal_keep_days=7)
    plain, memorable = j.add(pc, [item(url="https://e.com/1", interest=2), item(url="https://e.com/2", interest=5)], now=NOW)
    assert plain.expires_at == NOW + 3 * DAY
    assert memorable.expires_at == NOW + 7 * DAY

    j.mark_shared(pc, plain.id, now=NOW + DAY)
    assert j.get(plain.id).expires_at == NOW + 7 * DAY and j.get(plain.id).shared_ts

    assert j.forget_expired(now=NOW + 8 * DAY) == 2
    assert j.of("grok") == []


def test_best_unshared_prefers_interest(tmp_path):
    j = JournalStore(tmp_path)
    pc = grok()
    j.add(pc, [item(url="https://e.com/1", interest=2), item(url="https://e.com/2", interest=4), item(url="https://e.com/3", interest=3)], now=NOW)
    best = j.best_unshared("grok", now=NOW)
    assert best.url == "https://e.com/2"
    j.mark_shared(pc, best.id, now=NOW)
    assert j.best_unshared("grok", now=NOW).url == "https://e.com/3"
    j.mark_shared(pc, j.best_unshared("grok", now=NOW).id, now=NOW)
    assert j.best_unshared("grok", now=NOW) is None  # interest 2 is not worth sharing


def test_entries_are_private_to_each_member(tmp_path):
    j = JournalStore(tmp_path)
    j.add(grok(), [item()], now=NOW)
    assert j.prompt_section("claude") == ""
    section = j.prompt_section("grok")
    assert "棕矮星的月亮" in section and "https://x.com/a/1" in section and "不要编造" in section


def test_delete(tmp_path):
    j = JournalStore(tmp_path)
    (e,) = j.add(grok(), [item()], now=NOW)
    j.delete(e.id)
    assert j.of("grok") == []
    with pytest.raises(ConfigError):
        j.delete(e.id)


@pytest.mark.parametrize("raw,count", [
    ('[{"title": "a", "url": "https://e.com"}]', 1),
    ('好的，我刷到了：\n```json\n[{"title": "a", "url": "https://e.com"}]\n```', 1),
    ("[]", 0),
    ("[PASS]", 0),
    ("没找到", 0),
    ('{"title": "not a list"}', 0),
])
def test_parse_findings(raw, count):
    assert len(parse_findings(raw)) == count


async def test_browse_once_uses_a_throwaway_session(make, store):
    p = make("grok", [json.dumps([item()], ensure_ascii=False)])
    store.set(BROWSE_CHAT, "grok", "old-session")
    j = JournalStore(p.work_dir)
    added = await browse_once(p, j, "BROWSE")
    assert [e.title for e in added] == ["棕矮星的月亮"]
    assert p.prompts == ["BROWSE"]
    assert store.get(BROWSE_CHAT, "grok") is None


def test_prompts():
    browse = build_browse_prompt("宇宙", True, ["https://x.com/old"])
    assert "不会发到群里" in browse and "X（推特）" in browse and "https://x.com/old" in browse
    j_entry = type("E", (), dict(source="x", title="T", summary="S", take="K", url="https://x.com/a/1"))
    share = build_share_prompt([], j_entry)
    assert "https://x.com/a/1" in share and "[PASS]" in share
