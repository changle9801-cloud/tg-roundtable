import json
from datetime import datetime, timedelta

import pytest

from roundtable.config import ConfigError
from roundtable.life import LifeStore
from roundtable.life_work import LifeWorkbench
from roundtable.mcp_servers.reader import html_content
from roundtable.proactive import ProactiveScheduler
from roundtable.runtime import Runtime
from roundtable.settings import normalize_settings
from test_life import MEMBERS, Script, runtime
from test_web import admin, login  # noqa: F401


GOAL = {"title": "神话里的鲸", "why": "喜欢鲸的不同形象", "goal": "写一段自己的鲸故事", "next_step": "写第一小段"}


def test_zero_limits_round_trip_and_do_not_fall_back_to_claude_default():
    data = {"participants": MEMBERS, "life": {"members": ["claude", "codex"],
            "member_days": {"claude": 0}, "passive_member_days": {"claude": 0, "codex": 2}}}
    normalized = normalize_settings(data)
    assert normalized["life"]["member_days"]["claude"] == 0
    assert normalized["life"]["passive_member_days"] == {"claude": 0, "codex": 2}
    assert normalize_settings(normalized) == normalized


def test_passive_and_personal_limits_survive_restart_independently(tmp_path):
    r = runtime(tmp_path)
    p = r._member("claude")
    r.life_schedule.record(r._life_policy(p), "personal")
    left = r._life_left("claude")
    r._life_charge("claude")
    assert r._life_left("claude") == left
    again = Runtime(r.store)
    assert again._life_left("claude") == left
    assert again._life_private_left("claude") == 3
    assert again.life_status()["plans"]["claude"]["passive_today"] == 1


def test_legacy_mixed_counts_migrate_once_without_losing_schedule(tmp_path):
    # Only initialized plans are migrated; the old data remains available.
    r = runtime(tmp_path)
    data = {"claude": {"date": datetime.now().date().isoformat(), "posts": 3, "attempts": 1,
                       "next_at": 1234, "urls": []}}
    (tmp_path / "life_plan.json").write_text(json.dumps(data), encoding="utf-8")
    migrated = Runtime(r.store)
    assert migrated.life_schedule.plans["claude"].posts == 1
    assert migrated.life_private.plans["claude"].posts == 2
    assert migrated.life_schedule.plans["claude"].next_at == 1234
    migrated._life_charge("claude")
    assert Runtime(r.store).life_private.plans["claude"].posts == 3
    assert json.loads((tmp_path / "life_plan.json").read_text())["claude"]["posts"] == 3


def test_unlimited_still_has_spacing_and_respects_activity_hours(tmp_path):
    r = runtime(tmp_path)
    r.config.life.member_days["claude"] = 0
    p = r._life_policy(r._member("claude"))
    p.browse_hours = "17-24"
    clock = [datetime(2026, 10, 28, 19)]
    scheduler = ProactiveScheduler(tmp_path / "schedule.json", now=lambda: clock[0], prefix="browse")
    for _ in range(15):
        scheduler.plan(p).next_at = 0
        assert scheduler.due(p)
        scheduler.record(p, "activity")
    assert not scheduler.due(p)
    assert not scheduler.describe(p)["done_for_today"]
    delay = scheduler.plan(p).next_at - clock[0].timestamp()
    assert 0.7 * 3600 <= delay <= 1.3 * 3600
    clock[0] += timedelta(hours=2)
    assert scheduler.due(p)
    clock[0] = datetime(2026, 10, 29, 3)
    scheduler.plan(p).next_at = 0
    assert not scheduler.due(p)


async def test_exhausted_passive_quota_keeps_personal_work_available(tmp_path):
    r = runtime(tmp_path)
    r.config.life.passive_member_days["claude"] = 1
    r._life_charge("claude")
    with pytest.raises(ConfigError, match="被动私聊"):
        r._life_charge("claude")
    assert r._life_left("claude") == 2
    r.life.choose_interest("codex", GOAL)
    script = Script({"action": "idle"}, {"action": "idle"})
    r._life_ask = script.ask
    r.start_life("claude")
    await r._life_task
    r.start_life("codex")
    await r._life_task
    assert "这次不能私聊" in script.calls[-1][1]
    assert r._life_left("claude") == 1


async def test_self_chosen_goal_writes_file_and_continues_from_it_after_restart(tmp_path):
    r = runtime(tmp_path)
    script = Script({"action": "work", "interest": GOAL, "work": {"operation": "write", "kind": "creative"}},
                    {"title": "一只收藏星星的鲸", "content": "鲸把今天捡到的第一颗星星，藏进了一片安静的海里。", "next_step": "明天写它为什么收藏星星"})
    r._life_ask = script.ask
    r.start_life("claude")
    await r._life_task
    item = r.life.of("claude")[0]
    artifact = r.life.artifacts[0]
    assert "第一颗星星" in r.life_workbench.read_artifact(artifact)
    assert item.goal == GOAL["goal"] and item.artifact_ids == [artifact["id"]]
    assert not r.life.shares
    assert "第一颗星星" not in r.life.prompt_section("codex")
    assert "第一颗星星" in r.life.prompt_section("claude")
    again = Runtime(r.store)
    script = Script({"action": "work", "interest": {"id": item.id}, "work": {"operation": "write", "kind": "creative"}},
                    {"title": "鲸的收藏续篇", "content": "因为海底太暗，它想把路过的星光留给迷路的小鱼。", "goal_status": "done"})
    again._life_ask = script.ask
    again.start_life("claude")
    await again._life_task
    assert "第一颗星星" in script.calls[-1][1]
    assert again.life.of("claude")[0].status == "done"
    assert len(again.life.artifacts) == 2


async def test_no_goal_private_chat_is_allowed_without_turning_it_into_work(tmp_path):
    r = runtime(tmp_path)
    script = Script({"action": "private", "peer": "codex", "message": "今天看了什么"},
                    {"message": "还没看到有意思的", "done": True})
    r._life_ask = script.ask
    r.start_life("claude")
    await r._life_task
    assert len(r.life.threads) == 1
    assert not r.life.artifacts and not r.life.interests
    assert r.life.threads[0]["messages"][-1]["text"] == "还没看到有意思的"


async def test_forged_model_summary_is_replaced_with_actual_reading_evidence(tmp_path):
    script = Script({"outcome": "progress", "progress": "从回执只读到了鲸的文字", "title": "鲸",
                     "sources": [{"url": "https://invented.invalid", "read_level": "看完视频"}],
                     "artifacts": [{"id": "fake"}]})
    seen = []
    async def read(url):
        seen.append(url)
        return {"status": "ok", "url": url, "title": "鲸", "content": "这是一段实际取得的鲸的文字介绍。",
                "read_level": "网页静态文字，不含视频"}
    workbench = LifeWorkbench(tmp_path, script.ask, read)
    result = await workbench.research("claude", {"outcome": "progress", "progress": "我已经看完视频了",
         "sources": [{"url": "https://example.com/whale", "read_level": "看完视频"}]})
    assert seen == ["https://example.com/whale"]
    assert result["sources"][0]["read_level"] == "网页静态文字，不含视频"
    assert result["artifacts"][0]["id"] != "fake"
    assert "实际取得的鲸的文字" in script.calls[0][1]
    assert "已经看完视频" not in script.calls[0][1]
    assert "实际取得" in workbench.read_artifact(result["artifacts"][0])


async def test_read_failure_does_not_create_progress_or_success_artifacts(tmp_path):
    r = runtime(tmp_path)
    script = Script({"action": "work", "interest": GOAL,
                     "work": {"operation": "read", "urls": ["https://example.com/missing"]}})
    r._life_ask = script.ask
    async def unavailable(url): return {"status": "unavailable", "reason": "HTTP 403"}
    r.life_workbench.read = unavailable
    r.start_life("claude")
    await r._life_task
    assert not r.life.artifacts
    assert r.life.of("claude")[0].progress == "尚未开始了解"
    assert r.life.events[-1]["action"] == "blocked"


async def test_collection_rejects_other_members_evidence_and_empty_creation(tmp_path):
    workbench = LifeWorkbench(tmp_path, Script({"content": ""}).ask)
    item = LifeStore(tmp_path).choose_interest("claude", GOAL)
    with pytest.raises(ConfigError, match="自己的|自己已有"):
        await workbench.work("claude", item, {"operation": "write", "kind": "collection",
                              "source_urls": ["https://example.com/unknown"]}, "", {})
    with pytest.raises(ConfigError, match="正文"):
        await workbench.work("claude", item, {"operation": "write"}, "", {})
    assert not list(tmp_path.rglob("*.md"))


def test_cannot_mark_goal_done_without_receipts_or_follow_untrusted_file_paths(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("claude", GOAL)
    with pytest.raises(ConfigError, match="执行成果"):
        store.choose_interest("claude", {"id": item.id, "status": "done"})
    workbench = LifeWorkbench(tmp_path, None)
    artifact = workbench.save("claude", "note", "我的笔记", "原来的文字")
    with pytest.raises(ConfigError):
        workbench.read_artifact({**artifact, "file": "../../settings.json"})
    (workbench.root / artifact["file"]).write_text("改过的文字", encoding="utf-8")
    with pytest.raises(ConfigError, match="已变化"):
        workbench.read_artifact(artifact)


async def test_artifact_endpoint_requires_auth_and_checks_file_hash(admin):
    artifact = admin.runtime.life_workbench.save("claude", "creative", "私有作品", "只有管理员能打开的作品")
    admin.runtime.life.record_artifacts("claude", {"artifacts": [artifact]})
    url = "/api/life/artifacts/" + artifact["id"]
    assert (await admin.get(url)).status == 401
    await login(admin)
    response = await admin.get(url)
    assert response.status == 200 and response.content_type == "text/plain"
    assert "只有管理员" in await response.text()
    (admin.runtime.life_workbench.root / artifact["file"]).write_text("changed")
    assert (await admin.get(url)).status == 400


def test_goals_are_reviewed_instead_of_forced_to_stop_after_three_steps(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("claude", GOAL)
    bench = LifeWorkbench(tmp_path, None)
    for index in range(3):
        receipt = bench.save("claude", "creative", f"篇{index}", "实际保存的故事")
        store.record_work(item, {"outcome": "progress", "progress": f"写完篇{index}", "artifacts": [receipt]})
    assert item.status == "active" and item.review_due
    assert "重新想想" in store.prompt_section("claude")


def test_no_progress_keeps_old_progress_but_remembers_the_actual_obstacle(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("claude", GOAL)
    artifact = LifeWorkbench(tmp_path, None).save("claude", "reading", "原文", "没有目标段落")
    result = {"outcome": "nothing_new", "progress": "这页没有说明释放按钮", "next_step": "换看访谈第三部分",
              "artifacts": [artifact]}
    store.record_artifacts("claude", result, item)
    assert not store.record_work(item, result)
    again = LifeStore(tmp_path)
    assert again.of("claude")[0].progress == "尚未开始了解"
    assert again.of("claude")[0].next_step == "换看访谈第三部分"
    assert "没有说明释放按钮" in again.events[-1]["note"]


async def test_writing_from_own_legacy_sighting_reads_it_first(tmp_path):
    r = runtime(tmp_path)
    item = r.life.choose_interest("claude", GOAL)
    item.sources = [{"url": "https://example.com/whale", "excerpt": "旧的自报摘要"}]
    r.life.save()
    read_urls = []
    async def read(url):
        read_urls.append(url)
        return {"status": "ok", "url": url, "requested_url": url,
                "content": "实际网页只写了鲸的迁徙。", "read_level": "网页文字", "title": "鲸"}
    r.life_workbench.read = read
    script = Script({"outcome": "progress", "progress": "读到了鲸的迁徙", "title": "鲸"},
                    {"title": "读鲸", "content": "# 读鲸\n\n我读到的是鲸的迁徙，还想继续了解它怎么辨认方向。"})
    r._life_ask = script.ask
    result = await r._life_work("claude", item, {"operation": "write", "kind": "note",
                                               "source_urls": ["https://example.com/whale"]})
    assert read_urls == ["https://example.com/whale"]
    assert "实际网页只写了鲸的迁徙" in script.calls[-1][1]
    assert "旧的自报摘要" not in script.calls[-1][1]
    assert r.life.artifacts[0]["kind"] == "reading"
    assert r.life.record_work(item, result)
    content = r.life_workbench.read_artifact(result["artifacts"][0])
    assert content.count("# 读鲸") == 1
    assert len(item.artifact_ids) == 2


def test_long_article_focus_reads_later_passage_instead_of_repeating_the_opening():
    html = '<article>' + ('Opening about the screen. ' * 500) + '<p>The release button pushes the magnets apart.</p>' + ('More text. ' * 50) + '</article>'
    normal = html_content(html, 'https://example.com', 600)
    focused = html_content(html, 'https://example.com', 600, 'release magnet')
    assert 'release button' not in normal['content']
    assert 'release button' in focused['content'] and focused['start_char'] > 10000
    assert focused['focus_found'] and focused['truncated']
    missing = html_content(html, 'https://example.com', 600, 'unavailable-topic')
    assert not missing['focus_found'] and '未找到' in missing['read_level']
    with_early_release = html.replace('<article>', '<article>Release announcement. ')
    assert html_content(with_early_release, 'https://example.com', 600, 'magnet release')['start_char'] > 10000


async def test_goal_read_passes_focus_to_real_executor(tmp_path):
    calls=[]
    async def read(url, focus=''):
        calls.append((url,focus))
        return {'status':'ok','url':url,'content':'The release button is described here.','read_level':'关键词附近的正文片段'}
    script=Script({'outcome':'progress','progress':'找到了释放按钮的段落'})
    bench=LifeWorkbench(tmp_path,script.ask,read)
    item=LifeStore(tmp_path).choose_interest('claude',GOAL)
    result=await bench.work('claude',item,{'operation':'read','urls':['https://example.com'],'focus':'release'},'',{})
    assert calls==[('https://example.com','release')]
    assert result['sources'][0]['read_level']=='关键词附近的正文片段'
