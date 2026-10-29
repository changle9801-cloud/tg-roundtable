"""Exercise a real model/game action interrupted after dispatch, then verify resume.

Uses isolated game sessions and never sends Telegram messages or edits chat memory.
"""
import argparse
import asyncio
import json
from pathlib import Path

from roundtable.games.spire import GameService, SpireProcess, exclusive_game, make_game_actor, progress
from roundtable.settings import build_config


async def main(root, member):
    settings = json.loads((root / 'settings.json').read_text(encoding='utf-8'))
    secrets = json.loads((root / 'secrets.json').read_text(encoding='utf-8'))
    cfg = build_config(settings, secrets, root)
    pc = next(p for p in cfg.enabled_participants if p.id == member)
    service = GameService(root, lambda _, folder: make_game_actor(pc, folder, cfg.round.call_timeout))
    service.config.update(enabled=True, members=[member], decisions=5, sessions_per_day=0)
    original = SpireProcess.observe
    interrupted = []

    async def observe(process, after=0, timeout=25):
        if after and process.last and process.last['payload'].get('in_game') and not interrupted:
            # act() has already written the command. The real game must finish this
            # step and return its actual state before the controller saves/quits.
            interrupted.append(after)
            service.stop()
        return await original(process, after=after, timeout=timeout)

    SpireProcess.observe = observe
    try:
        receipt = await service.play(member)
    finally:
        SpireProcess.observe = original
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    assert interrupted and receipt['stopped_because'] == 'owner_stopped'
    assert receipt['actions'] >= 2 and receipt['checkpoint_available']
    assert len(service.events(receipt)) == receipt['actions']
    folder = service.root / 'runs' / receipt['id'] / 'resume-verification'
    with exclusive_game(service.root):
        process = SpireProcess(service.root, Path(service.config['install_dir']), member, folder)
        try:
            menu = await process.start()
            assert 'resume' in menu['payload']['available_commands']
            resumed = await process.act(menu, 'resume')
            state = progress(resumed['payload'])
            assert state['floor'] == receipt['last']['floor']
        finally:
            saved = await process.close()
    result = {'interrupted_after_dispatch': True, 'actions_recorded': receipt['actions'],
              'resumed': state, 'checkpoint_available': saved, 'not_sent_to_telegram': True}
    (folder / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    assert saved


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--member', default='grok')
    args = parser.parse_args()
    asyncio.run(main(args.data_dir, args.member))
