"""Configuration model shared by the bot, the console and the admin page."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class TelegramConfig:
    controller_token: str = ""
    allowed_user_ids: list[int] = field(default_factory=list)
    allowed_chat_ids: list[int] = field(default_factory=list)


@dataclass
class RoundConfig:
    max_turns_per_model: int = 3
    pass_consumes_turn: bool = False
    call_timeout: float = 180.0
    # When a model takes longer than this, another may start speaking meanwhile...
    slow_after: float = 8.0
    # ...as long as no more than this many are speaking at once (1 = strictly one by one).
    max_parallel: int = 2


@dataclass
class ParticipantConfig:
    id: str
    name: str
    kind: str
    enabled: bool = True
    bot_token: str = ""
    model: str = ""
    effort: str = ""
    aliases: list[str] = field(default_factory=list)
    persona: str = ""
    # Read-only web search with the model's own built-in tool (Grok's also covers X).
    web_search: bool = False
    # Explicit command prefix, e.g. ["C:/tools/claude.exe"]; resolved from PATH when empty.
    command: list[str] = field(default_factory=list)
    # kind = "api" only
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    params: dict = field(default_factory=dict)  # extra request fields, e.g. temperature
    max_history: int = 40  # messages of history sent with each request


@dataclass
class Config:
    data_dir: Path
    telegram: TelegramConfig
    round: RoundConfig
    participants: list[ParticipantConfig]
    system_template: str = ""  # empty: use the built-in group chat prompt
    stickers_enabled: bool = True
    max_stickers: int = 1  # per message
    memory_learn: str = "review"  # off / review / auto: what happens to [记住:…] from models

    @property
    def enabled_participants(self) -> list[ParticipantConfig]:
        return [p for p in self.participants if p.enabled]


KNOWN_KINDS = {"claude_cli", "codex_cli", "grok_cli", "api"}


def load_legacy_toml(path: Path) -> Config:
    """Read the config.toml format used before the admin page existed."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        data_dir=Path(raw.get("data_dir", "~/.tg-roundtable")).expanduser(),
        telegram=TelegramConfig(**raw.get("telegram", {})),
        round=RoundConfig(**raw.get("round", {})),
        participants=[ParticipantConfig(**p) for p in raw.get("participants", [])],
    )


def telegram_problems(cfg: Config) -> list[str]:
    """What still has to be filled in before the Telegram side can start."""
    problems = []
    if not cfg.telegram.controller_token:
        problems.append("还没有设置总控 bot")
    if not cfg.telegram.allowed_user_ids:
        problems.append("还没有填写允许的 Telegram 用户 ID")
    if not cfg.enabled_participants:
        problems.append("还没有启用任何成员")
    return problems
