"""MCP tools: settings, who gets what, each CLI's per-run configuration, listing a server's tools."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest
from test_web import admin, login  # noqa: F401  (fixture)

from roundtable.config import ConfigError, ParticipantConfig
from roundtable.participants import SessionStore
from roundtable.participants.claude_cli import ClaudeCLI
from roundtable.participants.codex_cli import CodexCLI
from roundtable.participants.grok_cli import GrokCLI
from roundtable.prompts import build_browse_prompt
from roundtable.runtime import Runtime
from roundtable.settings import (
    SettingsStore, apply_secret_changes, build_config, masked_secrets, normalize_settings, prune_secrets,
)
from roundtable.tools import (
    ToolServer, claude_allowed, claude_config, codex_overrides, grok_config, grok_rules, list_tools,
    normalize_tool, tools_for,
)

MEMBERS = [
    {"id": "claude", "name": "Claude", "kind": "claude_cli"},
    {"id": "codex", "name": "Codex", "kind": "codex_cli", "tool_notes": {"video": "手工、修东西", "gone": "x"}},
    {"id": "grok", "name": "Grok", "kind": "grok_cli"},
    {"id": "deepseek", "name": "DeepSeek", "kind": "api", "provider": "deepseek"},
]
VIDEO = {"id": "video", "name": "看视频", "description": "搜视频、看视频", "command": "uv",
         "args": ["run", "--script", "video.py"], "allowed": ["watch_video", "search_videos"],
         "members": ["codex", "grok", "deepseek", "nobody"]}
XWEB = {"id": "x-web", "name": "刷 X", "transport": "sse", "url": "https://mcp.example.com/sse",
        "allowed": ["search_posts"], "members": ["codex", "grok"]}


def test_tool_settings_are_checked():
    assert normalize_tool(VIDEO)["timeout"] == 600
    for bad, why in [({"id": "Bad_ID", "command": "x"}, "ID"), ({"id": "a", "transport": "ftp"}, "连接方式"),
                     ({"id": "a"}, "启动命令"), ({"id": "a", "transport": "http", "url": "ftp://x"}, "http")]:
        with pytest.raises(ConfigError, match=why):
            normalize_tool(bad)
    s = normalize_settings({"participants": MEMBERS, "tools": [VIDEO, XWEB]})
    assert s["tools"][0]["members"] == ["codex", "deepseek", "grok"]  # unknown members dropped
    assert next(p for p in s["participants"] if p["id"] == "codex")["tool_notes"] == {"video": "手工、修东西"}
    with pytest.raises(ConfigError, match="重复"):
        normalize_settings({"tools": [VIDEO, VIDEO]})


def test_secrets_for_tools(tmp_path):
    secrets = apply_secret_changes({}, {"tools": {"x-web": {"headers": {"Authorization": "Bearer abcdefghijk"}},
                                                  "video": {"env": {"VIDEO_PROXY": "http://127.0.0.1:7890"}}}},
                                   set(), {"video", "x-web"})
    assert secrets["tools"]["x-web"]["headers"]["Authorization"] == "Bearer abcdefghijk"
    assert masked_secrets(secrets)["tools"]["x-web"]["headers"]["Authorization"] == "…hijk"
    secrets = apply_secret_changes(secrets, {"tools": {"video": {"env": {"VIDEO_PROXY": None}}}}, set(), {"video", "x-web"})
    assert secrets["tools"]["video"]["env"] == {}
    with pytest.raises(ConfigError):
        apply_secret_changes(secrets, {"tools": {"nope": {"env": {"A": "b"}}}}, set(), {"video"})
    assert set(prune_secrets(secrets, set(), {"video"})["tools"]) == {"video"}
    config = build_config(normalize_settings({"participants": MEMBERS, "tools": [VIDEO, XWEB]}), secrets, tmp_path)
    assert next(t for t in config.tools if t.id == "x-web").headers == {"Authorization": "Bearer abcdefghijk"}


def test_who_can_use_what(tmp_path):
    config = build_config(normalize_settings({"participants": MEMBERS, "tools": [VIDEO, XWEB]}), {}, tmp_path)
    pcs = {p.id: p for p in config.participants}
    assert [t.id for t in tools_for(pcs["codex"], config.tools)] == ["video"]  # Codex has no SSE client
    assert [t.id for t in tools_for(pcs["grok"], config.tools)] == ["video", "x-web"]
    assert tools_for(pcs["deepseek"], config.tools) == []  # API members: not yet
    assert tools_for(pcs["claude"], config.tools) == []  # not given any
    config.tools[0].allowed = []
    assert tools_for(pcs["grok"], config.tools)[0].id == "x-web"  # nothing allowed = not offered


def servers():
    return [ToolServer(id="video", name="看视频", command="uv", args=["run", "v.py"], allowed=["watch_video"],
                       env={"VIDEO_PROXY": "http://p"}, timeout=900),
            ToolServer(id="x-web", name="刷 X", transport="http", url="https://x/mcp", allowed=["search_posts"],
                       headers={"Authorization": "Bearer t"})]


def test_claude_and_codex_configuration():
    assert claude_config(servers()) == {"mcpServers": {
        "video": {"command": "uv", "args": ["run", "v.py"], "env": {"VIDEO_PROXY": "http://p"}},
        "x-web": {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}}}}
    assert claude_allowed(servers()) == ["mcp__video__watch_video", "mcp__x-web__search_posts"]
    args = codex_overrides(servers())
    values = [args[i + 1] for i in range(0, len(args), 2)]
    assert 'mcp_servers.video.command="uv"' in values
    assert 'mcp_servers.video.enabled_tools=["watch_video"]' in values
    assert 'mcp_servers.video.default_tools_approval_mode="approve"' in values
    assert 'mcp_servers.x-web.http_headers={"Authorization" = "Bearer t"}' in values
    assert 'mcp_servers.video.tool_timeout_sec=900' in values


def test_grok_configuration():
    text = grok_config(servers())
    assert '[mcp_servers.video]\ncommand = "uv"' in text and '[mcp_servers.video.env]\n"VIDEO_PROXY" = "http://p"' in text
    assert 'url = "https://x/mcp"' in text and '"Authorization" = "Bearer t"' in text
    rules = grok_rules(servers())
    assert "MCPTool(video__watch_video)" in rules and rules[-4:] == ["--deny", "Read", "--deny", "Edit"]


def make(cls, kind, tmp_path, **cfg):
    pc = ParticipantConfig(id=kind.split("_")[0], name="X", kind=kind, command=[kind], **cfg)
    p = cls(pc, SessionStore(tmp_path / "s.json"), tmp_path / kind, timeout=30)
    p.set_system_prompt("SYSTEM")
    return p


def test_claude_gets_only_its_tools(tmp_path):
    claude = make(ClaudeCLI, "claude_cli", tmp_path, web_search=True)
    assert "--mcp-config" not in claude.build_command(None)
    claude.tools = servers()[:1]
    cmd = claude.build_command(None)
    config = json.loads((tmp_path / "claude_cli" / "mcp.json").read_text(encoding="utf-8"))
    assert cmd[cmd.index("--mcp-config") + 1].endswith("mcp.json") and "video" in config["mcpServers"]
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,mcp__video__watch_video"
    assert "--strict-mcp-config" in cmd and cmd[cmd.index("--tools") + 1] == "WebSearch"


def test_codex_keeps_code_mode_only_for_tools(tmp_path):
    codex = make(CodexCLI, "codex_cli", tmp_path)
    assert "code_mode_host" in codex.build_command(None)
    codex.tools = servers()[:1]
    cmd = codex.build_command(None)
    assert "code_mode_host" not in cmd and 'mcp_servers.video.enabled_tools=["watch_video"]' in cmd
    assert "shell_tool" in cmd  # the rest stays off


def test_grok_trusts_only_with_tools(tmp_path):
    grok = make(GrokCLI, "grok_cli", tmp_path)
    assert grok.env["GROK_CLAUDE_MCPS_ENABLED"] == "0" and grok.env["GROK_CLAUDE_SKILLS_ENABLED"] == "0"
    cmd = grok.build_command("p.txt", None)
    assert "--trust" not in cmd and "use_tool" in cmd[cmd.index("--disallowed-tools") + 1]
    grok.tools = servers()[:1]
    cmd = grok.build_command("p.txt", None)
    config = tmp_path / "grok_cli" / ".grok" / "config.toml"
    assert config.exists() and "--trust" in cmd and "MCPTool(video__watch_video)" in cmd
    assert "use_tool" not in cmd[cmd.index("--disallowed-tools") + 1]
    grok.tools = []
    grok.build_command("p.txt", None)
    assert not config.exists()  # a removed tool leaves nothing behind


def test_prompts_mention_tools_and_preferences(tmp_path):
    rt = Runtime(SettingsStore(tmp_path))
    rt.config = build_config(normalize_settings({"participants": MEMBERS, "tools": [VIDEO]}), {}, tmp_path)
    pcs = {p.id: p for p in rt.config.participants}
    codex = rt.system_prompt_for(pcs["codex"])
    assert "你的工具" in codex and "- 看视频：搜视频、看视频（你平时爱看：手工、修东西）" in codex
    assert "看视频" in rt.system_prompt_for(pcs["claude"]).split("朋友们各自会什么")[1].split("自己做不到")[0]
    browse = build_browse_prompt("", False, [], tools=[("看视频", "手工")], can_search=False)
    assert "用你的工具去网上" in browse and "看视频（你爱看：手工）" in browse
    focused = build_browse_prompt("修东西", False, [], tools=[("看视频", "手工")], focus=("看视频", "手工"))
    assert "这次专门用你的工具「看视频」逛，按你的偏好：手工" in focused
    assert "用搜索工具" not in focused and "请先真的用「看视频」看几次" in focused


SERVER = textwrap.dedent('''
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("demo")

    @server.tool()
    def secret_word() -> str:
        """Today's secret word."""
        return "紫色长颈鹿"

    server.run()
''')


async def test_list_tools_from_a_real_server(tmp_path):
    script = tmp_path / "demo.py"
    script.write_text(SERVER, encoding="utf-8")
    tools = await list_tools(ToolServer(id="demo", name="演示", command=sys.executable, args=[str(script)]))
    assert tools == [{"name": "secret_word", "description": "Today's secret word."}]
    with pytest.raises(ConfigError, match="连不上"):
        await list_tools(ToolServer(id="bad", name="坏的", command=sys.executable, args=[str(tmp_path / "missing.py")]))


async def test_admin_lists_tools(admin, tmp_path):  # noqa: F811
    await login(admin)
    script = tmp_path / "demo.py"
    script.write_text(SERVER, encoding="utf-8")
    body = {"tool": {"id": "demo", "name": "演示", "command": sys.executable, "args": [str(script)]}}
    resp = await admin.post("/api/tools/list", json=body, headers={"Origin": admin.origin})
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["tools"][0]["name"] == "secret_word"
    state = await (await admin.get("/api/state")).json()
    assert state["meta"]["video_tool"].endswith("video.py") and state["settings"]["tools"] == []
