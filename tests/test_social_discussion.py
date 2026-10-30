import random
import asyncio

import pytest

from test_reply_chain import room_for
from nookling.moderator import RuleModerator
from nookling.transcript import Message


def test_public_interests_increase_selection_without_overriding_mentions(make):
    gamer, cook = make('gamer', []), make('cook', [])
    gamer.cfg.browse_topics = '游戏、硬件'
    cook.cfg.browse_topics = '美食、烘焙'
    moderator = RuleModerator(random.Random(10))
    message = Message('friend', 'Friend', '这个游戏的视频真有趣')
    picks = [moderator.pick([gamer, cook], message, 'friend').id for _ in range(500)]
    assert picks.count('gamer') > 340
    message = Message('friend', 'Friend', message.text, ('cook',))
    assert moderator.pick([gamer, cook], message, 'friend') is cook


async def test_discussion_keeps_the_first_pair_talking_with_a_bound(make, store):
    a = make('a', ['我喜欢透明的', '不应再发'])
    b = make('b', ['我更喜欢黑色', '透明的容易看见灰', '不应再发'])
    room, sink = room_for([a, b], store, 0, 0)
    room.start_proactive(a, lambda _: '', 1, prepared_text='看看这个游戏机', discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ['a', 'b', 'a', 'b']
    assert '这段分享可以停在这里' not in b.prompts[0]
    assert '这段分享可以停在这里' in a.prompts[0]
    assert '这段分享可以停在这里' in b.prompts[1]


async def test_author_can_end_after_acknowledgment_without_another_peer_turn(make, store):
    a = make('a', ['[PASS]'])
    b = make('b', ['嗯，明白了，我改一下', '不应再发'])
    room, sink = room_for([a, b], store, 0, 0)
    await room.start_proactive(a, lambda _: '', 1, prepared_text='刚才我把两个故事讲串了', discussion=True)
    await room.wait_idle()
    assert [pid for pid, _ in sink.posts] == ['a', 'b']
    assert len(b.prompts) == 1


@pytest.mark.parametrize('reply', ['[PASS]', ''])
async def test_discussion_stops_when_the_responder_has_nothing_to_add(make, store, reply):
    a, b = make('a', ['不应再发']), make('b', [reply])
    room, sink = room_for([a, b], store, 0, 0)
    room.start_proactive(a, lambda _: '', 1, prepared_text='看看这个', discussion=True)
    await room.wait_idle()
    assert len(sink.posts) == 1


async def test_user_interruption_replaces_the_pair_exchange(make, store):
    a, b = make('a', ['我接用户的话']), make('b', ['我喜欢这个'], delay=0.03)
    room, sink = room_for([a, b], store, 0, 0)
    await room.start_proactive(a, lambda _: '', 1, prepared_text='一个新视频', discussion=True)
    while not b.prompts:
        await asyncio.sleep(0)
    room.add_user_message('me', '@A 先问你件别的事')
    await room.wait_idle()
    assert len(a.prompts) == 1 and len(b.prompts) == 1
    assert '先问你件别的事' in a.prompts[0]


async def test_failed_reply_ends_the_discussion(make, store):
    from nookling.participants import ParticipantError
    a, b = make('a', ['不应再发']), make('b', [ParticipantError('failed')])
    room, sink = room_for([a, b], store, 0, 0)
    await room.start_proactive(a, lambda _: '', 1, prepared_text='一个新视频', discussion=True)
    await room.wait_idle()
    assert len(sink.posts) == 1 and not a.prompts
