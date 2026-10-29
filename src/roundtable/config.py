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
    # After the first reply to a message, the chance that someone else chimes in, and how
    # much that chance shrinks for every further reply (0.5, then 0.5*0.4=0.2, ...).
    # Being @-mentioned always gets an answer. 1.0 and 1.0 = everyone may speak.
    follow_up_chance: float = 0.5
    follow_up_decay: float = 0.4


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
    # Role card: who this member plays in the group. `name` is the character's name.
    tagline: str = ""  # one-line intro, also used as the bot's Telegram description
    persona: str = ""  # identity and personality
    style: str = ""  # how it talks: length, tone, catchphrases, emoji
    relationships: str = ""  # how it sees the others in the group
    examples: str = ""  # a few sample lines, one per line; pins the voice down best
    activity: str = "normal"  # low / normal / high: how eagerly it joins in
    appearance: str = ""  # what it looks like; used when someone draws it
    # Daily routine: "00:30-10:30" asleep; busy on weekdays (work, school), and what it does then.
    sleep: str = ""
    busy: str = ""
    busy_doing: str = ""
    # Drawing stickers for others when asked (Codex CLI only).
    can_draw: bool = False
    # The model is handed the pictures sent in the group (Claude CLI, or API models that take images;
    # Codex always can).
    vision: bool = False
    draws_per_day: int = 5
    max_turns: int = 0  # turns per round; 0 = the group's default
    # Read-only web search with the model's own built-in tool (Grok's also covers X).
    web_search: bool = False
    # Proactive posts: now and then share something (or start a topic) unprompted.
    proactive: bool = False
    proactive_topics: str = ""  # what to look for, e.g. "AI 圈八卦、离谱新闻"
    proactive_per_day: int = 3  # at most this many posts a day
    proactive_hours: str = "10-23"  # local hours it may post in (start-end)
    proactive_idle_minutes: int = 60  # only after the group has been quiet this long
    proactive_replies: int = 1  # turns each other member gets to react
    # Browsing on its own and keeping a journal of what it saw (needs web search).
    browse: bool = False
    browse_topics: str = ""  # what it likes to look at; empty = the proactive topics
    browse_per_day: int = 4
    browse_hours: str = "10-23"
    journal_days: int = 3  # ordinary sightings are forgotten after this many days...
    journal_keep_days: int = 7  # ...memorable or shared ones after this many
    # Explicit command prefix, e.g. ["C:/tools/claude.exe"]; resolved from PATH when empty.
    command: list[str] = field(default_factory=list)
    # kind = "api" only
    provider: str = ""
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    params: dict = field(default_factory=dict)  # extra request fields, e.g. temperature
    tool_notes: dict = field(default_factory=dict)  # tool id -> what it likes to look at with that tool
    max_history: int = 40  # messages of history sent with each request


@dataclass
class LifeConfig:
    enabled: bool = False
    members: list[str] = field(default_factory=list)
    per_day: int = 4
    hours: str = "10-23"
    shares_per_day: int = 2
    quiet_minutes: int = 30
    # Self-initiated activities; 0 means no daily cap, with normal spacing retained.
    member_days: dict = field(default_factory=dict)
    passive_per_day: int = 4
    passive_member_days: dict = field(default_factory=dict)


@dataclass
class Config:
    data_dir: Path
    telegram: TelegramConfig
    round: RoundConfig
    participants: list[ParticipantConfig]
    system_template: str = ""  # empty: use the built-in group chat prompt
    scene: str = ""  # shared setting for the whole group, e.g. "you are housemates"
    no_coding: bool = True  # everyday chat only: decline programming help
    proactive_chat_id: int = 0  # group for proactive posts; 0 = the most recently active one
    # Pictures: "help" (members who can see get them, the others ask a friend), "auto" or a member id
    # (that member describes every picture for everyone first), or "off".
    vision_member: str = "help"
    tools: list = field(default_factory=list)  # ToolServer: MCP servers the owner added
    life: LifeConfig = field(default_factory=LifeConfig)
    stickers_enabled: bool = True
    max_stickers: int = 1  # per message
    kaomoji_enabled: bool = True
    reactions_enabled: bool = True  # members may put emoji reactions on messages
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
