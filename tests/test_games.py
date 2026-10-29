import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from roundtable.config import ConfigError
from roundtable.games.spire import GameService, SpireProcess, exclusive_game, legal_actions, normalize_permissions, visible_state
from roundtable.games.spire_bridge import command_for, atomic_json
from roundtable.life import LifeEngine, LifeStore
from test_life import runtime
from test_web import admin, login  # noqa: F401


def combat():
    return {'ready_for_command': True, 'in_game': True, 'available_commands': ['play', 'end', 'click', 'key', 'state'],
            'game_state': {'seed': 123, 'floor': 1, 'act': 1, 'current_hp': 80, 'max_hp': 80, 'gold': 99,
                'room_phase': 'COMBAT', 'screen_type': 'NONE', 'combat_state': {
                    'draw_pile': [{'uuid': 'hidden-next-card'}], 'hand': [
                        {'name': 'Strike', 'is_playable': True, 'has_target': True},
                        {'name': 'Defend', 'is_playable': True, 'has_target': False},
                        {'name': 'Too expensive', 'is_playable': False, 'has_target': False}],
                    'monsters': [{'name': 'Alive', 'current_hp': 6, 'move_id': 4},
                                 {'name': 'Dead', 'current_hp': 0, 'is_gone': True}]}}}


def test_actions_are_enumerated_and_never_expose_desktop_or_cheat_commands():
    actions = {a['id'] for a in legal_actions(combat())}
    assert actions == {'play 1 0', 'play 2', 'end'}
    assert legal_actions({'ready_for_command': False, 'available_commands': ['start']}) == []
    assert legal_actions({'ready_for_command': True, 'available_commands': ['start', 'resume']}) == [
        {'id': 'resume', 'label': '继续自己的游戏存档'}]


def test_hidden_draw_order_and_seed_do_not_reach_model():
    raw = combat()
    shown = visible_state(raw)
    assert 'hidden-next-card' not in json.dumps(shown) and 'seed' not in shown['game']
    assert shown['game']['combat_state']['draw_pile_count'] == 1
    assert 'move_id' not in shown['game']['combat_state']['monsters'][0]
    assert raw['game_state']['seed'] == 123


def test_reward_memory_distinguishes_free_card_from_gold_spent():
    from roundtable.games.spire import highlights
    before = {'floor': 1, 'gold': 99, 'screen_type': 'CARD_REWARD', 'deck': []}
    after = {**before, 'deck': [{'name': 'Pommel Strike'}]}
    moments = highlights([{'before': {'in_game': True, 'game': before}, 'after': {'game': after}}])
    assert '没有花费金币' in '\n'.join(moments)
    after['gold'] = 49
    moments = highlights([{'before': {'in_game': True, 'game': before}, 'after': {'game': after}}])
    assert '花费 50 金币' in '\n'.join(moments)
    assert '没有花费金币' not in '\n'.join(moments)


def test_bridge_refuses_stale_wrong_owner_and_unscoped_commands():
    request = {'session': 's', 'sequence': 4, 'command': 'play 1 0'}
    assert command_for(request, 's', 4) == 'play 1 0'
    assert command_for(request, 'another-member-session', 4) is None
    assert command_for(request, 's', 5) is None
    for command in ('click left 200 300', 'key confirm', 'play 1\nend', 'console god', 'start ironclad 999'):
        assert command_for({**request, 'command': command}, 's', 4) is None


def test_permissions_are_explicit_and_bounded(tmp_path):
    install = tmp_path / 'installed'
    install.mkdir(); (install / 'desktop-1.0.jar').write_bytes(b'game')
    valid = {'enabled': True, 'members': ['a'], 'install_dir': str(install)}
    assert normalize_permissions(valid, {'a'})['minutes'] == 20
    for bad in ({'members': ['unknown']}, {'minutes': 999}, {'decisions': True}, {'install_dir': 'missing'}):
        with pytest.raises(ConfigError):
            normalize_permissions({**valid, **bad}, {'a'})


def test_profile_setup_preserves_original_saves_and_separates_members(tmp_path):
    from roundtable.games.spire import ASSETS
    root, install = tmp_path / 'game', tmp_path / 'human-game'
    (install / 'jre/bin').mkdir(parents=True)
    (install / 'jre/bin/java.exe').write_bytes(b'java')
    (install / 'preferences').mkdir()
    original = install / 'preferences/STSSaveSlots'; original.write_text('human-save')
    (root / 'runtime').mkdir(parents=True)
    for name in ASSETS:
        (root / 'runtime' / name).write_bytes(name.encode())
    (root / 'setup').mkdir()
    atomic_json(root / 'setup/asset-manifest.json', {name: {'sha256': hashlib.sha256(name.encode()).hexdigest()} for name in ASSETS})
    a = SpireProcess(root, install, 'a', root / 'runs/a')
    b = SpireProcess(root, install, 'b', root / 'runs/b')
    a.prepare(); b.prepare()
    assert original.read_text() == 'human-save'
    (a.profile / 'preferences/custom').write_text('only-a')
    assert not (b.profile / 'preferences/custom').exists()
    assert json.loads((b.profile / 'preferences/STSSaveSlots').read_text())['PROFILE_NAME'] == 'b'
    with pytest.raises(ConfigError):
        SpireProcess(root, install, '../human-game', root / 'runs/bad')


async def test_changed_observation_does_not_send_an_old_card_choice(tmp_path):
    p = SpireProcess(tmp_path, tmp_path, 'a', tmp_path / 'run')
    p.bridge.mkdir(parents=True)
    atomic_json(p.bridge / 'state.json', {'session': p.session, 'sequence': 7})
    with pytest.raises(ConfigError, match='状态已变化'):
        await p.act({'sequence': 6}, 'play 1 0')
    assert not (p.bridge / 'command.json').exists()


def test_only_one_game_controller_can_claim_the_same_machine(tmp_path):
    with exclusive_game(tmp_path):
        with pytest.raises(ConfigError, match='占用'):
            with exclusive_game(tmp_path):
                pass
    with exclusive_game(tmp_path):
        pass


@pytest.mark.parametrize('command', ['play 1 0', 'click left 10 20'])
async def test_real_results_are_required_for_game_receipts(tmp_path, monkeypatch, command):
    calls = []
    class Process:
        def __init__(self, root, install, member, run_dir):
            run_dir.mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def act(self, observation, action):
            calls.append(action)
            after = combat(); after['game_state'].update(room_phase='COMPLETE', screen_type='COMBAT_REWARD')
            return {'sequence': 2, 'payload': after}
        async def close(self): return True
    class Actor:
        async def reply(self, *args): return json.dumps({'action': command, 'reason': '自己选择的一步'})
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    service = GameService(tmp_path, lambda *_: Actor())
    service.config.update(enabled=True, members=['a'])
    receipt = await service.play('a', decision_limit=1)
    if command.startswith('play'):
        assert calls == [command] and receipt['actions'] == 1 and receipt['battles_won'] == [1]
        trace = service.root / 'runs' / receipt['id'] / 'actions.jsonl'
        assert hashlib.sha256(trace.read_bytes()).hexdigest() == receipt['trace_sha256']
    else:
        assert calls == [] and receipt['actions'] == 0 and receipt['status'] == 'error'
    assert not service.running and receipt['checkpoint_available']


async def test_daily_life_may_choose_play_only_with_current_permission(tmp_path):
    played = []
    async def ask(*args): return '{"action":"play","reason":"想试试"}'
    async def play(pid): played.append(pid)
    async def share(*args): pytest.fail('Playing must not automatically publish')
    engine = LifeEngine(LifeStore(tmp_path), ask, share, lambda _: '', {'a': 'A'}, play=play, can_play=lambda _: True)
    assert await engine.step('a', [], False) == 'play' and played == ['a']
    engine.can_play = lambda _: False
    assert await engine.step('a', [], False) == 'blocked' and played == ['a']


def test_game_experience_is_recorded_once_and_only_for_the_player(tmp_path):
    r = runtime(tmp_path)
    receipt = {'id': 'actual-run', 'member': 'claude', 'actions': 9, 'seconds': 42,
               'last': {'floor': 1, 'current_hp': 75, 'max_hp': 80}, 'battles_won': [1],
               'stopped_because': 'pilot_battle_completed', 'checkpoint_available': True, 'trace_sha256': 'actual-trace'}
    folder = r.games.root / 'runs/actual-run'; folder.mkdir(parents=True)
    event = {'before': visible_state(combat()), 'after': visible_state(combat()), 'reason': '打击', 'error': None}
    raw = '\n'.join(json.dumps(event) for _ in range(9)).encode()
    (folder / 'actions.jsonl').write_bytes(raw)
    receipt['trace_sha256'] = hashlib.sha256(raw).hexdigest()
    r._record_game(receipt); r._record_game(receipt)
    assert len(r.life.artifacts) == 1
    assert '75/80' in r.life.chat_summary('claude')
    assert '75/80' not in r.life.chat_summary('codex')
    assert '75/80' in r.life.game_recall('claude', '游戏玩得怎么样')
    assert r.life.game_recall('codex', 'Claude 的游戏玩得怎么样') == ''
    assert r.life.game_recall('claude', '今天下雨了') == ''
    (folder / 'actions.jsonl').write_bytes(b'tampered')
    with pytest.raises(ConfigError, match='校验失败'):
        r.games.events(receipt)


async def test_stop_cancels_a_pending_model_decision_and_keeps_execution_receipt(tmp_path, monkeypatch):
    entered, closed = asyncio.Event(), []
    class Process:
        def __init__(self, *args): args[-1].mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def close(self): closed.append(True); return True
    class Actor:
        async def reply(self, *args):
            entered.set()
            await asyncio.Event().wait()
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    service = GameService(tmp_path, lambda *_: Actor())
    service.config.update(enabled=True, members=['a'])
    task = asyncio.create_task(service.play('a'))
    await entered.wait()
    service.stop()
    result = await task
    assert closed and result['actions'] == 0 and result['stopped_because'] == 'owner_stopped'
    assert not service.running and service.receipts()[0]['checkpoint_available']


async def test_pause_waits_for_dispatched_action_and_records_it_before_save(tmp_path, monkeypatch):
    dispatched, finish = asyncio.Event(), asyncio.Event()
    order = []
    class Process:
        def __init__(self, *args): args[-1].mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def act(self, observation, command):
            dispatched.set()
            await finish.wait()
            order.append('acted')
            after = combat(); after['game_state']['current_hp'] = 73
            return {'sequence': 2, 'payload': after}
        async def close(self): order.append('saved'); return True
    class Actor:
        async def reply(self, *args): return '{"action":"end"}'
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    service = GameService(tmp_path, lambda *_: Actor())
    service.config.update(enabled=True, members=['a'])
    task = asyncio.create_task(service.play('a'))
    await dispatched.wait()
    service.stop()
    await asyncio.sleep(0)
    assert not task.done() and not order
    finish.set()
    result = await task
    assert order == ['acted', 'saved']
    assert result['actions'] == 1 and result['last']['current_hp'] == 73
    assert result['stopped_because'] == 'owner_stopped' and result['checkpoint_available']
    assert len(service.events(result)) == 1


async def test_returning_to_desktop_during_thinking_does_not_execute_choice(tmp_path, monkeypatch):
    idle = [9999]
    class Process:
        def __init__(self, *args): args[-1].mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def act(self, *args): pytest.fail('Owner returned while the model was thinking')
        async def close(self): return True
    class Actor:
        async def reply(self, *args):
            idle[0] = 0
            return '{"action":"end"}'
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    monkeypatch.setattr('roundtable.games.spire.idle_seconds', lambda: idle[0])
    service = GameService(tmp_path, lambda *_: Actor())
    service.config.update(enabled=True, members=['a'], autoplay=True)
    result = await service.play('a', automatic=True)
    assert result['actions'] == 0 and result['stopped_because'] == 'owner_returned'


async def test_stop_before_scheduled_game_launch_never_opens_game(tmp_path):
    r = runtime(tmp_path)
    r.games.config.update(enabled=True, members=['claude'])
    async def unexpected(*args, **kwargs): pytest.fail('A queued game must not launch after stop')
    r._play_game = unexpected
    r.start_game('claude')
    await r.stop_game()
    assert r._game_task is None and not r.games.running


def test_autoplay_requires_desktop_idle_and_respects_daily_limit(tmp_path, monkeypatch):
    service = GameService(tmp_path, lambda *_: None)
    service.config.update(enabled=True, members=['a'], autoplay=True, sessions_per_day=1)
    monkeypatch.setattr('roundtable.games.spire.idle_seconds', lambda: 0)
    assert service.available('a') and not service.available('a', automatic=True)
    monkeypatch.setattr('roundtable.games.spire.idle_seconds', lambda: 9999)
    assert service.available('a', automatic=True)
    path = service.root / 'runs/done'; path.mkdir(parents=True)
    from datetime import date
    atomic_json(path / 'receipt.json', {'id': 'done', 'member': 'a', 'day': date.today().isoformat(), 'started_at': 1})
    assert not service.available('a')
    r = runtime(tmp_path / 'app')
    r.games.running = 'claude'
    assert not r._life_idle()


async def test_game_endpoints_require_owner_auth_and_origin(admin):  # noqa: F811
    assert (await admin.get('/api/games')).status == 401
    assert (await admin.post('/api/games/claude/start')).status == 401
    await login(admin)
    current = await (await admin.get('/api/games')).json()
    assert not current['settings']['enabled']
    assert (await admin.put('/api/games', json=current['settings'])).status == 403
    assert (await admin.put('/api/games', json=current['settings'], headers={'Origin': admin.origin})).status == 200
    assert (await admin.post('/api/games/unknown/start', headers={'Origin': admin.origin})).status == 400
    assert (await admin.get('/api/games/runs/missing')).status == 404


async def test_a_card_not_in_hand_gets_a_second_chance(tmp_path, monkeypatch):
    calls, prompts = [], []
    class Process:
        def __init__(self, root, install, member, run_dir):
            run_dir.mkdir(parents=True)
        async def start(self): return {'sequence': 1, 'payload': combat()}
        async def act(self, observation, action):
            calls.append(action)
            after = combat(); after['game_state'].update(room_phase='COMPLETE', screen_type='COMBAT_REWARD')
            return {'sequence': 2, 'payload': after}
        async def close(self): return True
    answers = iter(['play 4 0', 'play 1 0'])  # Bash isn't in hand; then a card that is
    class Actor:
        async def reply(self, key, prompt):
            prompts.append(prompt)
            return json.dumps({'action': next(answers), 'reason': '趁易伤先重击'})
    monkeypatch.setattr('roundtable.games.spire.SpireProcess', Process)
    service = GameService(tmp_path, lambda *_: Actor())
    service.config.update(enabled=True, members=['a'])
    receipt = await service.play('a', decision_limit=1)
    assert calls == ['play 1 0'] and receipt['actions'] == 1 and receipt['status'] != 'error'
    assert '「play 4 0」不在这一步的可选动作里' in prompts[1] and 'play 1 0（' in prompts[1]
