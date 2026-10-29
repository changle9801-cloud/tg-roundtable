import asyncio
import random
import time

from conftest import FakeSink
from roundtable.config import RoundConfig
from roundtable.moderator import RuleModerator
from roundtable.participants import ParticipantError
from roundtable.room import ChatRoom
from roundtable.transcript import Message, find_recheck_targets


def make_room(make, store, *, delay=0, chance=0, max_turns=1, claude_reply='是我讲的，刚才没想起来。'):
    claude = make('claude', [claude_reply], aliases=['学姐'], activity='low', delay=delay)
    grok = make('grok', ['我来围观。'], activity='high')
    deepseek = make('deepseek', ['原来是这样。'])
    sink = FakeSink()
    cfg = RoundConfig(max_turns_per_model=max_turns, call_timeout=5, slow_after=0.005,
                      max_parallel=2, follow_up_chance=chance, follow_up_decay=1)
    room = ChatRoom('chat', [grok, deepseek, claude], sink, cfg, store, RuleModerator(random.Random(2)))
    now = time.time()
    room.messages.extend([
        Message('claude', 'Claude', '我没印象讲过，是不是我讲的，我不认。', ts=now - 1600),
        Message('deepseek', 'DeepSeek', '是在私聊里呀，你跟我讲的。', ts=now - 1590),
        Message('grok', 'Grok', '这瓜我吃了。', ts=now - 1500),
        Message('deepseek', 'DeepSeek', '明明记了东西还被说不认账。', ts=now - 1467),
    ])
    return room, sink, claude, grok, deepseek


async def test_corrected_subject_goes_before_the_person_being_reassured(make, store):
    room, sink, claude, grok, ds = make_room(make, store)
    room.add_user_message('阿明', '不是 DeepSeek 记错了，是 Claude 刚才忘了')
    await room.wait_idle()
    assert sink.posts[0][0] == 'claude'
    assert '核对' in claude.prompts[0]


async def test_actual_nickname_correction_is_not_a_random_followup(make, store):
    room, sink, claude, grok, ds = make_room(make, store)
    room.add_user_message('阿明', '不是你记错了，是你学姐刚才忘了')
    await room.wait_idle()
    assert sink.posts == [('claude', '是我讲的，刚才没想起来。')]
    assert '核对' in claude.prompts[0] and not grok.prompts


async def test_bystanders_wait_for_the_correction_subject_even_if_slow(make, store):
    room, sink, claude, grok, ds = make_room(make, store, delay=0.08, chance=1)
    room.add_user_message('阿明', '不是你记错了，是你学姐刚才忘了')
    await room.wait_idle()
    assert sink.posts[0][0] == 'claude'
    assert all('是我讲的' in p.prompts[0] for p in (grok, ds) if p.prompts)


async def test_explicit_other_addressee_still_takes_precedence(make, store):
    room, sink, claude, grok, ds = make_room(make, store)
    room.add_user_message('阿明', '@Grok 不是 DeepSeek 记错了，是 Claude 刚才忘了，你怎么看？')
    await room.wait_idle()
    assert sink.posts[0][0] == 'grok'


async def test_correction_pass_and_failure_do_not_loop_or_block_everyone(make, store):
    for reply in ('[PASS]', ParticipantError('failed')):
        room, sink, claude, grok, ds = make_room(make, store, chance=1, claude_reply=reply)
        room.add_user_message('阿明', '是学姐刚才记错了')
        await asyncio.wait_for(room.wait_idle(), 2)
        assert len(claude.prompts) == 1
        assert grok.prompts or ds.prompts


def test_routing_is_conservative_and_explicit_addresses_win(make, store):
    room, _, claude, grok, ds = make_room(make, store)
    route = lambda text, **kw: find_recheck_targets(text, room.participants, room.messages, now=time.time(), **kw)
    assert route('不是你记错了是你学姐刚才忘了') == ['claude']
    assert route('学姐，你刚才记错了') == ['claude']
    assert route('@Claude 你刚才记错了') == ['claude']
    assert route('@Grok 不是 DeepSeek 记错了，是 Claude 刚才忘了') == []
    assert route('Grok，你看看，学姐刚才忘了') == []
    assert route('不是学姐忘了，是我忘了') == []
    assert route('学姐没有记错') == []
    assert route('我忘了问学姐') == []
    assert route('她说“学姐忘了”，你怎么看？') == []
    assert route('不是你记错了，是学姐忘了', reply_to='grok') == []
    grok.aliases.append('学姐')
    assert route('学姐忘了') == []


def test_you_correction_uses_the_recent_actual_speaker_and_metadata_survives_reload(make, store):
    room, _, claude, grok, ds = make_room(make, store)
    now = time.time()
    room.messages.append(Message('claude', 'Claude', '我没印象。', ts=now - 10))
    assert find_recheck_targets('你刚才记错了', room.participants, room.messages, now=now) == ['claude']
    assert find_recheck_targets('你刚才记错了', room.participants, room.messages, now=now + 1000) == []
    event = Message('user', '阿明', '学姐忘了', recheck_targets=('claude',))
    assert Message.from_json(event.to_json()).recheck_targets == ('claude',)
    assert Message.from_json({'speaker_id':'user','speaker_name':'阿明','text':'你好'}).recheck_targets == ()


async def test_new_correction_gets_its_own_chance_while_old_answer_is_inflight(make, store):
    room, sink, claude, grok, ds = make_room(make, store, delay=0.08)
    claude.replies = ['刚才的回答。', '我核对过了，确实说过。']
    room.add_user_message('阿明', '@Claude 你先说一句')
    await asyncio.sleep(0.01)
    room.add_user_message('阿明', '学姐刚才记错了')
    await asyncio.wait_for(room.wait_idle(), 2)
    assert [text for pid,text in sink.posts if pid=='claude'] == ['刚才的回答。', '我核对过了，确实说过。']
    assert '核对' in claude.prompts[-1]
    assert not grok.prompts


async def test_user_switching_to_someone_else_releases_the_recheck_wait(make, store):
    room, sink, claude, grok, ds = make_room(make, store, delay=0.1)
    room.add_user_message('阿明', '学姐刚才记错了')
    await asyncio.sleep(0.01)
    room.add_user_message('阿明', '@Grok 先说你那边的事')
    await asyncio.wait_for(room.wait_idle(), 2)
    assert sink.posts[0][0]=='grok'
