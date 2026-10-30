"""Real game + real participants, with a local group preview instead of Telegram."""
import argparse
import asyncio
import json
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from nookling.games.social import GameConversation
from nookling.games.spire import GameService, make_game_actor
from nookling.life import LifeStore
from nookling.participants import SessionStore, participant_class
from nookling.conversation import Conversation
from nookling.runtime import Runtime
from nookling.settings import SettingsStore


class PreviewSink:
    def __init__(self, folder): self.folder, self.posts, self.photos, self.index = folder, [], [], 0
    async def post(self, chat, speaker, text, reply_to=None):
        self.index += 1
        self.posts.append({'speaker': speaker.id, 'text': text})
        print(json.dumps({'scope': 'local group preview', **self.posts[-1]}, ensure_ascii=False), flush=True)
        return self.index
    async def post_photo(self, chat, speaker, path, caption, reply_to=None):
        self.photos.append(str(path))
        return await self.post(chat, speaker, caption, reply_to)
    async def react(self, *args): pass
    async def notice(self, *args): pass
    async def activity(self, *args): pass
    async def clear_activity(self, *args): pass
    async def post_sticker(self, *args): pass
    @asynccontextmanager
    async def typing(self, *args): yield


async def main(root, player, peer, decisions):
    snapshot = root / 'games/slay-the-spire/setup' / ('social-preview-' + time.strftime('%Y%m%dT%H%M%S'))
    snapshot.mkdir()
    shutil.copy2(root / 'life.json', snapshot / 'life.json')
    runtime = Runtime(SettingsStore(root))
    runtime.life = LifeStore(snapshot)  # No writes to production memories or sessions.
    configs = {p.id: p for p in runtime.config.enabled_participants}
    names = {p.id: p.name for p in runtime.config.participants}
    sink = PreviewSink(snapshot)
    public = []
    for pid in (player, peer):
        pc = replace(configs[pid], web_search=False, max_history=8, sleep='', busy='', activity='high')
        folder = snapshot / 'public' / pid
        actor = participant_class(pc)(pc, SessionStore(folder / 'sessions.json'), folder, 120)
        actor.set_system_prompt(runtime.system_prompt_for(pc))
        actor.turn_context = lambda prompt, pc=pc: runtime._chat_context(pc, prompt)
        public.append(actor)
    room = Conversation('__social_preview', public, sink,
        replace(runtime.config.round, max_turns_per_model=2, max_parallel=1, follow_up_chance=1, follow_up_decay=0),
        SessionStore(snapshot / 'room-sessions.json'))
    charged, buddies = [], {}
    social = None

    def make_actor(pid, folder):
        actor = make_game_actor(configs[pid], folder, 120)
        reply = actor.reply
        async def log(*args):
            answer = await reply(*args)
            print(json.dumps({'scope': 'player decision', 'member': pid, 'reply': answer}, ensure_ascii=False), flush=True)
            return answer
        actor.reply = log
        return actor

    def make_social(pid, folder, preferred):
        nonlocal social
        async def ask(who, prompt):
            if who not in buddies:
                pc = replace(configs[who], web_search=False, max_history=8)
                work = folder / 'conversation' / who
                buddies[who] = participant_class(pc)(pc, SessionStore(work / 'sessions.json'), work, 120)
                buddies[who].set_system_prompt(runtime.system_prompt_for(pc) + '\n根据本轮要求返回 JSON；不使用工具。')
                buddies[who].turn_context = lambda prompt, pc=pc: runtime._chat_context(pc, prompt)
            answer = await buddies[who].reply('game-company', prompt)
            print(json.dumps({'scope': 'private companion', 'member': who, 'reply': answer}, ensure_ascii=False), flush=True)
            return answer
        async def publish(who, text, image, scene):
            at = time.time()
            sender = next(p for p in public if p.id == who)
            future = room.start_proactive(sender, lambda _: '', 1, prepared_text=text, discussion=True,
                                          image_path=image, image_description=scene)
            posted = await future if future is not None else None
            return {'text': posted, 'at': at} if posted else None
        async def wait_public():
            if room.busy:
                await asyncio.wait({room._task}, timeout=35)
        social = GameConversation(pid, folder, names, runtime.life, ask, lambda: [peer], charged.append,
            publish=publish, can_share=lambda: not room.busy,
            public_feed=lambda since: [{'speaker': m.speaker_name, 'text': m.text} for m in room.messages if m.ts >= since],
            requested_peer=preferred, wait_public=wait_public)
        social.publication_target = 'local_preview'
        return social

    service = GameService(root, make_actor, make_social)
    runtime.games = service
    service.config.update(enabled=True, members=[player], decisions=decisions, minutes=10, sessions_per_day=0)
    receipt = await service.play(player, peer=peer)
    if room.busy:
        await asyncio.wait({room._task}, timeout=40)
        if room.busy: await room.stop()
    result = {'receipt': receipt, 'private': social.messages if social else [], 'public_preview': sink.posts,
              'screenshots': sink.photos, 'passive_charges': charged, 'not_sent_to_telegram': True}
    (snapshot / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'result': str(snapshot / 'result.json'), 'receipt': receipt}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--player', default='deepseek')
    p.add_argument('--peer', default='codex')
    p.add_argument('--decisions', type=int, default=14)
    args = p.parse_args()
    asyncio.run(main(args.data_dir, args.player, args.peer, args.decisions))
