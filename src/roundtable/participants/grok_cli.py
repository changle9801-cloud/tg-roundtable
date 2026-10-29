"""Grok via the locally logged-in Grok Build CLI (uses the grok.com subscription)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Sequence

from ..tools import grok_rules, write_grok_config
from .base import ActivityHook, Participant, ParticipantError, resolve_command, run_cli

# Every built-in tool, removed so Grok can only chat. Unknown names only cause a warning.
DISALLOWED_TOOLS = ",".join([
    "run_terminal_cmd", "read_file", "write", "search_replace", "list_dir", "grep",
    "task", "Agent", "get_task_output", "wait_tasks", "kill_task", "todo_write",
    "scheduler_create", "scheduler_delete", "scheduler_list", "monitor", "workflow",
    "search_tool", "use_tool", "enter_plan_mode", "exit_plan_mode", "ask_user_question",
    "send_feedback", "image_gen", "image_edit", "image_to_video", "reference_to_video",
    # Opening a page needs an approval nobody can give in a headless run, which ends the turn
    # with no answer; pages are read through the 读帖子与订阅 tool instead.
    "web_fetch",
])


class GrokCLI(Participant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command = resolve_command("grok", self.cfg.command)
        # No cross-session memory writes, no self-update while the bot runs.
        self.env = {**os.environ, "GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"}
        # Grok also reads Claude Code's and Cursor's MCP servers, skills, hooks and rules by
        # default; this member gets none of the owner's own setup.
        for cell in ("AGENTS", "HOOKS", "MCPS", "RULES", "SKILLS"):
            self.env[f"GROK_CLAUDE_{cell}_ENABLED"] = "0"
            self.env[f"GROK_CURSOR_{cell}_ENABLED"] = "0"

    def build_command(self, prompt_file: str, session_id: str | None) -> list[str]:
        cmd = [
            *self.command,
            "--prompt-file", prompt_file,
            "--verbatim",
            "--output-format", "streaming-json",  # tool events show when a search starts
            # MCP tools are called through search_tool / use_tool; those stay off without tools.
            "--disallowed-tools", DISALLOWED_TOOLS if not self.tools else ",".join(
                t for t in DISALLOWED_TOOLS.split(",") if t not in ("search_tool", "use_tool")),
            "--no-subagents",
            "--no-plan",
            "--permission-mode", "default",
            "--system-prompt-override", self.system_prompt,
        ]
        if not self.cfg.web_search:
            # With search on, Grok keeps its built-in web and X search; everything else stays off.
            cmd.append("--disable-web-search")
        # The owner's MCP servers live in this member's own folder, which it must trust to load
        # them; only the allowed tools may run and file reads and edits are refused outright.
        write_grok_config(self.work_dir, self.tools)
        if self.tools:
            cmd += ["--trust", *grok_rules(self.tools)]
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
        # Grok does not read prompts from stdin; rooms may call concurrently, so use a unique file.
        prompt = self.with_turn_context(prompt)
        prompt_path = self.work_dir / f"prompt-{uuid.uuid4().hex}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        announced: set[str] = set()

        def watch(line: str) -> None:
            if on_activity is None or '"tool_call"' not in line:
                return
            activity = describe_tool(json.loads(line))
            if activity and activity not in announced:  # one notice per kind of search
                announced.add(activity)
                on_activity(activity)

        try:
            cmd = self.build_command(str(prompt_path), self.store.get(chat_key, self.id))
            out = await run_cli(cmd, "", self.work_dir, self.timeout, env=self.env, on_line=watch)
        finally:
            prompt_path.unlink(missing_ok=True)
        text, session_id = parse_output(out)
        self.store.set(chat_key, self.id, session_id)
        return text


def describe_tool(event: dict) -> str:
    if event.get("type") != "tool_call":
        return ""
    name = str(event.get("toolName") or event.get("title") or "")
    if name in ("use_tool", "search_tool"):  # an MCP tool
        return "正在用工具"
    variant = str((event.get("rawInput") or {}).get("variant", ""))
    if "XSearch" in variant or name.lower().startswith("x search"):
        return "正在 X 上搜索"
    if "search" in name.lower() or "web" in name.lower():
        return "正在联网搜索"
    return ""


def parse_output(out: str) -> tuple[str, str]:
    """Read a streaming-json run: the reply is the text after the last tool call."""
    segments = [[]]
    session_id = None
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "text":
            segments[-1].append(str(event.get("data", "")))
        elif kind == "tool_call":
            # Text before a search is usually "let me look that up"; keep the answer after it.
            segments.append([])
        elif kind == "end":
            session_id = event.get("sessionId")
        elif kind == "error":
            raise ParticipantError(str(event.get("message", "未知错误")))
    if not session_id:
        raise ParticipantError(f"Grok 输出缺少 sessionId：{out[:200]}")
    texts = ["".join(s).strip() for s in segments]
    answer = next((t for t in reversed(texts) if t), "")
    return answer, str(session_id)
