"""Run an owner-requested, bounded real-game check. Never posts to Telegram."""
import argparse
import asyncio
import json
from pathlib import Path

from roundtable.games.spire import GameService, make_game_actor
from roundtable.settings import build_config


async def main(root, member, install, decisions):
    settings = json.loads((root / 'settings.json').read_text(encoding='utf-8'))
    secrets = json.loads((root / 'secrets.json').read_text(encoding='utf-8'))
    config = build_config(settings, secrets, root)
    members = {p.id: p for p in config.enabled_participants}
    if member not in members:
        raise ValueError('Participant must be enabled')
    def make(pid, folder):
        actor = make_game_actor(members[pid], folder, config.round.call_timeout)
        original = actor.reply
        async def reply(*args, **kwargs):
            result = await original(*args, **kwargs)
            print(json.dumps({'member': pid, 'decision': result}, ensure_ascii=False), flush=True)
            return result
        actor.reply = reply
        return actor
    service = GameService(root, make)
    # This explicit CLI run does not enable unattended play or alter persistent permissions.
    service.config.update(enabled=True, members=[member], install_dir=str(install), decisions=decisions, sessions_per_day=5)
    result = await service.play(member, stop_after_battle=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--member', required=True)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--decisions', type=int, default=35)
    args = parser.parse_args()
    asyncio.run(main(args.data_dir, args.member, args.install_dir, args.decisions))
