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
from ..memory import LEARN_INSTRUCTIONS
from ..kaomoji import STARTER as KAOMOJI_STARTER, parse_lines
from ..stickers import KINDS as STICKER_KINDS

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
VIDEO_TOOL = Path(__file__).parent.parent / "mcp_servers" / "video.py"  # the built-in 看视频 server
READER_TOOL = Path(__file__).parent.parent / "mcp_servers" / "reader.py"
COOKIE = "roundtable_admin"

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
        if not _same(request.cookies.get(COOKIE, ""), token):
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

    def followups() -> list[dict]:
        return [asdict(f) for f in runtime.followups.items[-60:]]

    def chronicle() -> list[dict]:
        return [asdict(m) for m in runtime.chronicle.moments]

    def memes() -> list[dict]:
        return [asdict(m) for m in runtime.memes.items]

    def memory() -> list[dict]:
        return [asdict(f) for f in runtime.memory.items]

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

    async def step_life(request):
        return web.json_response(runtime.start_life(request.match_info["pid"]))

    async def delete_journal_entry(request):
        runtime.journal.delete(request.match_info["eid"])
        runtime.refresh_prompts()
        return web.json_response(journal())

    async def try_browse(request):
        added = await runtime.try_browse(request.match_info["pid"])
        return web.json_response({"added": added, **journal()})

    async def try_proactive(request):
        return web.json_response(await runtime.try_proactive(request.match_info["pid"]))

    async def test_member(request):
        return web.json_response(await runtime.test_participant(request.match_info["pid"]))

    async def member_models(request):
        return web.json_response({"models": await runtime.list_models(request.match_info["pid"])})

    async def room_action(request):
        room = runtime.rooms.get(request.match_info["key"])
        if room is None:
            return web.json_response({"error": "没有这个群"}, status=404)
        if request.match_info["action"] == "stop":
            await room.stop()
        else:
            await room.reset()
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
        await runtime.room(request.match_info["key"]).reset()
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
    app.router.add_post("/api/life/{pid}/step", step_life)
    app.router.add_post("/api/memory", add_memory)
    app.router.add_put("/api/memory/{fid}", update_memory)
    app.router.add_delete("/api/memory/{fid}", delete_memory)
    app.router.add_post("/api/memes", add_meme)
    app.router.add_put("/api/memes/{mid}", update_meme)
    app.router.add_delete("/api/memes/{mid}", delete_meme)
    app.router.add_post("/api/tools/list", list_tool)
    app.router.add_post("/api/feedback/{fid}/use", use_feedback)
    app.router.add_post("/api/feedback/{fid}/ignore", ignore_feedback)
    app.router.add_delete("/api/feedback/{fid}", delete_feedback)
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
    app.router.add_post("/api/members/{pid}/browse", try_browse)
    app.router.add_get("/api/journal", get_journal)
    app.router.add_delete("/api/journal/{eid}", delete_journal_entry)
    app.router.add_get("/api/members/{pid}/models", member_models)
    app.router.add_post("/api/rooms/{key}/{action:stop|reset}", room_action)
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
