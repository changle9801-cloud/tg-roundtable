"""Local admin page: http://127.0.0.1:<port>, reachable only from this machine.

Access needs the token printed at startup (kept in secrets.json). The token link sets an
HttpOnly cookie; requests must also carry a local Host header (against DNS rebinding) and,
for writes, a local Origin header (against cross-site requests).
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from aiohttp import web

from ..config import ConfigError
from ..participants import ParticipantError
from ..participants.api import PROVIDERS
from ..prompts import DEFAULT_SYSTEM_PROMPT, PLACEHOLDERS
from ..settings import apply_secret_changes, masked_secrets
from .. import models
from ..furniture import FUNCTIONS, PlacementError
from ..memory import LEARN_INSTRUCTIONS
from ..kaomoji import STARTER as KAOMOJI_STARTER, parse_lines
from ..stickers import KINDS as STICKER_KINDS

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
VIDEO_TOOL = Path(__file__).parent.parent / "mcp_servers" / "video.py"  # the built-in 看视频 server
READER_TOOL = Path(__file__).parent.parent / "mcp_servers" / "reader.py"
COOKIE = "nookling_admin"
LEGACY_COOKIE = "roundtable_admin"  # set before the rename; still accepted so open pages stay logged in

KIND_LABELS = {
    "claude_cli": "Claude 订阅（Claude Code CLI）",
    "codex_cli": "Codex 订阅（Codex CLI）",
    "grok_cli": "Grok 订阅（Grok Build CLI）",
    "api": "API（兼容 OpenAI 格式，按量计费）",
}

UNAUTHORIZED_PAGE = """<!doctype html><meta charset="utf-8"><title>需要访问口令</title>
<body style="font-family:system-ui;max-width:560px;margin:80px auto;padding:0 16px;line-height:1.7">
<h2>需要访问口令</h2>
<p>请使用 bot 启动时终端里打印的链接打开管理页面（链接里带有 <code>?token=…</code>）。</p>
<p>口令保存在数据目录的 <code>secrets.json</code> 里。</p></body>"""


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def make_app(runtime: Runtime, token: str, port: int) -> web.Application:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.host not in hosts:
            return web.Response(status=403, text="forbidden host")
        if request.path == "/" and "token" in request.query:
            if not _same(request.query["token"], token):
                return web.Response(status=401, text=UNAUTHORIZED_PAGE, content_type="text/html")
            resp = web.Response(status=302, headers={"Location": "/"})
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", path="/")
            return resp
        if not _same(request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE, ""), token):
            if request.path.startswith("/api/"):
                return web.json_response({"error": "未登录，请使用启动时打印的链接打开管理页面"}, status=401)
            return web.Response(status=401, text=UNAUTHORIZED_PAGE, content_type="text/html")
        if request.method not in ("GET", "HEAD") and request.headers.get("Origin") not in origins:
            return web.json_response({"error": "forbidden origin"}, status=403)
        try:
            resp = await handler(request)
            if request.path.startswith("/static/"):
                resp.headers["Cache-Control"] = "no-cache"  # pick up page updates after an upgrade
            return resp
        except (ConfigError, ParticipantError) as e:
            return web.json_response({"error": str(e)}, status=400)
        except json.JSONDecodeError:
            return web.json_response({"error": "请求内容不是合法的 JSON"}, status=400)

    def stickers() -> list[dict]:
        return [s.public() for s in runtime.stickers.items]

    def kaomoji() -> list[dict]:
        return [asdict(k) for k in runtime.kaomoji.items]

    def feedback() -> list[dict]:
        return [asdict(f) for f in runtime.feedback.items[-150:]]

    def game_memories() -> dict:
        from ..game_memories import FEELINGS

        store = runtime.game_memories
        now = store.now()
        items = [{**asdict(m), "strength": round(m.strength(now), 2), "icon": FEELINGS.get(m.feeling, "")}
                 for m in sorted(store.memories, key=lambda m: m.at, reverse=True)[:300]]
        return {"items": items, "opinions": {pid: store.opinions_view(pid) for pid in store.opinions}}

    def followups() -> list[dict]:
        return [asdict(f) for f in runtime.followups.items[-60:]]

    def chronicle() -> list[dict]:
        return [asdict(m) for m in runtime.chronicle.moments]

    def memes() -> list[dict]:
        return [asdict(m) for m in runtime.memes.items]

    def memory() -> list[dict]:
        store = runtime.memory
        shown = {f.id for pid in ["", *(p.id for p in runtime.config.participants)] for f in store.in_prompt(pid)}
        return [{**asdict(f), "in_prompt": f.id in shown} for f in store.items]

    def state() -> dict:
        return {
            "settings": runtime.settings,
            "secrets": masked_secrets(runtime.secrets),
            "status": runtime.status(),
            "stickers": stickers(),
            "kaomoji": kaomoji(),
            "memory": memory(),
            "memes": memes(),
            "followups": followups(),
            "feedback": feedback(),
            "game_memories": game_memories(),
            "chronicle": chronicle(),
            "meta": {
                "memory_instructions": LEARN_INSTRUCTIONS,
                "sticker_prompt": runtime.sticker_prompt(),
                "sticker_exts": sorted(STICKER_KINDS),
                "kinds": KIND_LABELS,
                "providers": {k: {"base_url": url, "env": env} for k, (url, env) in PROVIDERS.items()},
                "default_prompt": DEFAULT_SYSTEM_PROMPT,
                "placeholders": PLACEHOLDERS,
                "data_dir": str(runtime.store.data_dir),
                "video_tool": str(VIDEO_TOOL),
                "reader_tool": str(READER_TOOL),
                "function_names": FUNCTIONS,
            },
        }

    async def index(request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    async def get_state(request):
        return web.json_response(state())

    async def get_status(request):
        return web.json_response(runtime.status())

    async def put_settings(request):
        await runtime.apply(settings=await request.json())
        return web.json_response(state())

    async def put_secrets(request):
        ids = {p["id"] for p in runtime.settings["participants"]}
        tool_ids = {t["id"] for t in runtime.settings["tools"]}
        changes = await request.json()
        await runtime.apply(secrets=apply_secret_changes(runtime.secrets, changes, ids, tool_ids))
        return web.json_response(state())

    async def preview(request):
        body = await request.json()
        return web.json_response({"prompt": runtime.preview_prompt(body.get("settings") or {}, str(body.get("member", "")))})

    def journal() -> dict:
        names = {p["id"]: p["name"] for p in runtime.settings["participants"]}
        return {
            "members": [
                {"id": pid, "name": name, "entries": [asdict(e) for e in runtime.journal.of(pid)]}
                for pid, name in names.items()
            ]
        }

    async def get_journal(request):
        return web.json_response(journal())

    async def get_life(request):
        return web.json_response(runtime.life_status())

    async def life_artifact(request):
        item = next((a for a in runtime.life.artifacts if a["id"] == request.match_info["aid"]), None)
        if item is None:
            raise web.HTTPNotFound()
        return web.Response(text=runtime.life_workbench.read_artifact(item), content_type="text/plain",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def step_life(request):
        return web.json_response(runtime.start_life(request.match_info["pid"]))

    async def get_home(request):
        return web.json_response(runtime.home_view())

    async def home_diary(request):
        return web.json_response(runtime.diary_book(request.match_info["pid"], request.query.get("day", "")))

    async def home_look(request):
        body = await request.json()
        try:
            look = runtime.home.set_look(request.match_info["pid"], str(body.get("color", "")), str(body.get("accessory", "")))
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return web.json_response(look)

    async def home_choose_look(request):
        return web.json_response(await runtime.choose_look(request.match_info["pid"]))

    # ---- the furniture manager (docs/furniture.md, 2d) ----

    def house(change):
        """Run a change to the house; its refusals ("放不下", "挡住门了") come back as a 400 with the reason."""
        try:
            return web.json_response(change())
        except (PlacementError, models.ModelError) as e:
            raise ConfigError(str(e)) from None

    async def new_type(request):
        body = await request.json()
        return house(lambda: runtime.home.save_type(body))

    async def edit_type(request):
        body = await request.json()
        return house(lambda: runtime.home.save_type(body, request.match_info["tid"]))

    async def remove_type(request):
        return house(lambda: runtime.home.delete_type(request.match_info["tid"]) or {"ok": True})

    async def new_piece(request):
        body = await request.json()
        if body.get("wall") is not None:
            return house(lambda: runtime.home.place({k: body[k] for k in ("type", "room", "wall", "along", "y") if k in body}))
        if body.get("at") is not None:
            return house(lambda: runtime.home.place({k: body[k] for k in ("type", "room", "at", "turn") if k in body}))
        return house(lambda: runtime.home.put(str(body.get("type", "")), str(body.get("room", ""))))

    async def edit_piece(request):
        body = await request.json()
        return house(lambda: runtime.home.edit_piece(request.match_info["pid"], body))

    async def remove_piece(request):
        return house(lambda: runtime.home.remove(request.match_info["pid"]) or {"ok": True})

    async def new_room(request):
        body = await request.json()
        size = body.get("size", "")
        response = house(lambda: runtime.home.add_room(body.get("name", ""), size if isinstance(size, list) else str(size),
                                                       body.get("at"), str(body.get("kind") or "room"), int(body.get("level") or 0)))
        runtime.refresh_prompts()  # the members' prompt lists the rooms of the house
        return response

    async def edit_room(request):
        body = await request.json()
        rid = request.match_info["rid"]

        def change():  # where it is and how big, floor and walls, a new name, or several
            if "at" in body or "size" in body:
                runtime.home.reshape_room(rid, body.get("at"), body.get("size"))
            looks = {k: body[k] for k in ("floor", "walls") if k in body}
            room = runtime.home.dress_room(rid, looks) if looks else runtime.home.room(rid)
            return runtime.home.rename_room(rid, body["name"]) if "name" in body else room

        response = house(change)
        runtime.refresh_prompts()
        return response

    async def new_door(request):
        body = await request.json()
        return house(lambda: runtime.home.add_door(str(body.get("line", "")), body.get("at"), outside=bool(body.get("outside")),
                                                   level=int(body.get("level") or 0)))

    async def new_floor(request):
        body = await request.json()
        return house(lambda: runtime.home.add_floor(body.get("name", "")))

    async def edit_floor(request):
        body = await request.json()
        return house(lambda: runtime.home.rename_floor(int(request.match_info["level"]), body.get("name", "")))

    async def remove_floor(request):
        return house(lambda: runtime.home.delete_floor(int(request.match_info["level"])) or {"ok": True})

    async def remove_door(request):
        return house(lambda: runtime.home.remove_door(request.match_info["did"]) or {"ok": True})

    async def remove_room(request):
        response = house(lambda: runtime.home.delete_room(request.match_info["rid"]) or {"ok": True})
        runtime.refresh_prompts()
        return response

    async def upload_model(request):
        data = await request.read()
        return house(lambda: models.store(runtime.store.data_dir, data))

    async def upload_image(request):
        data = await request.read()
        return house(lambda: models.store_image(runtime.store.data_dir, data))

    async def get_image(request):
        name = request.match_info["name"]
        path = models.image_path(runtime.store.data_dir, name)
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": models.IMAGE_TYPES[name.rsplit(".", 1)[1]],
                                               "Cache-Control": "private, max-age=86400"})

    async def get_model(request):
        path = models.path_of(runtime.store.data_dir, request.match_info["name"])
        if path is None:
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "model/gltf-binary", "Cache-Control": "private, max-age=86400"})

    async def get_games(request):
        return web.json_response(runtime.games.status())

    async def update_games(request):
        runtime.games.update(await request.json(), {p.id for p in runtime.participants})
        return web.json_response(runtime.games.status())

    async def start_game(request):
        data = await request.json() if request.can_read_body else {}
        return web.json_response(runtime.start_game(request.match_info['pid'], peer=data.get('peer')))

    async def stop_game(request):
        await runtime.stop_game()
        return web.json_response(runtime.games.status())

    async def game_trace(request):
        rid = request.match_info['rid']
        receipt = next((r for r in runtime.games.receipts() if r['id'] == rid), None)
        if receipt is None:
            raise web.HTTPNotFound()
        lines = [f"{receipt['member']} · 《杀戮尖塔》真实游玩记录", json.dumps(receipt, ensure_ascii=False, indent=2)]
        social_path = runtime.games.root / 'runs' / rid / 'social.jsonl'
        if social_path.exists():
            lines.append('\n共同游玩对话与分享：\n' + social_path.read_text(encoding='utf-8'))
        if receipt['actions']:
            for index, step in enumerate(runtime.games.events(receipt), 1):
                after = step['after'].get('game', {})
                lines.append(f"\n{index}. {step['command']}\n当时的想法：{step['reason']}\n"
                             f"实际结果：第 {after.get('floor')} 层，生命 {after.get('current_hp')}，"
                             f"画面 {after.get('screen_type')}" + (f"\n失败：{step['error']}" if step.get('error') else ''))
        return web.Response(text='\n'.join(lines), content_type='text/plain',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def delete_journal_entry(request):
        runtime.journal.delete(request.match_info["eid"])
        runtime.refresh_prompts()
        return web.json_response(journal())

    async def try_browse(request):
        added = await runtime.try_browse(request.match_info["pid"])
        return web.json_response({"added": added, **journal()})

    async def try_proactive(request):
        return web.json_response(await runtime.try_proactive(request.match_info["pid"]))

    async def clear_events(request):
        runtime.events.clear()
        return web.json_response({"events": []})

    async def test_member(request):
        return web.json_response(await runtime.test_participant(request.match_info["pid"]))

    async def member_models(request):
        return web.json_response({"models": await runtime.list_models(request.match_info["pid"])})

    async def room_action(request):
        convo = runtime.conversations.get(request.match_info["key"])
        if convo is None:
            return web.json_response({"error": "没有这个群"}, status=404)
        if request.match_info["action"] == "stop":
            await convo.stop()
        else:
            await convo.reset()
        return web.json_response(runtime.status())

    def stickers_response(**extra):
        runtime.stickers_changed()
        return web.json_response({**extra, "stickers": stickers(), "sticker_prompt": runtime.sticker_prompt()})

    def members_field(text: str) -> list:
        try:
            return json.loads(text) if text.strip() else []
        except json.JSONDecodeError:
            raise ConfigError("成员列表格式不对") from None

    async def upload_stickers(request):
        reader = await request.multipart()
        label, members = "", []
        added = 0
        async for part in reader:
            if part.name == "label":
                label = (await part.text()).strip()
            elif part.name == "members":  # sent before the files
                members = members_field(await part.text())
            elif part.name == "file" and part.filename:
                filename = unquote(part.filename)  # some clients percent-encode non-ASCII names
                data = await part.read(decode=False)
                runtime.stickers.add(filename, bytes(data), label or Path(filename).stem, members=members)
                added += 1
        if not added:
            raise ConfigError("没有收到文件")
        return stickers_response(added=added)

    async def replace_sticker_file(request):
        reader = await request.multipart()
        async for part in reader:
            if part.name == "file" and part.filename:
                data = await part.read(decode=False)
                runtime.stickers.replace_file(request.match_info["sid"], unquote(part.filename), bytes(data))
                return stickers_response()
        raise ConfigError("没有收到文件")

    async def update_sticker(request):
        runtime.stickers.update(request.match_info["sid"], await request.json())
        return stickers_response()

    async def delete_sticker(request):
        runtime.stickers.delete(request.match_info["sid"])
        return stickers_response()

    async def import_set(request):
        body = await request.json()
        added = await runtime.import_sticker_set(str(body.get("set", "")))
        return web.json_response({"added": added, "stickers": stickers(), "sticker_prompt": runtime.sticker_prompt()})

    async def sticker_file(request):
        s = runtime.stickers.get(request.match_info["sid"])
        return web.FileResponse(s.path, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})

    def kaomoji_response(**extra):
        runtime.refresh_prompts()
        return web.json_response({**extra, "kaomoji": kaomoji()})

    async def add_kaomoji(request):
        """One face ({text, mood}), several pasted lines ({lines}), or the starter set ({starter: true})."""
        body = await request.json()
        members = body.get("members") or []
        if body.get("starter"):
            return kaomoji_response(added=runtime.kaomoji.add_many(KAOMOJI_STARTER, members))
        if "lines" in body:
            return kaomoji_response(added=runtime.kaomoji.add_many(parse_lines(str(body["lines"])), members))
        runtime.kaomoji.add(str(body.get("text", "")), str(body.get("mood", "")), members)
        return kaomoji_response(added=1)

    async def update_kaomoji(request):
        runtime.kaomoji.update(request.match_info["kid"], await request.json())
        return kaomoji_response()

    async def delete_kaomoji(request):
        runtime.kaomoji.delete(request.match_info["kid"])
        return kaomoji_response()

    def memory_response():
        runtime.refresh_prompts()
        return web.json_response({"memory": memory(), "status": runtime.status()})

    async def add_memory(request):
        body = await request.json()
        if runtime.memory.add(str(body.get("text", "")), str(body.get("scope") or "all")) is None:
            raise ConfigError("已经有一条一样的记忆了")
        return memory_response()

    async def consolidate_memory(request):
        body = await request.json() if request.can_read_body else {}
        added = await runtime.consolidate_memory_now(body.get("day") or None)
        runtime.refresh_prompts()
        return web.json_response({"memory": memory(), "status": runtime.status(), "added": len(added)})

    async def update_memory(request):
        runtime.memory.update(request.match_info["fid"], await request.json())
        return memory_response()

    async def delete_memory(request):
        runtime.memory.delete(request.match_info["fid"])
        return memory_response()

    def memes_response():
        runtime.refresh_prompts()
        return web.json_response({"memes": memes()})

    async def add_meme(request):
        body = await request.json()
        if runtime.memes.add(str(body.get("text", "")), str(body.get("url", "")), "你") is None:
            raise ConfigError("梗库里已经有这条了")
        return memes_response()

    async def update_meme(request):
        runtime.memes.update(request.match_info["mid"], await request.json())
        return memes_response()

    async def delete_meme(request):
        runtime.memes.delete(request.match_info["mid"])
        return memes_response()

    async def list_tool(request):
        body = await request.json()
        tools = await runtime.list_tool(body.get("tool") or {}, body.get("env") or {}, body.get("headers") or {})
        return web.json_response({"tools": tools})

    async def use_feedback(request):
        await runtime.use_feedback(request.match_info["fid"])
        return web.json_response({"feedback": feedback(), "settings": runtime.settings})

    async def ignore_feedback(request):
        runtime.feedback.mark(request.match_info["fid"], "ignored")
        return web.json_response({"feedback": feedback()})

    async def delete_feedback(request):
        runtime.feedback.delete(request.match_info["fid"])
        runtime.refresh_prompts()
        return web.json_response({"feedback": feedback()})

    def game_memories_response():
        runtime.refresh_prompts()
        return web.json_response({"game_memories": game_memories()})

    async def list_game_memories(request):
        data = game_memories()
        member = request.query.get("member")
        if member:
            data["items"] = [m for m in data["items"] if m["member"] == member]
        return web.json_response({"game_memories": data})

    async def set_game_memory_status(request):
        runtime.game_memories.set_status(request.match_info["mid"], request.match_info["status"])
        return game_memories_response()

    async def edit_game_memory(request):
        runtime.game_memories.edit(request.match_info["mid"], (await request.json()).get("story", ""))
        return game_memories_response()

    async def delete_game_memory(request):
        runtime.game_memories.delete(request.match_info["mid"])
        return game_memories_response()

    async def clear_game_opinion(request):
        runtime.game_memories.clear_opinion(request.match_info["member"], request.match_info["subject"])
        return game_memories_response()

    def followups_response():
        runtime.refresh_prompts()
        return web.json_response({"followups": followups()})

    async def add_followup(request):
        from datetime import datetime

        from ..followups import parse_when

        body = await request.json()
        when = parse_when(str(body.get("due", "")), datetime.now())
        if when is None:
            raise ConfigError("时间要像 2026-10-27 18:00 这样，而且不能是过去的时间")
        chat = runtime.proactive_chat()
        if not chat:
            raise ConfigError("还不知道是哪个群，先在群里聊几句")
        if runtime.followups.add(str(body.get("text", "")), when, chat, "") is None:
            raise ConfigError("已经记过这件事了")
        return followups_response()

    async def update_followup(request):
        runtime.followups.update(request.match_info["fid"], await request.json())
        return followups_response()

    async def delete_followup(request):
        runtime.followups.delete(request.match_info["fid"])
        return followups_response()

    async def write_chronicle(request):
        body = await request.json() if request.can_read_body else {}
        added = await runtime.write_chronicle_now(str(body.get("day") or "") or None)
        return web.json_response({"added": len(added), "chronicle": chronicle(), "memes": memes()})

    async def delete_moment(request):
        runtime.chronicle.delete(request.match_info["mid"])
        runtime.refresh_prompts()
        return web.json_response({"chronicle": chronicle()})

    async def list_chats(request):
        return web.json_response({"chats": runtime.chat_overview()})

    async def chat_detail(request):
        return web.json_response(runtime.chat_detail(request.match_info["key"]))

    async def reset_chat(request):
        await runtime.conversation(request.match_info["key"]).reset()
        return web.json_response(runtime.chat_detail(request.match_info["key"]))

    async def reset_member(request):
        key = request.match_info["key"]
        await runtime.reset_member_context(key, request.match_info["pid"])
        return web.json_response(runtime.chat_detail(key))

    async def delete_history(request):
        key = request.match_info["key"]
        runtime.delete_history_entry(key, request.match_info["pid"], int(request.match_info["index"]))
        return web.json_response(runtime.chat_detail(key))

    app = web.Application(middlewares=[guard], client_max_size=100 * 1024 * 1024)
    app.router.add_get("/api/life", get_life)
    app.router.add_get("/api/home", get_home)
    app.router.add_get("/api/home/diary/{pid}", home_diary)
    app.router.add_put("/api/home/looks/{pid}", home_look)
    app.router.add_post("/api/home/looks/{pid}/choose", home_choose_look)
    app.router.add_post("/api/home/types", new_type)
    app.router.add_put("/api/home/types/{tid:mine/[a-z0-9]+}", edit_type)
    app.router.add_delete("/api/home/types/{tid:mine/[a-z0-9]+}", remove_type)
    app.router.add_post("/api/home/pieces", new_piece)
    app.router.add_put("/api/home/pieces/{pid}", edit_piece)
    app.router.add_delete("/api/home/pieces/{pid}", remove_piece)
    app.router.add_post("/api/home/rooms", new_room)
    app.router.add_put("/api/home/rooms/{rid}", edit_room)
    app.router.add_delete("/api/home/rooms/{rid}", remove_room)
    app.router.add_post("/api/home/doors", new_door)
    app.router.add_delete("/api/home/doors/{did}", remove_door)
    app.router.add_post("/api/home/floors", new_floor)
    app.router.add_put(r"/api/home/floors/{level:\d+}", edit_floor)
    app.router.add_delete(r"/api/home/floors/{level:\d+}", remove_floor)
    app.router.add_post("/api/home/models", upload_model)
    app.router.add_get("/api/home/models/{name}", get_model)
    app.router.add_post("/api/home/images", upload_image)
    app.router.add_get("/api/home/images/{name}", get_image)
    app.router.add_get('/api/games', get_games)
    app.router.add_put('/api/games', update_games)
    app.router.add_post('/api/games/stop', stop_game)
    app.router.add_post('/api/games/{pid}/start', start_game)
    app.router.add_get('/api/games/runs/{rid}', game_trace)
    app.router.add_get("/api/life/artifacts/{aid}", life_artifact)
    app.router.add_post("/api/life/{pid}/step", step_life)
    app.router.add_post("/api/memory", add_memory)
    app.router.add_post("/api/memory/consolidate", consolidate_memory)
    app.router.add_put("/api/memory/{fid}", update_memory)
    app.router.add_delete("/api/memory/{fid}", delete_memory)
    app.router.add_post("/api/memes", add_meme)
    app.router.add_put("/api/memes/{mid}", update_meme)
    app.router.add_delete("/api/memes/{mid}", delete_meme)
    app.router.add_post("/api/tools/list", list_tool)
    app.router.add_post("/api/feedback/{fid}/use", use_feedback)
    app.router.add_post("/api/feedback/{fid}/ignore", ignore_feedback)
    app.router.add_delete("/api/feedback/{fid}", delete_feedback)
    app.router.add_get("/api/game-memories", list_game_memories)
    app.router.add_post("/api/game-memories/{mid}/{status:pinned|hidden|active}", set_game_memory_status)
    app.router.add_put("/api/game-memories/{mid}", edit_game_memory)
    app.router.add_delete("/api/game-memories/{mid}", delete_game_memory)
    app.router.add_delete("/api/game-opinions/{member}/{subject}", clear_game_opinion)
    app.router.add_post("/api/followups", add_followup)
    app.router.add_put("/api/followups/{fid}", update_followup)
    app.router.add_delete("/api/followups/{fid}", delete_followup)
    app.router.add_post("/api/chronicle/write", write_chronicle)
    app.router.add_delete("/api/chronicle/{mid}", delete_moment)
    app.router.add_get("/api/chats", list_chats)
    app.router.add_get("/api/chats/{key}", chat_detail)
    app.router.add_post("/api/chats/{key}/reset", reset_chat)
    app.router.add_post("/api/chats/{key}/members/{pid}/reset", reset_member)
    app.router.add_delete("/api/chats/{key}/members/{pid}/history/{index:\\d+}", delete_history)
    app.router.add_get("/", index)
    app.router.add_static("/static/", STATIC)
    app.router.add_get("/api/state", get_state)
    app.router.add_get("/api/status", get_status)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_put("/api/secrets", put_secrets)
    app.router.add_post("/api/preview", preview)
    app.router.add_post("/api/members/{pid}/test", test_member)
    app.router.add_post("/api/members/{pid}/proactive", try_proactive)
    app.router.add_delete("/api/events", clear_events)
    app.router.add_post("/api/members/{pid}/browse", try_browse)
    app.router.add_get("/api/journal", get_journal)
    app.router.add_delete("/api/journal/{eid}", delete_journal_entry)
    app.router.add_get("/api/members/{pid}/models", member_models)
    app.router.add_post("/api/conversations/{key}/{action:stop|reset}", room_action)
    app.router.add_post("/api/stickers", upload_stickers)
    app.router.add_post("/api/stickers/import", import_set)
    app.router.add_put("/api/stickers/{sid}", update_sticker)
    app.router.add_delete("/api/stickers/{sid}", delete_sticker)
    app.router.add_get("/api/stickers/{sid}/file", sticker_file)
    app.router.add_put("/api/stickers/{sid}/file", replace_sticker_file)
    app.router.add_post("/api/kaomoji", add_kaomoji)
    app.router.add_put("/api/kaomoji/{kid}", update_kaomoji)
    app.router.add_delete("/api/kaomoji/{kid}", delete_kaomoji)
    return app


async def start_web(runtime: Runtime, port: int) -> tuple[web.AppRunner, str]:
    token = runtime.store.web_token()
    runner = web.AppRunner(make_app(runtime, token, port), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise ConfigError(f"管理页面无法监听端口 {port}（{e}），可以用 --port 换一个端口。") from None
    return runner, f"http://127.0.0.1:{port}/?token={token}"
