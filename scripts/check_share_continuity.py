"""Replay a recorded personal share with isolated sessions and a local capture sink.

Uses configured model accounts, but never connects a Telegram bot or posts to Telegram.
Selects a recorded opening by a required substring instead of generating test traffic.
"""
import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, build_opener, ProxyHandler

from nookling.life import LifeStore
from nookling.memes import MemeLibrary
from nookling.chronicle import Chronicle
from nookling.participants import SessionStore, participant_class
from nookling.prompts import build_personal_share_prompt, build_turn_prompt
from nookling.conversation import Conversation
from nookling.runtime import Runtime
from nookling.settings import build_config
from nookling.transcript import Message


class CaptureSink:
    def __init__(self):
        self.posts = []

    async def post(self, chat, actor, text, reply_to=None):
        self.posts.append({"member": actor.id, "text": text})
        print(json.dumps(self.posts[-1], ensure_ascii=False), flush=True)

    @asynccontextmanager
    async def typing(self, *args):
        yield

    async def activity(self, *args): pass
    async def clear_activity(self, *args): pass
    async def notice(self, *args): pass
    async def react(self, *args): pass
    async def post_sticker(self, *args): pass


async def main(root, match):
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    secrets = json.loads((root / "secrets.json").read_text(encoding="utf-8"))
    cfg = build_config(settings, secrets, root)
    life = LifeStore(root)
    share = next(s for s in reversed(life.shares) if match in s["text"])
    matched = []
    for log in (root / "chats").glob("*.jsonl"):
        history = [Message.from_json(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines() if line]
        for i, message in enumerate(history):
            if (message.speaker_id == share["member"] and message.text == share["text"]
                    and abs(message.ts - share["ts"]) < 120):
                matched.append((history, i))
    assert len(matched) == 1, "Expected one exact public share record"
    messages, index = matched[0]
    opening = messages[index]
    peer_reply = messages[index + 1]
    assert peer_reply.speaker_id not in ("user", opening.speaker_id)
    output = root / "backups" / ("share-continuity-replay-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    base = "http://127.0.0.1:" + str(settings["web"]["port"])
    opener = build_opener(ProxyHandler({}))
    headers = {"Cookie": "nookling_admin=" + secrets["web_token"], "Origin": base, "Content-Type": "application/json"}
    def preview(pid):
        body = json.dumps({"settings": settings, "member": pid}).encode()
        with opener.open(Request(base + "/api/preview", headers=headers, data=body), timeout=15) as response:
            return json.load(response)["prompt"]

    context = SimpleNamespace(life=life, config=cfg, memes=MemeLibrary(root), chronicle=Chronicle(root))
    def actor(pid, sessions, folder):
        pc = replace(next(p for p in cfg.participants if p.id == pid), web_search=False)
        member = participant_class(pc)(pc, sessions, folder / pid, 120)
        member.set_system_prompt(preview(pid))
        member.turn_context = lambda prompt: Runtime._chat_context(context, pc, prompt)
        return member

    # An old prepared opening must reach its author's next public input even on reload.
    sessions = SessionStore(output / "legacy" / "sessions.json")
    author = actor(opening.speaker_id, sessions, output / "legacy")
    sink = CaptureSink()
    room = Conversation("isolated-legacy", [author], sink, replace(cfg.round, max_parallel=1), sessions)
    room.messages = [replace(opening, external_to_session=True), peer_reply]
    legacy = await room._ask(author, build_turn_prompt([peer_reply], 1, me=author.id), 1)
    print(json.dumps({"case": "legacy_followup", "reply": legacy}, ensure_ascii=False), flush=True)

    # New shares are composed by the same public session that then answers the peer.
    sessions = SessionStore(output / "composed" / "sessions.json")
    author = actor(opening.speaker_id, sessions, output / "composed")
    peer = actor(peer_reply.speaker_id, sessions, output / "composed")
    sink = CaptureSink()
    room = Conversation("isolated-composed", [author, peer], sink, replace(cfg.round, max_parallel=1), sessions)
    room.messages = messages[max(0, index - 5):index]
    future = room.start_proactive(author, lambda unseen: build_personal_share_prompt(
        unseen, opening.text, opening.sources, me=author.id), 1, sources=opening.sources, discussion=True)
    await future
    await room.wait_idle()
    result = {"original_opening": opening.text, "original_peer_reply": peer_reply.text,
              "legacy_followup": legacy, "composed_dialogue": sink.posts, "not_sent_to_telegram": True}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(output / "result.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--match", required=True, help="A substring in the recorded sharing text")
    args = parser.parse_args()
    asyncio.run(main(args.data_dir, args.match))
