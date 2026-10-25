from pathlib import Path

from ..config import Config, ConfigError, ParticipantConfig
from .anthropic_api import AnthropicAPI
from .api import HistoryParticipant, OpenAICompatAPI
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


def participant_class(pc: ParticipantConfig) -> type[Participant]:
    if pc.kind == "api" and pc.provider == "anthropic":
        return AnthropicAPI  # Claude has its own protocol, not the OpenAI one
    cls = KINDS.get(pc.kind)
    if cls is None:
        raise ConfigError(f"未知的 kind {pc.kind!r}")
    return cls


def supports_web_search(pc: ParticipantConfig) -> bool:
    """Members whose model has a built-in, read-only search tool we can switch on."""
    return pc.kind in ("claude_cli", "codex_cli", "grok_cli") or (pc.kind == "api" and pc.provider == "anthropic")


def build_participants(cfg: Config, store: SessionStore) -> list[Participant]:
    result = []
    for p in cfg.enabled_participants:
        work_dir = Path(cfg.data_dir) / "work" / p.id
        result.append(participant_class(p)(p, store, work_dir, cfg.round.call_timeout))
    return result


__all__ = [
    "HistoryParticipant",
    "Participant",
    "ParticipantError",
    "SessionStore",
    "build_participants",
    "participant_class",
    "supports_web_search",
]
