"""Daily life produces casual discoveries and scoped conversations, not a task treadmill."""

import json

from test_life import IDEA, Script, observation, runtime

from nookling.life import LifeStore


VIDEO = {"url": "https://example.com/game-video", "title": "游戏机改装视频",
         "read_level": "字幕和三张画面，未读评论", "excerpt": "有人把旧掌机装进了透明外壳。"}


async def test_casual_browse_keeps_a_discovery_without_creating_an_interest(tmp_path):
    script = Script({"action": "browse", "interest": IDEA},
                    observation(title="透明掌机", sources=[VIDEO], progress="想把这个给喜欢游戏的朋友看"))
    store = LifeStore(tmp_path)
    engine = script.engine(store)
    found = []
    engine.found = lambda pid, item, result: found.append((pid, item, result["title"]))

    assert await engine.step("codex", "claude", True) == "browse"
    restored = LifeStore(tmp_path)
    assert restored.interests == []
    assert "透明掌机" in restored.prompt_section("codex")
    assert VIDEO["url"] not in restored.prompt_section("claude")
    assert found == [("codex", None, "透明掌机")]
    assert not script.public and not restored.threads


async def test_casual_discovery_can_be_shared_later_without_private_notes(tmp_path):
    store = LifeStore(tmp_path)
    browse = Script({"action": "browse"}, observation(title="透明掌机", sources=[VIDEO],
                                                     progress="私人想法：想起昨天那段私聊"))
    await browse.engine(store).step("codex", "claude", True)
    shared = []

    async def share(pid, message, sources=()):
        shared.append((pid, message, sources))
        return message

    script = Script({"action": "share", "message": "这个外壳你会选吗", "peer": "claude",
                     "share_urls": [VIDEO["url"]]})
    engine = script.engine(LifeStore(tmp_path))
    engine.share = share
    await engine.step("codex", "claude", True)
    assert len(shared) == 1
    assert "@Claude" in shared[0][1]
    assert shared[0][2] == [VIDEO]
    assert "私人想法" not in json.dumps(shared, ensure_ascii=False)


async def test_a_failed_casual_browse_never_becomes_a_discovery_or_share(tmp_path):
    script = Script({"action": "browse"}, observation(sources=[], follow_up="share", message="我看过了"))
    store = LifeStore(tmp_path)
    assert await script.engine(store).step("codex", "claude", True) == "browse"
    assert not store.interests and not store.discoveries_of("codex")
    assert not script.public


def test_repeated_exploration_pauses_but_explicit_resumption_keeps_the_history(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("codex", IDEA)
    for n in range(3):
        assert store.observe(item, observation(progress=f"第{n + 1}次进展"))
    assert item.status == "paused"
    restored = LifeStore(tmp_path)
    same = restored.choose_interest("codex", {"id": item.id, "status": "active"})
    assert same.progress == "第3次进展"
    assert restored.observe(same, observation(progress="有新线索了"))
    assert same.status == "active"


async def test_explicit_source_selection_cannot_hide_an_unread_link_in_message(tmp_path):
    store = LifeStore(tmp_path)
    item = store.choose_interest("codex", IDEA)
    store.observe(item, observation(sources=[VIDEO]))
    script = Script({"action": "share", "message": "看这个 https://example.com/not-read",
                     "share_urls": [VIDEO["url"]]})
    await script.engine(store).step("codex", "claude", True)
    assert script.public == [] and store.shares == []
    assert store.events[-1]["action"] == "deferred"


async def test_runtime_casual_discoveries_enter_the_existing_journal(tmp_path):
    r = runtime(tmp_path)
    r._member("codex").cfg.web_search = True
    r.life.choose_interest("codex", {"title": "自己的掌机笔记", "goal": "写一份掌机设计随想"})
    script = Script({"action": "browse"}, observation(title="透明掌机", sources=[VIDEO]),
                    observation(title="透明掌机", sources=[VIDEO]))
    async def read(url):
        return {"status": "ok", "url": url, "title": "透明掌机", "content": "实际取得的文字介绍：透明外壳。", "read_level": "文字介绍"}
    r.life_workbench.read = read
    r._life_ask = script.ask
    r.start_life("codex")
    await r._life_task
    assert r.journal.of("codex")[0].title == "透明掌机"
    assert len(r.life.of("codex")) == 1  # casual browsing does not add a new goal
    assert r.life.artifacts[0]["kind"] == "reading"


async def test_private_help_reads_once_and_shares_evidence_only_with_the_requester(tmp_path):
    script = Script(
        {"action": "private", "peer": "codex", "message": "这个掌机视频能帮我看看吗"},
        {"lookup": "打开这个掌机视频看外壳", "message": "", "done": False},
        observation(title="透明掌机", sources=[VIDEO], message="外壳是透明的，按键没换"),
        {"message": "那我喜欢这个壳", "done": False},
        {"lookup": "再查查评论", "message": "评论我没读到", "done": True},
    )
    store = LifeStore(tmp_path)
    engine = script.engine(store)
    engine.can_browse = lambda pid: pid == "codex"
    charged = []
    engine.charge = charged.append
    await engine.step("claude", "codex", False)

    assert len(script.calls) == 5  # one decision, three replies, one actual lookup
    assert len(store.threads[0]["messages"]) == 4
    assert store.threads[0]["messages"][1]["sources"] == [VIDEO]
    assert VIDEO["read_level"] in script.calls[3][1]
    assert VIDEO["url"] in LifeStore(tmp_path).prompt_section("claude")
    assert VIDEO["url"] not in store.prompt_section("grok")
    assert store.discoveries_of("claude") == []  # hearing a friend is not reading it yourself
    assert len(store.discoveries_of("codex")) == 1
    assert charged == ["codex"] and script.public == []


async def test_a_toolless_private_friend_cannot_execute_a_lookup(tmp_path):
    script = Script({"action": "private", "message": "帮我看看这个视频"},
                    {"lookup": "打开视频", "message": "我看完了", "done": True})
    store = LifeStore(tmp_path)
    await script.engine(store).step("claude", "codex", False)
    reply = store.threads[0]["messages"][-1]
    assert len(script.calls) == 2
    assert reply["text"] != "我看完了" and not reply.get("sources")
    assert not store.discoveries_of("codex")


async def test_private_lookup_failure_does_not_claim_success_or_publish(tmp_path):
    script = Script({"action": "private", "message": "帮我看看这个视频"},
                    {"lookup": "打开视频", "done": True},
                    observation(sources=[], outcome="blocked", message="看完了，非常好玩", note="视频打不开"))
    store = LifeStore(tmp_path)
    engine = script.engine(store)
    engine.can_browse = lambda pid: True
    await engine.step("claude", "codex", False)
    reply = store.threads[0]["messages"][-1]
    assert len(script.calls) == 3 and len(store.threads[0]["messages"]) == 2
    assert "看完了" not in reply["text"] and not reply.get("sources")
    assert not store.discoveries_of("codex") and script.public == []


def test_background_context_includes_public_interests_but_not_friends_private_memory(tmp_path):
    r = runtime(tmp_path)
    r._member("codex").cfg.browse_topics = "游戏、硬件"
    secret = r.life.choose_interest("codex", {**IDEA, "title": "不想公开的维修经历"})
    r.life.observe(secret, observation(progress="只有我知道的细节"))
    context = r._life_context("claude")
    assert "Codex" in context and "游戏、硬件" in context
    assert "不想公开的维修经历" not in context and "只有我知道的细节" not in context
