"""Claude via the locally logged-in Claude Code CLI (uses the Claude subscription)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Sequence

from ..tools import claude_allowed, claude_config
from .base import ActivityHook, Participant, ToolActivity, ParticipantError, image_data, resolve_command, run_cli


class ClaudeCLI(Participant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command = resolve_command("claude", self.cfg.command)
        self._prompt_file = self.work_dir / "system_prompt.md"
        self._mcp_file = self.work_dir / "mcp.json"

    def set_system_prompt(self, prompt: str) -> None:
        super().set_system_prompt(prompt)
        self._prompt_file.write_text(prompt, encoding="utf-8")

    def build_command(self, session_id: str | None, with_images: bool = False) -> list[str]:
        cmd = [
            *self.command,
            "-p",
            # Pictures need the JSON message format on stdin; plain chat stays plain text.
            *(["--input-format", "stream-json"] if with_images else []),
            # Streamed events show when a web search starts; the last line is the result.
            "--output-format", "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--system-prompt-file", str(self._prompt_file),
        ]
        # Built-in tools: read-only web search at most. Allowed up front since nobody can approve.
        cmd += ["--tools", "WebSearch" if self.cfg.web_search else ""]
        allowed = ["WebSearch"] if self.cfg.web_search else []
        if self.tools:
            # The owner's MCP servers, in this member's own file (--strict-mcp-config ignores the rest).
            self._mcp_file.write_text(json.dumps(claude_config(self.tools), ensure_ascii=False), encoding="utf-8")
            cmd += ["--mcp-config", str(self._mcp_file)]
            allowed += claude_allowed(self.tools)
        if allowed:
            cmd += ["--allowedTools", ",".join(allowed)]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    async def reply(
        self, chat_key: str, prompt: str, on_activity: ActivityHook | None = None, images: Sequence[Path] = ()
    ) -> str:
        session_id = self.store.get(chat_key, self.id)
        prompt = self.with_turn_context(prompt)
        stdin = image_message(prompt, images) if images else prompt
        env = None
        if self.tools:  # slow tools (watching a video) may take minutes
            env = {**os.environ, "MCP_TOOL_TIMEOUT": str(1000 * max(s.timeout for s in self.tools))}
        names = {s.id: s.name for s in self.tools}
        out = await run_cli(
            self.build_command(session_id, bool(images)), stdin, self.work_dir, self.timeout,
            env=env, on_line=_activity_reader(on_activity, names),
        )
        text, new_session = parse_output(out)
        self.store.set(chat_key, self.id, new_session)
        return text


def image_message(prompt: str, images: Sequence[Path]) -> str:
    """One user message with pictures, as a line of --input-format stream-json."""
    content = []
    for image in images:
        mime, data = image_data(image)
        content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
    content.append({"type": "text", "text": prompt})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False) + "\n"


def _activity_reader(on_activity: ActivityHook | None, tool_names: dict[str, str] | None = None):
    if on_activity is None:
        return None

    def read(line: str) -> None:
        if '"tool_use"' not in line:
            return
        event = json.loads(line)
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") != "tool_use":
                continue
            name = str(block.get("name", ""))
            if name == "WebSearch":
                query = (block.get("input") or {}).get("query", "")
                on_activity(f"正在联网搜索：{query}" if query else "正在联网搜索")
            elif name.startswith("mcp__"):
                server = name.split("__")[1]
                on_activity(ToolActivity(f"正在用{(tool_names or {}).get(server, server)}", server))

    return read


def parse_output(out: str) -> tuple[str, str]:
    """Read the final ``result`` event of a stream-json (or plain json) run."""
    data = None
    for line in reversed(out.strip().splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if candidate.get("type", "result") == "result":
            data = candidate
            break
    if data is None:
        raise ParticipantError(f"无法解析 Claude 输出：{out[:200]}")
    if data.get("is_error"):
        raise ParticipantError(str(data.get("result") or data.get("subtype") or "未知错误"))
    return str(data.get("result", "")), str(data["session_id"])
