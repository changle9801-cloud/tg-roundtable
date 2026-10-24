"""Settings and secrets, stored in the data dir outside the repository.

    <data_dir>/settings.json   members, round rules, prompts (no secrets)
    <data_dir>/secrets.json    bot tokens, API keys, admin page token
"""

from __future__ import annotations

import json
import re
import secrets as _secrets
from dataclasses import asdict, fields
from pathlib import Path

from .config import (
    KNOWN_KINDS,
    Config,
    ConfigError,
    ParticipantConfig,
    RoundConfig,
    TelegramConfig,
    load_legacy_toml,
)
from .memory import LEARN_MODES

SETTINGS_FILE = "settings.json"
SECRETS_FILE = "secrets.json"
DEFAULT_PORT = 8787

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SECRET_FIELDS = ("bot_token", "api_key")
PARTICIPANT_FIELDS = [f for f in fields(ParticipantConfig) if f.name not in SECRET_FIELDS]


def default_data_dir() -> Path:
    return Path("~/.tg-roundtable").expanduser()


def default_settings() -> dict:
    return {
        "telegram": {"allowed_user_ids": [], "allowed_chat_ids": [], "controller_participant": ""},
        "round": asdict(RoundConfig()),
        "prompts": {"system_template": ""},
        "stickers": {"enabled": True, "max_per_message": 1},
        "memory": {"learn": "review"},
        "web": {"port": DEFAULT_PORT},
        "participants": [],
    }


def default_secrets() -> dict:
    return {"controller_token": "", "bots": {}, "api_keys": {}, "web_token": ""}


def mask(secret: str) -> str:
    if not secret:
        return ""
    return "…" + secret[-4:] if len(secret) > 8 else "已设置"


# ---- validation ----


def _int_list(value, what: str) -> list[int]:
    if isinstance(value, str):
        value = [v for v in re.split(r"[\s,，]+", value) if v]
    try:
        return [int(v) for v in value or []]
    except (TypeError, ValueError):
        raise ConfigError(f"{what} 只能填数字 ID") from None


def normalize_participant(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("成员配置格式不对")
    pid = str(raw.get("id", "")).strip()
    if not ID_RE.match(pid):
        raise ConfigError(f"成员 ID {pid!r} 不合法：只能用小写字母、数字、- 和 _，最长 32 个字符")
    if not str(raw.get("name", "")).strip():
        raise ConfigError(f"成员 {pid} 缺少名称")
    if raw.get("kind") not in KNOWN_KINDS:
        raise ConfigError(f"成员 {pid} 的类型 {raw.get('kind')!r} 不支持")

    out = {}
    defaults = asdict(ParticipantConfig(id=pid, name="", kind=""))
    for f in PARTICIPANT_FIELDS:
        value = raw.get(f.name, defaults[f.name])
        if f.name == "aliases" and isinstance(value, str):
            value = [a.strip() for a in re.split(r"[,，]", value) if a.strip()]
        if f.name == "command" and isinstance(value, str):
            value = value.split() if value.strip() else []
        if f.name == "params":
            if isinstance(value, str):
                try:
                    value = json.loads(value) if value.strip() else {}
                except ValueError:
                    raise ConfigError(f"成员 {pid} 的附加参数不是合法的 JSON") from None
            if not isinstance(value, dict):
                raise ConfigError(f"成员 {pid} 的附加参数必须是 JSON 对象")
        if f.name == "max_history":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"成员 {pid} 的历史条数必须是数字") from None
            if value < 2:
                raise ConfigError(f"成员 {pid} 的历史条数至少为 2")
        if f.name in ("name", "model", "effort", "persona", "provider", "base_url", "api_key_env"):
            value = str(value or "").strip()
        if f.name == "enabled":
            value = bool(value)
        out[f.name] = value
    return out


def normalize_settings(raw: dict) -> dict:
    base = default_settings()
    tg = {**base["telegram"], **(raw.get("telegram") or {})}
    tg["allowed_user_ids"] = _int_list(tg.get("allowed_user_ids"), "允许的用户")
    tg["allowed_chat_ids"] = _int_list(tg.get("allowed_chat_ids"), "允许的群")
    tg["controller_participant"] = str(tg.get("controller_participant") or "")

    rnd = {**base["round"], **(raw.get("round") or {})}
    try:
        rnd = {
            "max_turns_per_model": int(rnd["max_turns_per_model"]),
            "pass_consumes_turn": bool(rnd["pass_consumes_turn"]),
            "call_timeout": float(rnd["call_timeout"]),
        }
    except (TypeError, ValueError):
        raise ConfigError("群聊规则里有不是数字的值") from None
    if not 1 <= rnd["max_turns_per_model"] <= 10:
        raise ConfigError("每轮发言次数要在 1 到 10 之间")
    if not 10 <= rnd["call_timeout"] <= 1800:
        raise ConfigError("超时时间要在 10 到 1800 秒之间")

    prompts = {"system_template": str((raw.get("prompts") or {}).get("system_template") or "")}
    stickers = {**base["stickers"], **(raw.get("stickers") or {})}
    try:
        stickers = {"enabled": bool(stickers["enabled"]), "max_per_message": int(stickers["max_per_message"])}
    except (TypeError, ValueError):
        raise ConfigError("每条消息的表情数必须是数字") from None
    if not 1 <= stickers["max_per_message"] <= 3:
        raise ConfigError("每条消息的表情数要在 1 到 3 之间")
    learn = str((raw.get("memory") or {}).get("learn") or base["memory"]["learn"])
    if learn not in LEARN_MODES:
        raise ConfigError(f"自动记忆的模式只能是 {'、'.join(LEARN_MODES)}")
    memory = {"learn": learn}
    web = {**base["web"], **(raw.get("web") or {})}
    web = {"port": int(web.get("port") or DEFAULT_PORT)}

    participants = [normalize_participant(p) for p in raw.get("participants") or []]
    ids = [p["id"] for p in participants]
    if len(set(ids)) != len(ids):
        raise ConfigError("成员 ID 不能重复")
    if tg["controller_participant"] and tg["controller_participant"] not in ids:
        tg["controller_participant"] = ""

    return {
        "telegram": tg,
        "round": rnd,
        "prompts": prompts,
        "stickers": stickers,
        "memory": memory,
        "web": web,
        "participants": participants,
    }


# ---- store ----


class SettingsStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.settings_path = data_dir / SETTINGS_FILE
        self.secrets_path = data_dir / SECRETS_FILE

    def exists(self) -> bool:
        return self.settings_path.exists()

    def load(self) -> tuple[dict, dict]:
        settings = normalize_settings(_read_json(self.settings_path))
        secrets = {**default_secrets(), **_read_json(self.secrets_path)}
        return settings, secrets

    def save(self, settings: dict | None = None, secrets: dict | None = None) -> None:
        if settings is not None:
            _write_json(self.settings_path, normalize_settings(settings))
        if secrets is not None:
            _write_json(self.secrets_path, secrets)

    def web_token(self) -> str:
        settings, secrets = self.load()
        if not secrets.get("web_token"):
            secrets["web_token"] = _secrets.token_urlsafe(24)
            self.save(secrets=secrets)
        return secrets["web_token"]

    def config(self) -> Config:
        return build_config(*self.load(), self.data_dir)


def apply_secret_changes(secrets: dict, changes: dict, participant_ids: set[str]) -> dict:
    """Merge admin-page edits: a string sets a value, null deletes it, absent keeps it."""
    out = {**secrets, "bots": dict(secrets.get("bots", {})), "api_keys": dict(secrets.get("api_keys", {}))}
    if "controller_token" in changes:
        out["controller_token"] = (changes["controller_token"] or "").strip()
    for group in ("bots", "api_keys"):
        for pid, value in (changes.get(group) or {}).items():
            if pid not in participant_ids:
                raise ConfigError(f"没有 ID 为 {pid} 的成员")
            if value is None or not str(value).strip():
                out[group].pop(pid, None)
            else:
                out[group][pid] = str(value).strip()
    return out


def prune_secrets(secrets: dict, participant_ids: set[str]) -> dict:
    """Drop tokens and keys of members that no longer exist."""
    out = dict(secrets)
    for group in ("bots", "api_keys"):
        out[group] = {k: v for k, v in secrets.get(group, {}).items() if k in participant_ids}
    return out


def masked_secrets(secrets: dict) -> dict:
    return {
        "controller_token": mask(secrets.get("controller_token", "")),
        "bots": {k: mask(v) for k, v in secrets.get("bots", {}).items()},
        "api_keys": {k: mask(v) for k, v in secrets.get("api_keys", {}).items()},
    }


def build_config(settings: dict, secrets: dict, data_dir: Path) -> Config:
    bots = secrets.get("bots", {})
    keys = secrets.get("api_keys", {})
    participants = [
        ParticipantConfig(**p, bot_token=bots.get(p["id"], ""), api_key=keys.get(p["id"], ""))
        for p in settings["participants"]
    ]
    tg = settings["telegram"]
    controller = tg.get("controller_participant", "")
    token = bots.get(controller, "") if controller else secrets.get("controller_token", "")
    return Config(
        data_dir=data_dir,
        telegram=TelegramConfig(
            controller_token=token,
            allowed_user_ids=list(tg["allowed_user_ids"]),
            allowed_chat_ids=list(tg["allowed_chat_ids"]),
        ),
        round=RoundConfig(**settings["round"]),
        participants=participants,
        system_template=settings["prompts"]["system_template"],
        stickers_enabled=settings["stickers"]["enabled"],
        max_stickers=settings["stickers"]["max_per_message"],
        memory_learn=settings["memory"]["learn"],
    )


def import_legacy_toml(path: Path) -> tuple[dict, dict]:
    """Convert an old config.toml into (settings, secrets)."""
    cfg = load_legacy_toml(path)
    settings = default_settings()
    secrets = default_secrets()
    settings["telegram"]["allowed_user_ids"] = cfg.telegram.allowed_user_ids
    settings["telegram"]["allowed_chat_ids"] = cfg.telegram.allowed_chat_ids
    settings["round"] = asdict(cfg.round)
    for p in cfg.participants:
        data = asdict(p)
        if p.bot_token:
            secrets["bots"][p.id] = p.bot_token
        if p.api_key:
            secrets["api_keys"][p.id] = p.api_key
        settings["participants"].append({k: v for k, v in data.items() if k not in SECRET_FIELDS})
    shared = next((p.id for p in cfg.participants if p.bot_token and p.bot_token == cfg.telegram.controller_token), "")
    if shared:
        settings["telegram"]["controller_participant"] = shared
    else:
        secrets["controller_token"] = cfg.telegram.controller_token
    return normalize_settings(settings), secrets


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"无法读取 {path}：{e}") from None
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
