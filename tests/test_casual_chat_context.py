from roundtable.life_work import LifeWorkbench
from test_life import runtime


def test_writing_body_is_not_a_default_chat_voice_sample(tmp_path):
    r = runtime(tmp_path)
    artifact = LifeWorkbench(tmp_path, None).save('claude', 'note', '海怪小笔记',
                                                '# 我的正式分析\n\n专属正文：首先分析，再进行三点总结。')
    r.life.record_artifacts('claude', {'artifacts': [artifact]})
    r.refresh_prompts()
    actor = r._member('claude')
    assert '海怪小笔记' in actor.system_prompt
    assert '专属正文' not in actor.system_prompt
    assert '专属正文' not in actor.with_turn_context('今天什么也不想干，聊两句吧')
    assert '专属正文' not in actor.with_turn_context('你在忙啥呢')
    assert '专属正文' not in actor.with_turn_context('那我笔记得改一下，我还当是一个故事呢')
    assert '专属正文' in actor.with_turn_context('你的笔记写了什么？')
    assert '专属正文' in actor.with_turn_context('你写的海怪小笔记里有什么？')
    # Personal work still gets its full writing context and can continue it.
    assert '专属正文' in r.life.prompt_section('claude')
    assert '专属正文' in r.system_prompt_for(actor.cfg, background=True)


def test_artifact_recall_stays_with_its_author(tmp_path):
    r = runtime(tmp_path)
    artifact = LifeWorkbench(tmp_path, None).save('claude', 'creative', '秘密故事', '只有作者知道的故事正文')
    r.life.record_artifacts('claude', {'artifacts': [artifact]})
    assert r.life.artifact_recall('codex', '给我看看秘密故事这篇作品') == ''
    assert '只有作者知道' in r.life.artifact_recall('claude', '你写的秘密故事')


def test_current_voice_reaches_old_member_without_resetting_session(tmp_path):
    r = runtime(tmp_path)
    actor = r._member('claude')
    r.sessions.set('public', 'claude', 'existing-session')
    actor.cfg.style = '新的当前口吻：随口接话。'
    actor.cfg.relationships = '现在把对方当平常聊得来的朋友。'
    actor.cfg.examples = '新的示例：我更喜欢黑色的。'
    sent = actor.with_turn_context('聊两句？')
    assert '新的当前口吻' in sent and '不是这次要模仿的台词' in sent
    assert '平常聊得来的朋友' in sent
    assert '新的示例' in sent
    assert r.sessions.get('public', 'claude') == 'existing-session'


def test_later_progress_reaches_resumed_chat_without_using_old_draft_as_current_fact(tmp_path):
    r = runtime(tmp_path)
    item = r.life.choose_interest('claude', {'title': 'Hafgufa 海怪故事', 'why': '想核对来源'})
    artifact = LifeWorkbench(tmp_path, None).save('claude', 'note', '海怪小笔记', '旧稿：还没查证过')
    r.life.record_artifacts('claude', {'artifacts': [artifact]}, item)
    r.life.observe(item, {'outcome': 'progress', 'progress': '后来读了资料，生火情节不能算在 Hafgufa 底下。',
                          'sources': [{'url': 'https://example.com/whale', 'read_level': '读了介绍段落'}]})
    r.sessions.set('public', 'claude', 'resumed')
    actual = r._member('claude').with_turn_context('那我笔记得改一下，生火那条不能放在 Hafgufa 底下了')
    assert '旧稿：还没查证过' not in actual
    assert '后来读了资料' in actual and '读了介绍段落' in actual
    assert r.life.progress_recall('codex', 'Hafgufa 是什么') == ''
    assert r.life.progress_recall('claude', '今天晚饭吃什么') == ''
    requested = r._member('claude').with_turn_context('你的海怪小笔记写了什么？')
    assert '旧稿：还没查证过' in requested and '后来读了资料' in requested
    assert r.sessions.get('public', 'claude') == 'resumed'
