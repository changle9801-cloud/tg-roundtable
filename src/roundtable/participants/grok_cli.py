"""Grok via the locally logged-in Grok Build CLI (uses the grok.com subscription)."""

from __future__ import annotations

import json
import os
import uuid

from .base import Participant, ParticipantError, resolve_command, run_cli

# Every built-in tool, removed so Grok can only chat. Unknown names only cause a warning.
DISALLOWED_TOOLS = ",".join([
    "run_terminal_cmd", "read_file", "write", "search_replace", "list_dir", "grep",
    "task", "Agent", "get_task_output", "wait_tasks", "kill_task", "todo_write",
    "scheduler_create", "scheduler_delete", "scheduler_list", "monitor", "workflow",
    "search_tool", "use_tool", "enter_plan_mode", "exit_plan_mode", "ask_user_question",
    "send_feedback", "image_gen", "image_edit", "image_to_video", "reference_to_video",
])


class GrokCLI(Participant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command = resolve_command("grok", self.cfg.command)
        # No cross-session memory writes, no self-update while the bot runs.
        self.env = {**os.environ, "GROK_MEMORY": "0", "GROK_DISABLE_AUTOUPDATER": "1"}

    def build_command(self, prompt_file: str, session_id: str | None) -> list[str]:
        cmd = [
            *self.command,
            "--prompt-file", prompt_file,
            "--verbatim",
            "--output-format", "json",
            "--disallowed-tools", DISALLOWED_TOOLS,
            "--disable-web-search",
            "--no-subagents",
            "--no-plan",
            "--permission-mode", "default",
            "--system-prompt-override", self.system_prompt,
        ]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    async def reply(self, chat_key: str, prompt: str) -> str:
        # Grok does not read prompts from stdin; rooms may call concurrently, so use a unique file.
        prompt_path = self.work_dir / f"prompt-{uuid.uuid4().hex}.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        try:
            cmd = self.build_command(str(prompt_path), self.store.get(chat_key, self.id))
            out = await run_cli(cmd, "", self.work_dir, self.timeout, env=self.env)
        finally:
            prompt_path.unlink(missing_ok=True)
        text, session_id = parse_output(out)
        self.store.set(chat_key, self.id, session_id)
        return text


def parse_output(out: str) -> tuple[str, str]:
    # The result is a (pretty-printed) JSON object, possibly after notices; parse from the
    # last line that opens an object at column 0.
    data = None
    lines = out.strip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("{"):
            try:
                data = json.loads("\n".join(lines[i:]))
                break
            except ValueError:
                continue
    if data is None:
        raise ParticipantError(f"无法解析 Grok 输出：{out[:200]}")
    if data.get("type") == "error":
        raise ParticipantError(str(data.get("message", "未知错误")))
    if not data.get("sessionId"):
        raise ParticipantError(f"Grok 输出缺少 sessionId：{out[:200]}")
    return str(data.get("text", "")), str(data["sessionId"])
