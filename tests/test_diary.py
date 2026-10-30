from datetime import datetime

import pytest

from roundtable.life import LifeEngine, LifeStore
from test_life import Script, runtime


def reading(**changes):
    return {"outcome": "progress", "title": "磁吸手柄拆机", "progress": "读到了磁吸手柄释放按钮的说明。",
            "take": "拆开以后挺有意思，后半段讲得有点啰嗦。", "diary": "今天看了磁吸手柄拆机，结构挺有意思，但后面看得有点无聊。",
            "feelings": {"好奇": 0.8, "无聊": 0.4}, "topics": ["磁吸", "手柄"],
            "sources": [{"url": "https://example.com/controller", "title": "手柄拆机", "read_level": "网页文字，不含视频画面"}],
            **changes}


def test_diary_keeps_mixed_impressions_and_isolates_members_after_restart(tmp_path):
    store = LifeStore(tmp_path)
    found = store.remember_discovery("codex", reading())
    again = LifeStore(tmp_path)
    entries = again.diary.of("codex")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.ref == found.id
    assert entry.feelings == {"好奇": 0.8, "无聊": 0.4}
    assert "无聊" in entry.thoughts
    assert entry.sources[0]["read_level"] == "网页文字，不含视频画面"
    assert "磁吸手柄" in again.diary.recall("codex", "你说那个磁吸手柄靠谱吗")
    assert again.diary.recall("claude", "磁吸手柄") == ""
    assert again.diary.recall("codex", "明天吃火锅吗") == ""


def test_negative_reading_counts_as_experience_but_failed_read_does_not(tmp_path):
    store = LifeStore(tmp_path)
    assert store.remember_discovery("codex", reading(outcome="nothing_new", take="无聊，不想再看了"))
    assert store.remember_discovery("claude", reading(outcome="blocked", sources=[])) is None
    assert len(store.diary.of("codex")) == 1
    assert not store.diary.of("claude")


def test_diary_deduplicates_receipts_and_feelings_fade_without_losing_story(tmp_path):
    from roundtable.diary import DiaryStore
    clock = [datetime(2026, 10, 30, 12).timestamp()]
    diary = DiaryStore(tmp_path, now=lambda: clock[0])
    entry = diary.record("codex", "receipt-1", reading())
    diary.record("codex", "receipt-1", reading())
    assert len(diary.of("codex")) == 1
    assert entry.strength(clock[0]) == pytest.approx(0.8)
    clock[0] += 3 * 86400
    assert entry.strength(clock[0]) == pytest.approx(0.4)
    assert diary.view()[0]["day"] == "2026-10-30"
    assert "无聊" in diary.of("codex")[0].thoughts


def test_own_experience_changes_participation_and_enters_only_owners_context(tmp_path):
    from roundtable.room import _RoundState
    from roundtable.transcript import Message
    r = runtime(tmp_path)
    r.life.remember_discovery("claude", reading())
    room = r.room(-100)
    message = Message("user", "Orion", "磁吸手柄靠谱吗", ts=100)
    room.messages.append(message)
    scored = {p.id: chance for p, chance, _ in room._urges(r.participants, message, _RoundState(room._fresh_turns(), None))}
    assert scored["claude"].value > scored["codex"].value
    assert "自己的相关经历" in scored["claude"].explain()
    prompt = "[群聊新消息]\nOrion: 磁吸手柄靠谱吗\n[/群聊新消息]"
    assert "后面看得有点无聊" in r._chat_context(r._member("claude").cfg, prompt)
    assert "后面看得有点无聊" not in r._chat_context(r._member("codex").cfg, prompt)


def finish_immediate(coroutine):
    """Exercise immediate coroutines; real asynchronous I/O belongs in integration tests."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise AssertionError("This in-memory step unexpectedly tried to suspend")


def test_no_goal_member_can_browse_and_keep_verified_private_diary(tmp_path):
    r = runtime(tmp_path)
    r._member("claude").cfg.web_search = True
    script = Script({"action": "browse"}, reading(), reading(outcome="nothing_new"))
    r._life_ask = script.ask

    async def read(url):
        return {"status": "ok", "url": url, "title": "磁吸手柄拆机", "content": "手柄的释放按钮会推动组件分离。",
                "read_level": "只取得网页正文"}

    r.life_workbench.read = read
    engine = LifeEngine(r.life, script.ask, script.share, lambda pid: "", {"claude": "Claude"},
                        verify=r._life_verify, work=r._life_work)
    assert finish_immediate(engine.step("claude", [], True)) == "browse"
    assert not r.life.interests
    entries = r.life.diary.of("claude")
    assert len(entries) == 1
    assert entries[0].sources[0]["read_level"] == "只取得网页正文"
    assert not r.life.shares
    assert r.life_status()["diary"][0]["member"] == "claude"


def test_forged_artifacts_without_sources_never_become_diary(tmp_path):
    r = runtime(tmp_path)
    script = Script({"action": "explore", "interest": {"title": "手柄", "next_step": "看看视频"}},
                    reading(sources=[], artifacts=[{"id": "fake", "owner": "claude"}]))
    r._life_ask = script.ask
    engine = LifeEngine(r.life, script.ask, script.share, lambda pid: "", {"claude": "Claude"},
                        verify=r._life_verify, work=r._life_work)
    finish_immediate(engine.step("claude", [], True))
    assert not r.life.diary.of("claude")
    assert not r.life.artifacts


def test_work_can_leave_negative_impression_without_advancing_goal(tmp_path):
    r = runtime(tmp_path)
    item = r.life.choose_interest("claude", {"title": "手柄结构", "goal": "弄清楚结构"})
    artifact = r.life_workbench.save("claude", "reading", "手柄", "真实阅读回执", reading()["sources"])
    assert not r.life.record_work(item, reading(outcome="nothing_new", artifacts=[artifact]))
    assert item.progress == "尚未开始了解"
    assert len(r.life.diary.of("claude")) == 1


@pytest.mark.parametrize("kind", ["browse", "explore"])
def test_replayed_verified_receipt_does_not_duplicate_activity_diary(tmp_path, kind):
    r = runtime(tmp_path)
    item = r.life.choose_interest("claude", {"title": "手柄结构", "goal": "弄清楚结构"})
    artifact = r.life_workbench.save("claude", "reading", "手柄", "真实阅读回执", reading()["sources"])
    result = reading(artifacts=[artifact])
    r.life.record_artifacts("claude", result)
    for _ in range(2):
        if kind == "browse":
            r.life.remember_discovery("claude", result)
        else:
            r.life.observe(item, result)
    assert len(r.life.diary.of("claude")) == 1
    assert artifact["id"] in r.life.diary.of("claude")[0].ref


def test_game_diary_preserves_watching_and_follows_corrections_and_hiding(tmp_path):
    r = runtime(tmp_path)
    memory = r.game_memories.add([{"member": "codex", "climb": "run1", "kind": "时刻", "subject": "险胜",
                                  "feeling": "兴奋", "intensity": 0.8, "story": "最后一回合险胜了。", "watched": "Claude"}])[0]
    entry = next(e for e in r.life_status()["diary"] if e["ref"] == memory.id)
    assert "Claude" in entry["thoughts"] and "旁观" in entry["thoughts"]
    r.game_memories.edit(memory.id, "其实还剩两个回合。")
    assert "两个回合" in next(e for e in r.life_status()["diary"] if e["ref"] == memory.id)["thoughts"]
    r.game_memories.set_status(memory.id, "hidden")
    assert not any(e["ref"] == memory.id for e in r.life_status()["diary"])
    r.game_memories.delete(memory.id)
    assert not any(e["ref"] == memory.id for e in r.life_status()["diary"])


def test_video_receipt_requires_content_and_preserves_actual_reading_extent():
    import json
    from types import SimpleNamespace
    from roundtable.video_reading import video_receipt
    url = "https://www.youtube.com/watch?v=example"
    def result(body):
        return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=json.dumps(body))])
    good = video_receipt(result({"status": "ok", "title": "拆机", "content": "先把螺丝拆下来。",
                                "read_level": "视频语音转录前 9 字，未读取画面"}), url)
    assert good["content"] == "先把螺丝拆下来。"
    assert good["read_level"] == "视频语音转录前 9 字，未读取画面"
    assert video_receipt(result({"status": "ok", "title": "没看过的影片", "content": ""}), url)["status"] == "unavailable"
