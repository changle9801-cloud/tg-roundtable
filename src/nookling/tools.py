"""External tools over MCP: servers the owner adds on the admin page and hands to members.

    settings.json  "tools": [{id, name, description, enabled, transport, command, args, url,
                              allowed, members, timeout}]
    secrets.json   "tools": {id: {"env": {...}, "headers": {...}}}
    member card    tool_notes: {tool id: what it likes to look at with that tool}

Only the tools ticked in ``allowed`` can be called, and only by the members in ``members``.
Each CLI gets the servers in its own per-run configuration, never in the owner's own
~/.claude, ~/.codex or ~/.grok settings. API members cannot use tools yet.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .config import ConfigError

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")  # no "_": the CLIs join server and tool with "__"
TRANSPORTS = ("stdio", "http", "sse")
# What each kind of member can use; Codex has no SSE client.
SUPPORTED = {"claude_cli": set(TRANSPORTS), "codex_cli": {"stdio", "http"}, "grok_cli": set(TRANSPORTS)}
LIST_TIMEOUT = 120  # the first start of an npx/uvx server may download packages


@dataclass
class ToolServer:
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    allowed: list[str] = field(default_factory=list)  # tool names members may call
    members: list[str] = field(default_factory=list)
    timeout: int = 600  # seconds per tool call; watching a video takes a while
    env: dict[str, str] = field(default_factory=dict)  # from secrets.json
    headers: dict[str, str] = field(default_factory=dict)  # from secrets.json


def normalize_tool(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("工具配置格式不对")
    tid = str(raw.get("id", "")).strip()
    if not ID_RE.match(tid):
        raise ConfigError(f"工具 ID {tid!r} 不合法：小写字母开头，只能用小写字母、数字和 -，最长 32 个字符")
    name = str(raw.get("name", "")).strip() or tid
    transport = str(raw.get("transport") or "stdio")
    if transport not in TRANSPORTS:
        raise ConfigError(f"工具 {name} 的连接方式只能是 {'、'.join(TRANSPORTS)}")
    args = raw.get("args") or []
    if isinstance(args, str):
        args = args.split()
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ConfigError(f"工具 {name} 的参数格式不对")
    out = {
        "id": tid,
        "name": name,
        "description": str(raw.get("description") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "transport": transport,
        "command": str(raw.get("command") or "").strip(),
        "args": [a for a in args if a != ""],
        "url": str(raw.get("url") or "").strip(),
        "allowed": sorted({str(t).strip() for t in raw.get("allowed") or [] if str(t).strip()}),
        "members": sorted({str(m).strip() for m in raw.get("members") or [] if str(m).strip()}),
    }
    try:
        out["timeout"] = max(10, min(int(raw.get("timeout") or 600), 3600))
    except (TypeError, ValueError):
        raise ConfigError(f"工具 {name} 的超时时间必须是数字") from None
    if transport == "stdio" and not out["command"]:
        raise ConfigError(f"工具 {name} 缺少启动命令")
    if transport != "stdio" and not re.match(r"^https?://", out["url"]):
        raise ConfigError(f"工具 {name} 的地址要以 http:// 或 https:// 开头")
    return out


def normalize_tools(raw, member_ids: set[str]) -> list[dict]:
    tools = [normalize_tool(t) for t in raw or []]
    ids = [t["id"] for t in tools]
    if len(set(ids)) != len(ids):
        raise ConfigError("工具 ID 不能重复")
    for t in tools:
        t["members"] = [m for m in t["members"] if m in member_ids]
    return tools


def build_tools(settings_tools: Sequence[dict], secrets: dict) -> list[ToolServer]:
    stored = secrets.get("tools") or {}
    return [
        ToolServer(**t, env=dict((stored.get(t["id"]) or {}).get("env") or {}),
                   headers=dict((stored.get(t["id"]) or {}).get("headers") or {}))
        for t in settings_tools
    ]


def tools_for(pc, servers: Sequence[ToolServer]) -> list[ToolServer]:
    """The servers a member can actually use: enabled, given to it, some tool allowed, transport supported."""
    kinds = SUPPORTED.get(pc.kind, set())
    return [s for s in servers if s.enabled and pc.id in s.members and s.allowed and s.transport in kinds]


# ---- per-CLI configuration ----

def claude_config(servers: Sequence[ToolServer]) -> dict:
    """``--mcp-config`` JSON for the Claude CLI."""
    out = {}
    for s in servers:
        if s.transport == "stdio":
            out[s.id] = {"command": s.command, "args": s.args, "env": s.env}
        else:
            out[s.id] = {"type": s.transport, "url": s.url, "headers": s.headers}
    return {"mcpServers": out}


def claude_allowed(servers: Sequence[ToolServer]) -> list[str]:
    return [f"mcp__{s.id}__{t}" for s in servers for t in s.allowed]


def _toml(value) -> str:
    # JSON strings, arrays and inline-table-free values are valid TOML.
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def codex_overrides(servers: Sequence[ToolServer]) -> list[str]:
    """``-c`` overrides for ``codex exec``: only the allowed tools, approved up front."""
    args: list[str] = []

    def put(server: str, key: str, value) -> None:
        args.extend(["-c", f"mcp_servers.{server}.{key}={_toml(value)}"])

    for s in servers:
        if s.transport == "stdio":
            put(s.id, "command", s.command)
            put(s.id, "args", s.args)
            if s.env:
                put(s.id, "env", s.env)
        else:
            put(s.id, "url", s.url)
            if s.headers:
                put(s.id, "http_headers", s.headers)
        put(s.id, "enabled_tools", s.allowed)
        put(s.id, "default_tools_approval_mode", "approve")  # nobody can approve in an exec run
        put(s.id, "tool_timeout_sec", s.timeout)
    return args


def grok_config(servers: Sequence[ToolServer]) -> str:
    """The project-level ``.grok/config.toml`` in the member's own work folder."""
    lines: list[str] = []
    for s in servers:
        lines.append(f"[mcp_servers.{s.id}]")
        if s.transport == "stdio":
            lines += [f"command = {_toml(s.command)}", f"args = {_toml(s.args)}"]
        else:
            lines.append(f"url = {_toml(s.url)}")
            if s.transport == "sse":
                lines.append('type = "sse"')
        lines += ["enabled = true", "expose_image_base64 = true", f"tool_timeout_sec = {s.timeout}"]
        for table, values in (("env", s.env), ("headers", s.headers)):
            if values:
                lines.append(f"[mcp_servers.{s.id}.{table}]")
                lines += [f"{json.dumps(k)} = {_toml(v)}" for k, v in values.items()]
        lines.append("")
    return "\n".join(lines)


def grok_rules(servers: Sequence[ToolServer]) -> list[str]:
    """Permission flags: only the allowed tools run; file reads and edits are refused outright."""
    args: list[str] = []
    for s in servers:
        for t in s.allowed:
            # Grok's model sometimes calls use_tool with the bare tool name.
            args += ["--allow", f"MCPTool({s.id}__{t})", "--allow", f"MCPTool({t})"]
    return args + ["--deny", "Read", "--deny", "Edit"]


# ---- talking to a server from the admin page ----

@asynccontextmanager
async def _session(server: ToolServer):
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    if server.transport == "stdio":
        params = StdioServerParameters(command=server.command, args=server.args, env={**os.environ, **server.env})
        transport = stdio_client(params)
    elif server.transport == "sse":
        transport = sse_client(server.url, headers=server.headers)
    else:
        transport = streamable_http_client(server.url, http_client=create_mcp_http_client(headers=server.headers))
    async with transport as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            yield session


async def list_tools(server: ToolServer) -> list[dict]:
    """Connect once and return the server's tools as [{name, description}]."""
    async def run() -> list[dict]:
        async with _session(server) as session:
            result = await session.list_tools()
        return [{"name": t.name, "description": (t.description or "").strip()} for t in result.tools]

    try:
        return await asyncio.wait_for(run(), LIST_TIMEOUT)
    except asyncio.TimeoutError:
        raise ConfigError(f"{LIST_TIMEOUT} 秒内没连上 {server.name}") from None
    except Exception as e:  # noqa: BLE001 - whatever the server or command did, show it
        raise ConfigError(f"连不上 {server.name}：{_first_error(e)}") from None


async def call_tool(server: ToolServer, name: str, arguments: dict):
    """Use an explicitly allowed tool to obtain an application-owned receipt."""
    if not server.enabled or name not in server.allowed:
        raise ConfigError(f"没有获准使用 {server.name} 的 {name}")
    async def run():
        async with _session(server) as session:
            return await session.call_tool(name, arguments)
    try:
        return await asyncio.wait_for(run(), server.timeout)
    except asyncio.TimeoutError:
        raise ConfigError(f"{server.name} 在 {server.timeout} 秒内没有返回") from None
    except Exception as e:
        raise ConfigError(f"{server.name} 未完成：{_first_error(e)}") from None


def _first_error(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return str(e) or type(e).__name__


def write_grok_config(work_dir: Path, servers: Sequence[ToolServer]) -> None:
    path = work_dir / ".grok" / "config.toml"
    if servers:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(grok_config(servers), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
