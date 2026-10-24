from __future__ import annotations

import pytest
from aiohttp import web

from roundtable.config import ConfigError, ParticipantConfig
from roundtable.participants import ParticipantError
from roundtable.participants.api import OpenAICompatAPI


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def fake_api():
    """A minimal OpenAI-compatible server that echoes how many messages it got."""
    state = {"requests": [], "fail": None}

    async def chat(request: web.Request):
        if request.headers.get("Authorization") != "Bearer sk-test":
            return web.json_response({"error": {"message": "bad key"}}, status=401)
        body = await request.json()
        state["requests"].append(body)
        if state["fail"]:
            return web.json_response({"error": {"message": state["fail"]}}, status=429)
        n = len(body["messages"])
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": f"reply {n}"}}]})

    async def models(request: web.Request):
        return web.json_response({"data": [{"id": "m-b"}, {"id": "m-a"}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    state["url"] = f"http://127.0.0.1:{port}/v1"
    yield state
    await runner.cleanup()


def make_api(tmp_path, store, url, **overrides):
    fields = dict(id="ds", name="DeepSeek", kind="api", base_url=url, api_key="sk-test", model="m-a")
    fields.update(overrides)
    p = OpenAICompatAPI(ParticipantConfig(**fields), store, tmp_path / "ds", timeout=5)
    p.set_system_prompt("SYSTEM")
    return p


async def test_reply_sends_system_prompt_and_keeps_history(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"], params={"temperature": 0.5})

    assert await p.reply("chat", "first") == "reply 2"
    assert await p.reply("chat", "second") == "reply 4"

    last = fake_api["requests"][-1]
    assert last["model"] == "m-a"
    assert last["temperature"] == 0.5
    assert [m["role"] for m in last["messages"]] == ["system", "user", "assistant", "user"]
    assert last["messages"][0]["content"] == "SYSTEM"
    assert last["messages"][1]["content"] == "first"
    assert last["messages"][2]["content"] == "reply 2"

    # History survives a restart (new instance, same work dir).
    again = make_api(tmp_path, store, fake_api["url"])
    assert len(again.load_history("chat")) == 4


async def test_history_is_trimmed_to_start_on_a_user_turn(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"], max_history=3)
    for i in range(4):
        await p.reply("chat", f"msg {i}")
    history = p.load_history("chat")
    assert len(history) <= 3
    assert history[0]["role"] == "user"


async def test_chats_have_separate_history_and_reset(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"])
    await p.reply("a", "hi")
    await p.reply("b", "hi")
    p.reset("a")
    assert p.load_history("a") == []
    assert len(p.load_history("b")) == 2


async def test_http_error_becomes_participant_error(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"])
    fake_api["fail"] = "rate limited"
    with pytest.raises(ParticipantError, match="429.*rate limited"):
        await p.reply("chat", "hi")
    # A failed call does not leave a dangling user turn behind.
    assert p.load_history("chat") == []


async def test_bad_key_is_reported(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"], api_key="wrong")
    with pytest.raises(ParticipantError, match="401.*bad key"):
        await p.reply("chat", "hi")


async def test_list_models(tmp_path, store, fake_api):
    p = make_api(tmp_path, store, fake_api["url"])
    assert await p.list_models() == ["m-a", "m-b"]


def test_provider_preset_and_env_key(tmp_path, store, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "sk-env")
    cfg = ParticipantConfig(id="g", name="Grok", kind="api", provider="grok", model="grok-x")
    p = OpenAICompatAPI(cfg, store, tmp_path / "g", timeout=5)
    assert p.base_url == "https://api.x.ai/v1"
    assert p.api_key == "sk-env"


def test_missing_key_is_a_config_error(tmp_path, store, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg = ParticipantConfig(id="d", name="DeepSeek", kind="api", provider="deepseek", model="x")
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        OpenAICompatAPI(cfg, store, tmp_path / "d", timeout=5)


def test_unknown_provider_without_base_url(tmp_path, store):
    cfg = ParticipantConfig(id="x", name="X", kind="api", provider="nope", api_key="k", model="m")
    with pytest.raises(ConfigError, match="base_url"):
        OpenAICompatAPI(cfg, store, tmp_path / "x", timeout=5)
