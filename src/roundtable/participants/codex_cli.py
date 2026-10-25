"""GPT via the locally logged-in Codex CLI (uses the ChatGPT subscription)."""

from __future__ import annotations

import json

from .base import ActivityHook, Participant, ParticipantError, resolve_command, run_cli


# Chat only: remove every tool, not just sandbox it, so no prompt can talk Codex into
# running a command or reading a file. Unknown names in a future Codex are ignored.
DISABLED_FEATURES = [
    "shell_tool", "unified_exec", "code_mode_host", "view_image", "image_generation",
    "browser_use", "browser_use_external", "in_app_browser", "computer_use", "apps",
]


def _toml_str(value: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


class CodexCLI(Participant):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command = resolve_command("codex", self.cfg.command)

    def build_command(self, session_id: str | None) -> list[str]:
        cmd = [*self.command, "exec"]
        if session_id:
            cmd.append("resume")
        cmd += [
            "--json",
            "--skip-git-repo-check",
            # Skip ~/.codex/config.toml (MCP servers, memories, plugins): this is plain chat.
            "--ignore-user-config",
            "--ignore-rules",
            "-c", 'sandbox_mode="read-only"',
            "-c", 'web_search="live"' if self.cfg.web_search else 'web_search="disabled"',
            "-c", f"developer_instructions={_toml_str(self.system_prompt)}",
        ]
        for feature in DISABLED_FEATURES:
            cmd += ["--disable", feature]
        if self.cfg.model:
            cmd += ["-m", self.cfg.model]
        if self.cfg.effort:
            cmd += ["-c", f"model_reasoning_effort={_toml_str(self.cfg.effort)}"]
        if session_id:
            cmd.append(session_id)
        cmd.append("-")  # prompt from stdin
        return cmd

    async def reply(self, chat_key: str, prompt: str, on_activity: ActivityHook | None = None) -> str:
        session_id = self.store.get(chat_key, self.id)

        def watch(line: str) -> None:
            # The query is only known once the search completes; announce the start.
            if on_activity is not None and '"web_search"' in line and '"item.started"' in line:
                on_activity("正在联网搜索")

        out = await run_cli(self.build_command(session_id), prompt, self.work_dir, self.timeout, on_line=watch)
        text, thread_id = parse_events(out)
        if thread_id:
            self.store.set(chat_key, self.id, thread_id)
        return text


def parse_events(out: str) -> tuple[str, str | None]:
    thread_id = None
    messages: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            messages.append(event["item"].get("text", ""))
        elif kind in ("turn.failed", "error"):
            err = event.get("error") or event
            raise ParticipantError(str(err.get("message", err)))
    if not messages:
        raise ParticipantError("Codex 没有返回任何消息")
    return messages[-1], thread_id
