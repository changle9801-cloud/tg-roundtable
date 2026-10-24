from pathlib import Path

from ..config import Config, ConfigError
from .api import OpenAICompatAPI
from .base import Participant, ParticipantError, SessionStore
from .claude_cli import ClaudeCLI
from .codex_cli import CodexCLI
from .grok_cli import GrokCLI

KINDS: dict[str, type[Participant]] = {
    "claude_cli": ClaudeCLI,
    "codex_cli": CodexCLI,
    "grok_cli": GrokCLI,
    "api": OpenAICompatAPI,
}


def build_participants(cfg: Config, store: SessionStore) -> list[Participant]:
    result = []
    for p in cfg.enabled_participants:
        cls = KINDS.get(p.kind)
        if cls is None:
            raise ConfigError(f"未知的 kind {p.kind!r}")
        work_dir = Path(cfg.data_dir) / "work" / p.id
        result.append(cls(p, store, work_dir, cfg.round.call_timeout))
    return result


__all__ = ["Participant", "ParticipantError", "SessionStore", "build_participants"]
