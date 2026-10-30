from __future__ import annotations

import json
import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

from nookling.runtime import Runtime
from nookling.settings import SettingsStore
from nookling.transcript import Message
from nookling.web.server import COOKIE, make_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def admin(tmp_path):
    store = SettingsStore(tmp_path)
    runtime = Runtime(store)
    token = store.web_token()
    port = free_port()
    server = TestServer(make_app(runtime, token, port), host="127.0.0.1", port=port)
    client = TestClient(server)
    await client.start_server()
    client.origin = f"http://127.0.0.1:{port}"
    client.token = token
    client.runtime = runtime
    client.data_dir = tmp_path
    yield client
    await client.close()


async def login(client):
    resp = await client.get(f"/?token={client.token}", allow_redirects=False)
    assert resp.status == 302
    assert COOKIE in resp.cookies


async def test_requires_token(admin):
    assert (await admin.get("/api/state")).status == 401
    assert (await admin.get("/")).status == 401
    assert (await admin.get("/?token=wrong", allow_redirects=False)).status == 401


async def test_token_link_logs_in(admin):
    await login(admin)
    resp = await admin.get("/api/state")
    assert resp.status == 200
    data = await resp.json()
    assert data["settings"]["participants"] == []
    assert "default_prompt" in data["meta"]


async def test_rejects_foreign_host(admin):
    await login(admin)
    resp = await admin.get("/api/state", headers={"Host": "evil.example:80"})
    assert resp.status == 403


async def test_writes_need_local_origin(admin):
    await login(admin)
    body = {"participants": []}
    assert (await admin.put("/api/settings", json=body)).status == 403
    assert (await admin.put("/api/settings", json=body, headers={"Origin": "http://evil.example"})).status == 403
    assert (await admin.put("/api/settings", json=body, headers={"Origin": admin.origin})).status == 200


async def test_secrets_are_saved_but_never_returned(admin):
    await login(admin)
    headers = {"Origin": admin.origin}
    member = {"id": "ds", "name": "DeepSeek", "kind": "api", "provider": "deepseek", "model": "m", "enabled": False}
    resp = await admin.put("/api/settings", json={"participants": [member]}, headers=headers)
    assert resp.status == 200

    key = "sk-very-secret-key-9876"
    resp = await admin.put("/api/secrets", json={"api_keys": {"ds": key}}, headers=headers)
    assert resp.status == 200
    text = await resp.text()
    assert key not in text
    assert json.loads(text)["secrets"]["api_keys"]["ds"] == "…9876"

    assert key in (admin.data_dir / "secrets.json").read_text(encoding="utf-8")
    assert key not in (admin.data_dir / "settings.json").read_text(encoding="utf-8")
    assert key not in await (await admin.get("/api/state")).text()

    # Removing the member removes its key too.
    await admin.put("/api/settings", json={"participants": []}, headers=headers)
    assert key not in (admin.data_dir / "secrets.json").read_text(encoding="utf-8")


async def test_invalid_settings_are_rejected_with_a_message(admin):
    await login(admin)
    resp = await admin.put(
        "/api/settings",
        json={"participants": [{"id": "Bad Id", "name": "x", "kind": "api"}]},
        headers={"Origin": admin.origin},
    )
    assert resp.status == 400
    assert "ID" in (await resp.json())["error"]


async def test_sticker_upload_edit_delete(admin):
    from aiohttp import FormData

    await login(admin)
    headers = {"Origin": admin.origin}
    form = FormData()
    form.add_field("label", "")
    form.add_field("file", b"\x89PNG\r\n\x1a\nxx", filename="狗头.png", content_type="image/png")
    resp = await admin.post("/api/stickers", data=form, headers=headers)
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["added"] == 1
    sticker = data["stickers"][0]
    assert sticker["label"] == "狗头"
    assert "狗头" in data["sticker_prompt"]

    resp = await admin.get(f"/api/stickers/{sticker['id']}/file")
    assert resp.status == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"

    resp = await admin.put(f"/api/stickers/{sticker['id']}", json={"label": "无语"}, headers=headers)
    assert (await resp.json())["stickers"][0]["label"] == "无语"

    resp = await admin.delete(f"/api/stickers/{sticker['id']}", headers=headers)
    assert (await resp.json())["stickers"] == []


async def test_sticker_upload_rejects_other_files(admin):
    from aiohttp import FormData

    await login(admin)
    form = FormData()
    form.add_field("file", b"MZ", filename="virus.exe")
    resp = await admin.post("/api/stickers", data=form, headers={"Origin": admin.origin})
    assert resp.status == 400


async def test_memory_crud(admin):
    await login(admin)
    headers = {"Origin": admin.origin}
    resp = await admin.post("/api/memory", json={"text": "主人在杭州", "scope": "all"}, headers=headers)
    assert resp.status == 200
    fact = (await resp.json())["memory"][0]
    assert (await admin.post("/api/memory", json={"text": "主人在杭州"}, headers=headers)).status == 400

    resp = await admin.put(f"/api/memory/{fact['id']}", json={"enabled": False}, headers=headers)
    assert (await resp.json())["memory"][0]["enabled"] is False
    resp = await admin.delete(f"/api/memory/{fact['id']}", headers=headers)
    assert (await resp.json())["memory"] == []


async def test_chat_listing_and_reset(admin):
    await login(admin)
    room = admin.runtime.conversation("-100")
    room.messages.clear()
    room._append(Message("user", "me", "hi", (), 1.0))
    chats = (await (await admin.get("/api/chats")).json())["chats"]
    assert [c["key"] for c in chats] == ["-100"]
    detail = await (await admin.get("/api/chats/-100")).json()
    assert detail["messages"][0]["text"] == "hi"
    resp = await admin.post("/api/chats/-100/reset", headers={"Origin": admin.origin})
    assert (await resp.json())["messages"] == []


async def test_preview_uses_unsaved_draft(admin):
    await login(admin)
    draft = {
        "prompts": {"scene": "你们是邻居", "no_coding": True},
        "participants": [{"id": "a", "name": "阿杰", "kind": "claude_cli", "persona": "嘴毒心软"}],
    }
    resp = await admin.post("/api/preview", json={"settings": draft, "member": "a"}, headers={"Origin": admin.origin})
    prompt = (await resp.json())["prompt"]
    assert prompt.startswith("你在这个群里的角色：阿杰")
    assert "嘴毒心软" in prompt and "你们是邻居" in prompt and "不写代码" in prompt
    # Nothing was saved.
    assert (await (await admin.get("/api/state")).json())["settings"]["participants"] == []


async def test_member_without_key_shows_error_in_status(admin):
    await login(admin)
    member = {"id": "ds", "name": "DeepSeek", "kind": "api", "provider": "deepseek", "model": "m"}
    resp = await admin.put("/api/settings", json={"participants": [member]}, headers={"Origin": admin.origin})
    status = (await resp.json())["status"]
    ds = next(m for m in status["members"] if m["id"] == "ds")
    assert ds["state"] == "error"
    assert "API key" in ds["message"]


async def test_followups_and_chronicle(admin):
    await login(admin)
    headers = {"Origin": admin.origin}
    body = {"text": "主人面试，问问结果", "due": "2026-10-27 18:00"}
    assert (await admin.post("/api/followups", json=body, headers=headers)).status == 400  # no group yet
    admin.runtime.proactive_chat = lambda: "-100"
    assert (await admin.post("/api/followups", json={**body, "due": "2020-01-01"}, headers=headers)).status == 400
    from datetime import datetime, timedelta

    body["due"] = f"{datetime.now() + timedelta(days=1):%Y-%m-%d} 18:00"
    resp = await admin.post("/api/followups", json=body, headers=headers)
    item = (await resp.json())["followups"][0]
    assert item["chat"] == "-100" and item["status"] == "pending"
    assert (await admin.post("/api/followups", json=body, headers=headers)).status == 400  # already noted
    resp = await admin.put(f"/api/followups/{item['id']}", json={"text": "主人面试结果"}, headers=headers)
    assert (await resp.json())["followups"][0]["text"] == "主人面试结果"
    resp = await admin.delete(f"/api/followups/{item['id']}", headers=headers)
    assert (await resp.json())["followups"] == []

    moment = admin.runtime.chronicle.record("2026-10-25", [{"title": "洗衣液咖啡", "what": "倒错了", "people": []}])[0]
    state = await (await admin.get("/api/state")).json()
    assert state["chronicle"][0]["title"] == "洗衣液咖啡" and state["followups"] == []
    resp = await admin.delete(f"/api/chronicle/{moment.id}", headers=headers)
    assert (await resp.json())["chronicle"] == []


async def test_group_upgrade_moves_everything(admin):
    rt = admin.runtime
    settings = {**rt.settings, "telegram": {**rt.settings["telegram"], "allowed_chat_ids": [-5]}, "proactive": {"chat_id": -5}}
    await rt.apply(settings)
    room = rt.conversation("-5")
    room._append(Message("user", "主人", "升级前说的", (), 1.0))
    rt.sessions.set("-5", "grok", "abc")
    history = admin.data_dir / "work" / "ds" / "history"
    history.mkdir(parents=True)
    (history / "-5.json").write_text("[]", encoding="utf-8")
    from datetime import datetime, timedelta

    item = rt.followups.add("问问面试", datetime.now() + timedelta(days=1), "-5", "grok")

    assert await rt.migrate_chat(-5, -1009)
    assert not await rt.migrate_chat(-5, -1009)  # Telegram tells both groups; once is enough
    assert rt.config.telegram.allowed_chat_ids == [-1009] and rt.config.proactive_chat_id == -1009
    assert [m.text for m in rt.conversation("-1009").messages] == ["升级前说的"]
    assert rt.sessions.get("-1009", "grok") == "abc" and rt.sessions.get("-5", "grok") is None
    assert (history / "-1009.json").exists() and item.chat == "-1009"
