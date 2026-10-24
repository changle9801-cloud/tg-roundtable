"""Claude via the locally logged-in Claude Code CLI (uses the Claude subscription)."""

from __future__ import annotations

import json

from .base import Participant, ParticipantError, resolve_command, run_cli


class ClaudeCLI(Participant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command = resolve_command("claude", self.cfg.command)
        self._prompt_file = self.work_dir / "system_prompt.md"

    def set_system_prompt(self, prompt: str) -> None:
        super().set_system_prompt(prompt)
        self._prompt_file.write_text(prompt, encoding="utf-8")

    def build_command(self, session_id: str | None) -> list[str]:
        cmd = [
            *self.command,
            "-p",
            "--output-format", "json",
            # Plain chat: no tools, no MCP servers, our own system prompt.
            "--tools", "",
            "--strict-mcp-config",
            "--system-prompt-file", str(self._prompt_file),
        ]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    async def reply(self, chat_key: str, prompt: str) -> str:
        session_id = self.store.get(chat_key, self.id)
        out = await run_cli(self.build_command(session_id), prompt, self.work_dir, self.timeout)
        text, new_session = parse_output(out)
        self.store.set(chat_key, self.id, new_session)
        return text


def parse_output(out: str) -> tuple[str, str]:
    try:
        data = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ParticipantError(f"无法解析 Claude 输出：{out[:200]}") from None
    if data.get("is_error"):
        raise ParticipantError(str(data.get("result") or data.get("subtype") or "未知错误"))
    return str(data.get("result", "")), str(data["session_id"])
