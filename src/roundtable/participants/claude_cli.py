"""Claude via the locally logged-in Claude Code CLI (uses the Claude subscription)."""

from __future__ import annotations

import json

from .base import ActivityHook, Participant, ParticipantError, resolve_command, run_cli


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
            # Streamed events show when a web search starts; the last line is the result.
            "--output-format", "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--system-prompt-file", str(self._prompt_file),
        ]
        if self.cfg.web_search:
            # Read-only web search is the only tool; allowed up front since nobody can approve.
            cmd += ["--tools", "WebSearch", "--allowedTools", "WebSearch"]
        else:
            cmd += ["--tools", ""]
        if self.cfg.model:
            cmd += ["--model", self.cfg.model]
        if self.cfg.effort:
            cmd += ["--effort", self.cfg.effort]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    async def reply(self, chat_key: str, prompt: str, on_activity: ActivityHook | None = None) -> str:
        session_id = self.store.get(chat_key, self.id)
        out = await run_cli(
            self.build_command(session_id), prompt, self.work_dir, self.timeout,
            on_line=_activity_reader(on_activity),
        )
        text, new_session = parse_output(out)
        self.store.set(chat_key, self.id, new_session)
        return text


def _activity_reader(on_activity: ActivityHook | None):
    if on_activity is None:
        return None

    def read(line: str) -> None:
        if '"tool_use"' not in line:
            return
        event = json.loads(line)
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") == "WebSearch":
                query = (block.get("input") or {}).get("query", "")
                on_activity(f"正在联网搜索：{query}" if query else "正在联网搜索")

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
