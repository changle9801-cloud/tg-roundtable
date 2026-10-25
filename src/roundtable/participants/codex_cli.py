"""GPT via the locally logged-in Codex CLI (uses the ChatGPT subscription)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

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
            # Web search runs through the code-mode host. With the shell and exec tools off
            # it has no file or command tool to reach, so keep it when search is on.
            if feature == "code_mode_host" and self.cfg.web_search:
                continue
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


    async def draw(self, prompt: str) -> Path:
        """Generate one picture with Codex's image tool and return the file it saved.

        A separate, one-off run: image generation is the only thing left on (commands,
        files, browsing and search stay off). Codex keeps images under
        <CODEX_HOME>/generated_images/<thread id>/.
        """
        cmd = [
            *self.command, "exec",
            "--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "-c", 'sandbox_mode="read-only"',
            "-c", 'web_search="disabled"',
            "-c", f"developer_instructions={_toml_str(DRAW_INSTRUCTIONS)}",
            "-c", 'model_reasoning_effort="low"',
        ]
        for feature in DISABLED_FEATURES:
            if feature not in ("image_generation", "code_mode_host"):  # the image tool needs both
                cmd += ["--disable", feature]
        cmd.append("-")
        started = time.time()
        out = await run_cli(cmd, prompt, self.work_dir, max(self.timeout, 300))
        thread_id = next(
            (json.loads(line).get("thread_id") for line in out.splitlines() if '"thread.started"' in line), None
        )
        folder = codex_home() / "generated_images" / (thread_id or "")
        images = sorted(
            (p for p in folder.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and p.stat().st_mtime >= started - 5),
            key=lambda p: p.stat().st_mtime,
        ) if thread_id and folder.is_dir() else []
        if not images:
            raise ParticipantError("没有画出图片")
        return images[-1]


DRAW_INSTRUCTIONS = "你是表情包画师。只用画图工具画用户要的那一张图，画完就结束，不要写解释、不要追问。"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


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
