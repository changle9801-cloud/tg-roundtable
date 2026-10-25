import json

import pytest

from roundtable.participants import ParticipantError
from roundtable.participants.claude_cli import parse_output
from roundtable.participants.codex_cli import parse_events
from roundtable.participants.grok_cli import parse_output as parse_grok
from roundtable.prompts import clean_reply
from roundtable.telegram_bot import split_text


@pytest.mark.parametrize("raw", ["[PASS]", "PASS", " [pass] ", "[PASS]。", "", "Claude: [PASS]"])
def test_pass_variants(raw):
    assert clean_reply(raw, "Claude") is None


def test_clean_reply_strips_own_name_prefix():
    assert clean_reply("Claude：今天天气不错", "Claude") == "今天天气不错"
    assert clean_reply("我觉得 PASS 这个词挺好", "Claude") == "我觉得 PASS 这个词挺好"


def test_parse_claude_output():
    out = json.dumps({"type": "result", "is_error": False, "result": "hi", "session_id": "s1"})
    assert parse_output(out) == ("hi", "s1")


def test_parse_claude_error():
    out = json.dumps({"is_error": True, "result": "limit reached", "session_id": "s1"})
    with pytest.raises(ParticipantError, match="limit reached"):
        parse_output(out)


def test_parse_codex_events():
    lines = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
        {"type": "turn.completed", "usage": {}},
    ]
    out = "some warning\n" + "\n".join(json.dumps(x) for x in lines)
    assert parse_events(out) == ("hello", "t1")


def test_parse_codex_failure():
    out = json.dumps({"type": "turn.failed", "error": {"message": "usage limit"}})
    with pytest.raises(ParticipantError, match="usage limit"):
        parse_events(out)


def grok_stream(*events):
    return "update available\n" + "\n".join(json.dumps(e, ensure_ascii=False) for e in events)


def test_parse_grok_output():
    out = grok_stream(
        {"type": "text", "data": "你"}, {"type": "text", "data": "好"},
        {"type": "end", "stopReason": "end_turn", "sessionId": "g1"},
    )
    assert parse_grok(out) == ("你好", "g1")


def test_parse_grok_keeps_the_answer_after_a_search():
    out = grok_stream(
        {"type": "text", "data": "我去查一下。"},
        {"type": "tool_call", "toolName": "X search: {}", "rawInput": {"variant": "XSearch"}},
        {"type": "tool_call_update", "status": "completed"},
        {"type": "text", "data": "最新一条是……"},
        {"type": "end", "sessionId": "g2"},
    )
    assert parse_grok(out) == ("最新一条是……", "g2")


def test_grok_search_events():
    from roundtable.participants.grok_cli import describe_tool

    assert describe_tool({"type": "tool_call", "toolName": "X search: {}", "rawInput": {"variant": "XSearch"}}) == "正在 X 上搜索"
    assert describe_tool({"type": "tool_call", "toolName": "web_search"}) == "正在联网搜索"
    assert describe_tool({"type": "text", "data": "x"}) == ""


def test_parse_grok_error():
    out = json.dumps({"type": "error", "message": "Couldn't start session: not logged in"})
    with pytest.raises(ParticipantError, match="not logged in"):
        parse_grok(out)


def test_codex_runs_without_tools(tmp_path, store):
    from roundtable.config import ParticipantConfig
    from roundtable.participants.codex_cli import CodexCLI

    pc = ParticipantConfig(id="codex", name="Codex", kind="codex_cli", command=["codex"])
    codex = CodexCLI(pc, store, tmp_path, timeout=5)
    for session in (None, "thread-1"):
        cmd = codex.build_command(session)
        disabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--disable"}
        assert {"shell_tool", "unified_exec", "code_mode_host", "computer_use", "browser_use"} <= disabled
        assert 'sandbox_mode="read-only"' in cmd and 'web_search="disabled"' in cmd

    # With search on, search needs the code-mode host; commands and files stay off.
    codex.cfg.web_search = True
    cmd = codex.build_command(None)
    disabled = {cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--disable"}
    assert {"shell_tool", "unified_exec", "computer_use", "browser_use"} <= disabled
    assert "code_mode_host" not in disabled and 'web_search="live"' in cmd


def test_split_text():
    text = "a" * 3000 + "\n" + "b" * 3000
    assert split_text(text) == ["a" * 3000, "b" * 3000]
    assert split_text("x" * 9000) == ["x" * 4000, "x" * 4000, "x" * 1000]
