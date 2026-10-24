"""Shared plumbing for participants backed by a local CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from ..config import ConfigError, ParticipantConfig

log = logging.getLogger(__name__)


class ParticipantError(Exception):
    """A model call failed; the message is safe to show in the chat."""


class SessionStore:
    """Persists CLI session ids per (chat, participant) so restarts keep context."""

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, dict[str, str]] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("会话文件 %s 损坏，已忽略", path)

    def get(self, chat_key: str, participant_id: str) -> str | None:
        return self._data.get(chat_key, {}).get(participant_id)

    def set(self, chat_key: str, participant_id: str, session_id: str) -> None:
        if self.get(chat_key, participant_id) == session_id:
            return
        self._data.setdefault(chat_key, {})[participant_id] = session_id
        self._save()

    def clear(self, chat_key: str) -> None:
        if self._data.pop(chat_key, None) is not None:
            self._save()

    def clear_one(self, chat_key: str, participant_id: str) -> None:
        if self._data.get(chat_key, {}).pop(participant_id, None) is not None:
            self._save()

    def chats(self) -> list[str]:
        return list(self._data)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)


class Participant(ABC):
    """One AI member of the group chat."""

    def __init__(self, cfg: ParticipantConfig, store: SessionStore, work_dir: Path, timeout: float):
        self.cfg = cfg
        self.id = cfg.id
        self.name = cfg.name
        # Telegram usernames are added at startup so "@SomeBot" counts as a mention.
        self.aliases: list[str] = list(cfg.aliases)
        self.store = store
        self.work_dir = work_dir
        self.timeout = timeout
        self.system_prompt = ""
        work_dir.mkdir(parents=True, exist_ok=True)

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt

    def reset(self, chat_key: str) -> None:
        """Forget the conversation; the session store is cleared by the room."""

    @abstractmethod
    async def reply(self, chat_key: str, prompt: str) -> str:
        """Send one turn to the model and return its raw text answer."""


# On Windows, npm installs .cmd shims. Passing free text through cmd.exe is fragile,
# so we call the real entry point behind the shim when we recognise it.
_NPM_ENTRYPOINTS = {
    "claude": ("@anthropic-ai/claude-code/bin/claude.exe", False),
    "codex": ("@openai/codex/bin/codex.js", True),
}


# Installers that put the binary somewhere a freshly updated PATH may not reach yet.
_KNOWN_LOCATIONS = {
    "grok": [Path.home() / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok")],
}


def resolve_command(name: str, override: list[str]) -> list[str]:
    if override:
        return list(override)
    path = shutil.which(name)
    if not path:
        known = next((p for p in _KNOWN_LOCATIONS.get(name, []) if p.exists()), None)
        if known:
            return [str(known)]
        raise ConfigError(f"在 PATH 中找不到 {name} 命令，请先安装并登录。")
    if os.name == "nt" and path.lower().endswith((".cmd", ".bat")) and name in _NPM_ENTRYPOINTS:
        rel, needs_node = _NPM_ENTRYPOINTS[name]
        entry = Path(path).parent / "node_modules" / rel
        node = shutil.which("node")
        if entry.exists() and (not needs_node or node):
            return [node, str(entry)] if needs_node else [str(entry)]
    return [path]


async def run_cli(
    cmd: list[str], stdin_text: str, cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> str:
    """Run a CLI with the prompt on stdin and return stdout; kills the process tree on timeout/cancel."""
    log.debug("run: %s", cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(proc)
        raise ParticipantError(f"超过 {timeout:.0f} 秒没有回应") from None
    except asyncio.CancelledError:
        await _kill_tree(proc)
        raise
    stdout = out.decode("utf-8", "replace")
    if proc.returncode != 0:
        detail = err.decode("utf-8", "replace").strip() or stdout.strip()
        raise ParticipantError(f"进程退出码 {proc.returncode}：{_tail(detail)}")
    return stdout


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    if os.name == "nt":
        # node-based CLIs spawn a native child; kill the whole tree.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        log.warning("进程 %s 未能及时结束", proc.pid)


def _tail(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else "…" + text[-limit:]
