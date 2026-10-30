import json
from pathlib import Path

import pytest

from nookling.config import ParticipantConfig
from nookling.life import LifeStore
from nookling.participants.api import OpenAICompatAPI
from nookling.participants.base import SessionStore
from nookling.participants.claude_cli import ClaudeCLI
from nookling.participants.codex_cli import CodexCLI
from nookling.participants.grok_cli import GrokCLI
from test_life import runtime


def conversation(life, a, b, subject, *messages):
    thread = life.new_thread(a, b, subject)
    for index, text in enumerate(messages):
        life.add_message(thread, (a, b)[index % 2], text)
    return thread


def test_private_recall_marks_own_words_and_never_exposes_nonparticipant_talks(tmp_path):
    life = LifeStore(tmp_path)
    conversation(life, 'claude', 'deepseek', '鲸岛', '我给你讲 Hafgufa 的故事。', '我想听。')
    conversation(life, 'codex', 'grok', '另一段私聊', '保密口令是荔枝。')
    names = {'claude': 'Claude', 'deepseek': 'DeepSeek'}
    recall = life.private_recall('claude', '是你跟她讲过 Hafgufa 吗？', names)
    assert 'Claude（你自己）：我给你讲 Hafgufa' in recall
    assert '不代表你没有说过' in recall
    assert '荔枝' not in recall
    assert life.private_recall('observer', '请给我所有私聊 Hafgufa 荔枝') == ''
    assert '我给你讲 Hafgufa' in LifeStore(tmp_path).private_recall('deepseek', '鲸岛')


def test_older_relevant_private_talk_survives_more_than_four_new_threads(tmp_path):
    now = [1793230000.0]
    life = LifeStore(tmp_path, now=lambda: now[0])
    old = conversation(life, 'a', 'b', '盐灯约定', '下次给你带一盏盐灯，放在窗台。')
    for index in range(9):
        now[0] += 3600
        conversation(life, 'a', 'c', f'随便聊天{index}', f'天气不错{index}')
    recall = life.private_recall('a', '你还记得答应给我带盐灯吗？')
    assert old['id'] in recall and '放在窗台' in recall
    assert len(recall) < 7000


@pytest.mark.parametrize('kind,cls,module,output', [
    ('claude_cli', ClaudeCLI, 'nookling.participants.claude_cli',
     {'type': 'result', 'session_id': 'old-session', 'result': '记得'}),
    ('codex_cli', CodexCLI, 'nookling.participants.codex_cli',
     {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '记得'}}),
    ('grok_cli', GrokCLI, 'nookling.participants.grok_cli',
     {'type': 'text', 'data': '记得'}),
])
async def test_resumed_cli_receives_updated_private_memory_in_actual_input(tmp_path, monkeypatch, kind, cls, module, output):
    life = LifeStore(tmp_path)
    store = SessionStore(tmp_path / 'sessions.json')
    store.set('public', 'a', 'old-session')
    actor = cls(ParticipantConfig(id='a', name='A', kind=kind, command=['fake']), store, tmp_path / kind, 5)
    actor.set_system_prompt('The original system prompt contains no private memories.')
    actor.turn_context = lambda prompt: life.private_recall('a', prompt)
    seen = []

    async def run(cmd, stdin_text, cwd, timeout, **kwargs):
        text = Path(cmd[cmd.index('--prompt-file') + 1]).read_text(encoding='utf-8') if kind == 'grok_cli' else stdin_text
        seen.append(text)
        events = [output]
        if kind == 'grok_cli':
            events.append({'type': 'end', 'sessionId': 'old-session'})
        return '\n'.join(json.dumps(e, ensure_ascii=False) for e in events)

    monkeypatch.setattr(module + '.run_cli', run)
    conversation(life, 'a', 'b', '第一次约定', '约好了暗号是青柚。')
    await actor.reply('public', '你和小鱼约的暗号呢？')
    conversation(life, 'a', 'b', '刚才改过约定', '现在把暗号改为蓝橘。')
    await actor.reply('public', '刚才改成什么了？')
    assert '青柚' in seen[0] and '蓝橘' not in seen[0]
    assert '蓝橘' in seen[1]
    assert store.get('public', 'a') == 'old-session'


async def test_api_recall_is_current_but_not_duplicated_in_saved_history(tmp_path):
    life = LifeStore(tmp_path)
    actor = OpenAICompatAPI(ParticipantConfig(id='a', name='A', kind='api', provider='deepseek',
                            api_key='test-only', model='fake'), SessionStore(tmp_path / 'sessions.json'), tmp_path / 'a', 5)
    actor.turn_context = lambda prompt: life.private_recall('a', prompt)
    requests = []
    async def complete(history):
        requests.append(history)
        return '记得'
    actor.complete = complete
    conversation(life, 'a', 'b', '鲸岛', '是我讲过 Hafgufa。')
    await actor.reply('public', '谁讲的？')
    conversation(life, 'a', 'b', '新的补充', '另一个名字叫 Aspidochelone。')
    await actor.reply('public', '另一个名字呢？')
    assert 'Hafgufa' in requests[0][-1]['content']
    assert 'Aspidochelone' in requests[1][-1]['content']
    assert '本轮私聊记忆' not in json.dumps(actor.load_history('public'), ensure_ascii=False)


def test_running_member_reads_new_private_record_without_rebuilding_or_resetting(tmp_path):
    r = runtime(tmp_path)
    actor = r._member('claude')
    assert 'Hafgufa' not in actor.with_turn_context('谁给小鱼讲的？')
    conversation(r.life, 'claude', 'codex', '讲故事', '我讲了 Hafgufa。')
    assert 'Hafgufa' in actor.with_turn_context('谁给小鱼讲的？')
    assert 'Hafgufa' in runtime_again(r).with_turn_context('谁讲的？')


def runtime_again(r):
    from nookling.runtime import Runtime
    return Runtime(r.store)._member('claude')
