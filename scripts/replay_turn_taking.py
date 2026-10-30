"""Replay the owner's real group messages through the turn-taking code, with stand-in members.

No model is called and nothing is sent: every member answers a fixed "嗯", so only *who*
speaks is measured, not what they say. Each of the owner's messages is replayed with the
group history before it and the members' real sleep/work hours at that moment, many times
with different dice, and the script prints how many replies a message gets, how the talking
is shared out, and how long two members keep going back and forth.

    .venv\\Scripts\\python.exe scripts\\replay_turn_taking.py                 # data dir from ROUNDTABLE_HOME
    .venv\\Scripts\\python.exe scripts\\replay_turn_taking.py --runs 50 --json out.json

Used to check changes to room.py / urge.py (see docs/urge-to-speak.md): run before and after.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import tempfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from statistics import mean

from nookling.config import ParticipantConfig
from nookling.moderator import RuleModerator
from nookling.participants import Participant, SessionStore
from nookling.conversation import Conversation
from nookling.routine import Routines
from nookling.settings import SettingsStore, build_config, resolve_data_dir
from nookling.transcript import Message, find_mentions, find_recheck_targets

HISTORY = 40  # messages of context before each replayed one


class StandIn(Participant):
    def __init__(self, cfg: ParticipantConfig, store: SessionStore, work_dir: Path):
        super().__init__(cfg, store, work_dir, timeout=5)

    async def reply(self, chat_key, prompt, on_activity=None, images=()) -> str:
        return "嗯"


class QuietSink:
    def __init__(self):
        self.posts: list[str] = []

    async def post(self, chat_key, participant, text, reply_to=None):
        self.posts.append(participant.id)

    async def post_photo(self, chat_key, participant, path, caption, reply_to=None):
        self.posts.append(participant.id)

    async def post_sticker(self, chat_key, participant, sticker):
        pass

    async def react(self, chat_key, participant, message_id, emoji):
        pass

    async def notice(self, chat_key, text):
        pass

    async def activity(self, chat_key, participant, text):
        pass

    async def clear_activity(self, chat_key, participant):
        pass

    @asynccontextmanager
    async def typing(self, chat_key, participant):
        yield


def load_messages(data_dir: Path) -> list[Message]:
    out = []
    for path in sorted((data_dir / "chats").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Message.from_json(json.loads(line)))
    return out


def run_chain(posts: list[str]) -> int:
    """Longest stretch of two members alternating (A B A B ...)."""
    best = 0
    for i in range(len(posts)):
        n = 1
        while i + n < len(posts) and posts[i + n] != posts[i + n - 1] and (n < 2 or posts[i + n] == posts[i + n - 2]):
            n += 1
        best = max(best, n if n >= 2 else 0)
    return best


async def replay(members: list[ParticipantConfig], history: list[Message], user: Message, seed: int,
                 round_cfg, tmp: Path) -> list[str]:
    store = SessionStore(tmp / "sessions.json")
    people = [StandIn(cfg, store, tmp / cfg.id) for cfg in members]
    clock = datetime.fromtimestamp(user.ts)
    room = Conversation("replay", people, QuietSink(), round_cfg, store, RuleModerator(random.Random(seed)),
                    routines=Routines(now=lambda: clock))
    room.messages = list(history)
    room.cursors = {p.id: len(room.messages) for p in people}
    mentions = find_mentions(user.text, people, from_user=True)
    recheck = find_recheck_targets(user.text, people, room.messages, now=user.ts)
    room._append(Message(user.speaker_id, user.speaker_name, user.text, tuple(mentions), user.ts, user.images,
                         recheck_targets=tuple(recheck)))
    room._task = asyncio.create_task(room._run_round())
    await room.wait_idle()
    return room.sink.posts


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--runs", type=int, default=20, help="dice per message")
    parser.add_argument("--json", type=Path, help="also write the numbers here")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args.data_dir)
    config = build_config(*SettingsStore(data_dir).load(), data_dir)
    members = [ParticipantConfig(id=p.id, name=p.name, kind="fake", aliases=list(p.aliases), activity=p.activity,
                                 sleep=p.sleep, busy=p.busy, busy_doing=p.busy_doing)
               for p in config.participants if p.enabled]
    messages = load_messages(data_dir)
    owner = [i for i, m in enumerate(messages) if m.from_user]

    replies, nobody, chains, share = [], 0, [], Counter()
    with tempfile.TemporaryDirectory() as tmp:
        for i in owner:
            history = messages[max(0, i - HISTORY):i]
            for seed in range(args.runs):
                posts = await replay(members, history, messages[i], seed, config.round, Path(tmp))
                replies.append(len(posts))
                nobody += not posts
                chains.append(run_chain(posts))
                share.update(posts)

    total = sum(share.values()) or 1
    result = {
        "owner_messages": len(owner),
        "runs": args.runs,
        "replies_mean": round(mean(replies), 3) if replies else 0,
        "replies": dict(sorted(Counter(replies).items())),
        "nobody_answered": round(nobody / max(1, len(replies)), 3),
        "share": {pid: round(n / total, 3) for pid, n in share.most_common()},
        "back_and_forth": dict(sorted(Counter(chains).items())),
    }
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
