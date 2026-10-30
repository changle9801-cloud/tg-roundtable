from __future__ import annotations

import pytest
from aiohttp import web

from nookling.config import ConfigError, ParticipantConfig
from nookling.participants import ParticipantError, participant_class
from nookling.participants.anthropic_api import AnthropicAPI as _AnthropicAPI
from nookling.participants.api import OpenAICompatAPI


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
async def fake_anthropic():
    """Just enough of the Messages API to exercise the participant."""
    state = {"requests": [], "headers": [], "reply": None}

    async def messages(request: web.Request):
        key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if key != "sk-ant-test":
            return web.json_response({"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}, status=401)
        body = await request.json()
        state["requests"].append(body)
        state["headers"].append(dict(request.headers))
        return web.json_response(state["reply"] or {
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": f"reply {len(body['messages'])}"},
            ],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    async def models(request: web.Request):
        return web.json_response({
            "data": [{"type": "model", "id": "claude-b", "display_name": "B", "created_at": "2026-01-01T00:00:00Z"},
                     {"type": "model", "id": "claude-a", "display_name": "A", "created_at": "2026-01-01T00:00:00Z"}],
            "has_more": False, "first_id": "claude-b", "last_id": "claude-a",
        })

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state["url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield state
    await runner.cleanup()


def make(tmp_path, store, url, **overrides):
    fields = dict(id="claude-api", name="Claude", kind="api", provider="anthropic",
                  base_url=url, api_key="sk-ant-test", model="claude-test", effort="low")
    fields.update(overrides)
    pc = ParticipantConfig(**fields)
    p = participant_class(pc)(pc, store, tmp_path / "c", timeout=10)
    p.set_system_prompt("SYSTEM")
    return p


def test_dispatch_by_provider(tmp_path, store):
    assert participant_class(ParticipantConfig(id="a", name="A", kind="api", provider="anthropic")) is _AnthropicAPI
    assert participant_class(ParticipantConfig(id="a", name="A", kind="api", provider="deepseek")) is OpenAICompatAPI


async def test_reply_sends_system_effort_and_keeps_history(tmp_path, store, fake_anthropic):
    p = make(tmp_path, store, fake_anthropic["url"])
    assert await p.reply("chat", "first") == "reply 1"
    assert await p.reply("chat", "second") == "reply 3"

    body = fake_anthropic["requests"][-1]
    assert body["system"] == "SYSTEM"
    assert body["output_config"] == {"effort": "low"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"] == "reply 1"  # text only, thinking blocks are not stored


def test_fallbacks_only_for_supported_models_on_first_party_api(tmp_path, store):
    assert make(tmp_path, store, "", model="claude-opus-5-5").uses_fallbacks()
    assert not make(tmp_path, store, "", model="claude-haiku-5-5").uses_fallbacks()
    assert not make(tmp_path, store, "http://proxy", model="claude-opus-5-5").uses_fallbacks()


async def test_refusal_becomes_error_and_is_not_stored(tmp_path, store, fake_anthropic):
    fake_anthropic["reply"] = {
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-test", "content": [],
        "stop_reason": "refusal", "stop_sequence": None,
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": None},
        "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    p = make(tmp_path, store, fake_anthropic["url"])
    with pytest.raises(ParticipantError, match="cyber"):
        await p.reply("chat", "hi")
    assert p.load_history("chat") == []


async def test_bad_key(tmp_path, store, fake_anthropic):
    p = make(tmp_path, store, fake_anthropic["url"], api_key="wrong")
    with pytest.raises(ParticipantError, match="API key"):
        await p.reply("chat", "hi")


async def test_list_models(tmp_path, store, fake_anthropic):
    p = make(tmp_path, store, fake_anthropic["url"])
    assert await p.list_models() == ["claude-a", "claude-b"]


def test_env_key(tmp_path, store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert make(tmp_path, store, "", api_key="").api_key == "sk-ant-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        make(tmp_path, store, "", api_key="")
