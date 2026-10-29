import asyncio
import json
from pathlib import Path

import pytest

from roundtable.config import ConfigError
from roundtable.games.social import GameConversation
from roundtable.games.spire import GameService, visible_state
from roundtable.life import LifeStore
from test_games import combat
from test_life import runtime
from test_reply_chain import room_for


def conversation(tmp_path, replies, **kwargs):
    charged, prompts = [], []
    async def ask(pid, prompt):
        prompts.append((pid, prompt))
        return json.dumps(replies.pop(0), ensure_ascii=False)
    social = GameConversation('deepseek', tmp_path / 'run',
        {'deepseek': 'DeepSeek', 'codex': 'Codex', 'grok': 'Grok'}, LifeStore(tmp_path),
        ask, lambda: ['codex'], charged.append, **kwargs)
    return social, charged, prompts


async def test_friends_talk_through_actual_scene_and_keep_a_shared_private_memory(tmp_path):
    social, charged, prompts = conversation(tmp_path, [
        {'message': '先防一下，这只马上要打你。', 'stay': True},
        {'message': '嗯，下回合再打。', 'stay': True}])
    state = visible_state(combat())
    await social.talk('Codex', '来帮我看看，这张该打吗？', state)
    await social.talk('codex', '那我先挡住。', state)
    assert charged == ['codex']
    assert len(social.thread['messages']) == 4 and social.companion == 'codex'
    assert '真实局面' in prompts[0][1] and 'Alive' in prompts[0][1]
    assert '先防一下' in social.context(state)
    loaded = LifeStore(tmp_path)
    for pid in ('deepseek', 'codex'):
        memory = loaded.private_recall(pid, '杀戮尖塔一起玩')
        assert '先防一下' in memory and '实际操作的是 deepseek' in memory
    assert loaded.private_recall('grok', '杀戮尖塔一起玩') == ''
    social.finish({'ended_at': 123, 'stopped_because': 'owner_stopped'})
    assert '共同游玩已经结束' in LifeStore(tmp_path).private_recall('codex', '杀戮尖塔')


async def test_invited_friend_may_decline_and_is_not_repeatedly_pinged(tmp_path):
    social, charged, _ = conversation(tmp_path, [{'message': '现在不想玩，你先玩吧。', 'stay': False}])
    await social.talk('codex', '一起看吗', visible_state(combat()))
    assert social.companion is None and charged == ['codex']
    await social.talk('codex', '再叫一次', visible_state(combat()))
    assert charged == ['codex'] and len(social.thread['messages']) == 2


async def test_companion_can_stay_quiet_and_leaves_when_unavailable(tmp_path):
    present = [True]
    social, _, prompts = conversation(tmp_path, [{'message': '行，我看看。', 'stay': True}, {'message': '', 'stay': True}],
                                      present=lambda _: present[0])
    state = visible_state(combat())
    await social.talk('codex', '一起看吗', state)
    await social.observe(state, 2)
    assert len(prompts) == 2 and len(social.messages) == 2
    await social.observe(state, 4)
    assert len(prompts) == 2  # Unchanged scene does not demand another comment.
    present[0] = False
    await social.observe(state, 6)
    assert social.companion is None and len(prompts) == 2


async def test_a_friends_advice_reaches_the_player_before_the_game_action(tmp_path, monkeypatch):
    actions = []
    social, charged, _ = conversation(tmp_path, [
        {'peer': 'codex', 'message': '这回合先打还是先挡？'},
        {'message': '先出 Defend，再考虑打它。', 'stay': True}])
    class Process:
        def __init__(self, *args): args[-1].mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def act(self, before, action):
            actions.append(action)
            return {'sequence': 2, 'payload': combat()}
        async def close(self): return True
    class Actor:
        async def reply(self, _, prompt):
            assert '先出 Defend' in prompt
            return '{"action":"play 2","say":"那我先挡一下。","reason":"听朋友的，先挡住"}'
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    service = GameService(tmp_path, lambda *_: Actor(), lambda *_: social)
    service.config.update(enabled=True, members=['deepseek'])
    result = await service.play('deepseek', decision_limit=1)
    assert actions == ['play 2'] and result['actions'] == 1
    assert charged == ['codex'] and result['private_messages'] == 2
    assert '那我先挡一下。' not in str(social.messages)  # Action narration is not an automatic chat post.
    assert len(service.events(result)) == 1


@pytest.mark.parametrize('delivered', [True, False])
async def test_sharing_requires_a_real_capture_and_successful_delivery(tmp_path, delivered):
    picture = tmp_path / 'actual.png'; picture.write_bytes(b'\x89PNG\r\n\x1a\nreal-frame')
    posts = []
    async def capture(): return picture
    async def publish(pid, text, image, scene):
        posts.append((pid, text, image, scene))
        return {'at': 100, 'text': text} if delivered else None
    social, _, _ = conversation(tmp_path, [], publish=publish, can_share=lambda: True,
        public_feed=lambda since: [{'speaker': '阿明', 'text': '试试先留那张防御牌'}])
    await social.share('这张牌我有点想拿，你们觉得呢？', visible_state(combat()), capture)
    assert posts[0][2] == picture and '生命 80/80' in posts[0][3]
    assert social.shares == int(delivered) and len(social.store.shares) == int(delivered)
    if delivered:
        assert '先留那张防御牌' in social.context(visible_state(combat()))
        assert social.store.shares[0]['external_to_session'] is True
    else:
        assert social.public == []


async def test_no_capture_or_message_when_sharing_is_disabled(tmp_path):
    async def capture(): pytest.fail('Do not even capture when sharing is disabled')
    async def publish(*args): pytest.fail('No public side effect')
    social, _, _ = conversation(tmp_path, [], publish=publish)
    await social.share('看看这个', visible_state(combat()), capture)
    assert social.store.shares == []


async def test_stop_during_upload_still_records_the_delivered_picture(tmp_path):
    sending, finish = asyncio.Event(), asyncio.Event()
    picture = tmp_path / 'frame.png'; picture.write_bytes(b'frame')
    async def capture(): return picture
    async def publish(*args):
        sending.set(); await finish.wait()
        return {'at': 100, 'text': '这张不错'}
    social, _, _ = conversation(tmp_path, [], publish=publish, can_share=lambda: True)
    task = asyncio.create_task(social.share('这张不错', visible_state(combat()), capture))
    await sending.wait(); task.cancel(); finish.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert social.shares == 1 and len(social.store.shares) == 1


async def test_group_photo_gets_replies_and_actual_image_context(make, store, tmp_path):
    a = make('a', ['是啊，刚才差一点。'])
    b = make('b', ['你还剩多少血？', '[PASS]'])
    room, sink = room_for([a, b], store, 0, 0)
    picture = tmp_path / 'frame.png'; picture.write_bytes(b'frame')
    posted = await room.start_proactive(a, lambda _: '', 1, prepared_text='吓我一跳', discussion=True,
                                      image_path=picture, image_description='实际生命 3/80，第 5 层')
    await room.wait_idle()
    assert posted and sink.photos == [('a', str(picture), '吓我一跳')]
    assert room.messages[0].images == (str(picture),) and room.messages[0].external_to_session
    assert '实际生命 3/80' in b.prompts[0]
    assert [p for p, _ in sink.posts][:3] == ['a', 'b', 'a']


async def test_failed_photo_is_not_remembered_as_a_public_post(make, store, tmp_path):
    a, b = make('a', []), make('b', ['不该接话'])
    room, sink = room_for([a, b], store, 0, 0)
    async def fail(*args, **kwargs): return None
    sink.post_photo = fail
    picture = tmp_path / 'frame.png'; picture.write_bytes(b'frame')
    assert await room.start_proactive(a, lambda _: '', 1, prepared_text='看这个', image_path=picture) is None
    await room.wait_idle()
    assert not room.messages and not b.prompts


def test_live_game_context_does_not_expose_private_chat_to_other_members(tmp_path):
    r = runtime(tmp_path)
    social, _, _ = conversation(tmp_path / 'social', [])
    social.latest_scene = '第 3 层，生命 80/80'
    social.messages = [{'text': '朋友私下说过的话'}]
    r.games.running, r.games.social = 'claude', social
    context = r._game_chat_context('claude', '现在玩得怎么样')
    assert '第 3 层' in context and '朋友私下说过的话' not in context
    assert r._game_chat_context('codex', 'Claude 在玩吗') == ''


async def test_plain_companion_speech_is_preserved_but_never_becomes_a_public_action(tmp_path):
    posts = []
    social, _, _ = conversation(tmp_path, [], publish=lambda *args: posts.append(args))
    async def ask(*args): return '你先看牌吧，我也想看看给了什么。'
    social.ask = ask
    await social.talk('codex', '你会先看牌吗', visible_state(combat()))
    assert social.messages[-1]['text'] == '你先看牌吧，我也想看看给了什么。'
    assert not posts and not social.shares


async def test_player_can_choose_solitude_and_opening_is_not_an_automatic_group_post(tmp_path):
    social, charged, _ = conversation(tmp_path, [{'peer': '', 'message': '', 'share_message': '自动开局报告'}])
    async def capture(): pytest.fail('An opening must not automatically send a picture')
    await social.before_turn(visible_state(combat()), 0, capture)
    assert not charged and not social.messages and not social.shares


async def test_wait_for_reply_receives_real_answer_before_continuing(tmp_path):
    social, charged, prompts = conversation(tmp_path, [
        {'message': '行，一起看。', 'stay': True},
        {'message': '你觉得先打哪只？', 'wait_for_reply': True},
        {'message': '先打右边那只。', 'stay': True}])
    state = visible_state(combat())
    await social.talk('codex', '陪我看一下', state)
    social.opened = True
    async def capture(): pytest.fail('No share was chosen')
    await social.before_turn(state, 1, capture)
    assert social.messages[-1]['text'] == '先打右边那只。' and charged == ['codex']
    assert '你觉得先打哪只' in prompts[-1][1]


def test_preview_conversation_is_not_imported_as_real_group_memory(tmp_path):
    r = runtime(tmp_path)
    r._record_game({'publication_target': 'local_preview'})
    assert not r.life.artifacts and not r.life.shares


async def test_unavailable_recipient_is_not_replaced_with_the_current_companion(tmp_path):
    social, _, _ = conversation(tmp_path, [
        {'message': '一起看。', 'stay': True},
        {'peer': 'grok', 'message': 'Grok，这句话是问你的。'}])
    state = visible_state(combat())
    await social.talk('codex', '一起玩吗', state)
    social.opened = True
    async def capture(): pytest.fail('No share was requested')
    await social.before_turn(state, 2, capture)
    assert len(social.messages) == 2 and social.companion == 'codex'


def test_picture_context_for_text_only_friends_includes_actual_option_effects():
    from roundtable.games.social import scene_note
    state = {'game': {'floor': 5, 'current_hp': 80, 'max_hp': 80, 'gold': 102,
                     'screen_type': 'EVENT', 'screen_state': {'options': [
                         {'text': 'Gain 75 Gold. Lose 11 HP.'}, {'text': 'Lose 40 Gold.'}]}}}
    text = scene_note(state)
    assert 'Gain 75 Gold. Lose 11 HP.' in text and 'Lose 40 Gold.' in text
