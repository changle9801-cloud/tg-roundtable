"""Settings and secrets, stored in the data dir outside the repository.

    <data_dir>/settings.json   members, round rules, prompts (no secrets)
    <data_dir>/secrets.json    bot tokens, API keys, admin page token
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import shutil
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
ACTIVITY_LEVELS = ("low", "normal", "high")
PROACTIVE_RANGES = {
    "proactive_per_day": (1, 12, "每天主动发言次数"),
    "proactive_idle_minutes": (0, 1440, "冷场分钟数"),
    "proactive_replies": (0, 3, "其他人接话句数"),
    "browse_per_day": (1, 12, "每天浏览次数"),
    "journal_days": (1, 30, "见闻保留天数"),
    "journal_keep_days": (1, 60, "印象深的见闻保留天数"),
}


def parse_hours(value: str) -> tuple[int, int] | None:
    """'10-23' -> (10, 23); the window may wrap past midnight ('22-2')."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–~～]\s*(\d{1,2})\s*", value or "")
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if not (0 <= start <= 23 and 1 <= end <= 24) or start == end:
        return None
    return start, end
SECRET_FIELDS = ("bot_token", "api_key")
PARTICIPANT_FIELDS = [f for f in fields(ParticipantConfig) if f.name not in SECRET_FIELDS]


DATA_DIR_ENV = "ROUNDTABLE_HOME"


def default_data_dir() -> Path:
    return Path("~/.tg-roundtable").expanduser()


def resolve_data_dir(explicit: Path | None) -> Path:
    """--data-dir, else $ROUNDTABLE_HOME, else ~/.tg-roundtable.

    A chosen location must already exist: if it is on a drive that is not mounted,
    silently starting with an empty config would look like everything was lost.
    """
    chosen = explicit or (Path(os.environ[DATA_DIR_ENV]) if os.environ.get(DATA_DIR_ENV) else None)
    if chosen is None:
        return default_data_dir()
    chosen = chosen.expanduser()
    if not chosen.is_dir():
        source = "--data-dir" if explicit else f"环境变量 {DATA_DIR_ENV}"
        raise ConfigError(f"数据目录 {chosen} 不存在（来自 {source}）。如果是新位置，请先创建这个文件夹。")
    return chosen


def default_settings() -> dict:
    return {
        "telegram": {"allowed_user_ids": [], "allowed_chat_ids": [], "controller_participant": ""},
        "round": asdict(RoundConfig()),
        "prompts": {"system_template": "", "scene": "", "no_coding": True},
        "stickers": {"enabled": True, "max_per_message": 1},
        "memory": {"learn": "review"},
        "proactive": {"chat_id": 0},
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
        if f.name in ("name", "model", "effort", "provider", "base_url", "api_key_env",
                      "tagline", "persona", "style", "relationships", "examples",
                      "proactive_topics", "proactive_hours", "browse_topics", "browse_hours"):
            value = str(value or "").strip()
        if f.name in ("enabled", "web_search", "proactive", "browse"):
            value = bool(value)
        if f.name in PROACTIVE_RANGES:
            low, high, label = PROACTIVE_RANGES[f.name]
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"成员 {pid} 的{label}必须是数字") from None
            if not low <= value <= high:
                raise ConfigError(f"成员 {pid} 的{label}要在 {low} 到 {high} 之间")
        if f.name == "activity" and value not in ACTIVITY_LEVELS:
            value = "normal"
        if f.name == "max_turns":
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                raise ConfigError(f"成员 {pid} 的每轮发言上限必须是数字") from None
            if not 0 <= value <= 10:
                raise ConfigError(f"成员 {pid} 的每轮发言上限要在 0 到 10 之间（0 表示跟随群设置）")
        out[f.name] = value
    if parse_hours(out["proactive_hours"]) is None:
        raise ConfigError(f"成员 {pid} 的主动发言时间段格式不对，应该像「10-23」")
    if parse_hours(out["browse_hours"]) is None:
        raise ConfigError(f"成员 {pid} 的浏览时间段格式不对，应该像「10-23」")
    if out["journal_keep_days"] < out["journal_days"]:
        out["journal_keep_days"] = out["journal_days"]
    if len(out["tagline"]) > 120:
        raise ConfigError(f"成员 {pid} 的一句话介绍最多 120 个字（Telegram 的限制）")
    if len(out["name"]) > 64:
        raise ConfigError(f"成员 {pid} 的角色名最多 64 个字（Telegram 的限制）")
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
            "slow_after": float(rnd["slow_after"]),
            "max_parallel": int(rnd["max_parallel"]),
        }
    except (TypeError, ValueError):
        raise ConfigError("群聊规则里有不是数字的值") from None
    if not 1 <= rnd["slow_after"] <= 120:
        raise ConfigError("「等多久让别人先说」要在 1 到 120 秒之间")
    if not 1 <= rnd["max_parallel"] <= 4:
        raise ConfigError("同时发言的人数要在 1 到 4 之间")
    if not 1 <= rnd["max_turns_per_model"] <= 10:
        raise ConfigError("每轮发言次数要在 1 到 10 之间")
    if not 10 <= rnd["call_timeout"] <= 1800:
        raise ConfigError("超时时间要在 10 到 1800 秒之间")

    raw_prompts = {**base["prompts"], **(raw.get("prompts") or {})}
    prompts = {
        "system_template": str(raw_prompts.get("system_template") or ""),
        "scene": str(raw_prompts.get("scene") or "").strip(),
        "no_coding": bool(raw_prompts.get("no_coding")),
    }
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
    try:
        proactive = {"chat_id": int((raw.get("proactive") or {}).get("chat_id") or 0)}
    except (TypeError, ValueError):
        raise ConfigError("主动发言的目标群 ID 必须是数字") from None
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
        "proactive": proactive,
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
        scene=settings["prompts"]["scene"],
        no_coding=settings["prompts"]["no_coding"],
        proactive_chat_id=settings["proactive"]["chat_id"],
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


def copy_data_dir(source: Path, target: Path) -> int:
    """Copy all data to an empty (or new) folder and check the copy; returns the file count.

    The source is left in place so nothing is lost if something goes wrong.
    """
    source, target = source.resolve(), target.expanduser().resolve()
    if not (source / SETTINGS_FILE).exists():
        raise ConfigError(f"{source} 里没有配置，不需要搬")
    if target == source or source in target.parents:
        raise ConfigError("新位置不能是当前数据目录本身或它的子文件夹")
    if target.exists() and any(target.iterdir()):
        raise ConfigError(f"{target} 不是空文件夹，为避免覆盖已有文件，请换一个位置或先清空它")
    shutil.copytree(source, target, dirs_exist_ok=True)
    files = [p for p in source.rglob("*") if p.is_file()]
    for p in files:
        copied = target / p.relative_to(source)
        if not copied.is_file() or copied.stat().st_size != p.stat().st_size:
            raise ConfigError(f"复制后校验失败：{copied}")
    return len(files)


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
