"""Sticker library: images the models can send by writing [表情:名称] in a reply.

    <data_dir>/stickers.json   index (label, description, members, enabled per file)
    <data_dir>/stickers/       the files
"""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import ConfigError

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_LABELS_IN_PROMPT = 80

# How Telegram should send each file type.
KINDS = {
    ".webp": "sticker", ".tgs": "sticker", ".webm": "sticker",
    ".gif": "animation", ".mp4": "animation",
    ".png": "photo", ".jpg": "photo", ".jpeg": "photo",
}

TAG_RE = re.compile(r"\[\s*(?:表情|sticker)\s*[:：]\s*([^\]\n]{1,30}?)\s*\]", re.IGNORECASE)


@dataclass
class Sticker:
    id: str
    file: str
    label: str
    description: str = ""
    enabled: bool = True
    members: list[str] = field(default_factory=list)  # who may send it; empty = everyone
    tg_ids: list[str] = field(default_factory=list)  # Telegram file_unique_ids, to recognise it when someone sends it
    path: Path = field(default=Path(), repr=False, compare=False)

    def allowed(self, member_id: str | None) -> bool:
        return member_id is None or not self.members or member_id in self.members

    @property
    def kind(self) -> str:
        return KINDS.get(Path(self.file).suffix.lower(), "photo")

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "enabled": self.enabled,
            "members": self.members,
            "kind": self.kind,
            "ext": Path(self.file).suffix.lower(),
            "file": self.file,  # changes when the image is replaced, so previews refresh
        }


def clean_label(label: str) -> str:
    label = re.sub(r"[\[\]\n:：]", "", label or "").strip()
    if not label:
        raise ConfigError("表情名称不能为空")
    if len(label) > 30:
        raise ConfigError("表情名称最多 30 个字")
    return label


def clean_members(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
        raise ConfigError("成员列表格式不对")
    return sorted({m.strip() for m in value if m.strip()})


def check_file(filename: str, data: bytes) -> str:
    """The file's extension, once it is known to be a usable sticker."""
    ext = Path(filename).suffix.lower()
    if ext not in KINDS:
        raise ConfigError(f"不支持 {ext or '无扩展名'} 文件，可用：{'、'.join(sorted(KINDS))}")
    if not data:
        raise ConfigError("文件是空的")
    if len(data) > MAX_FILE_BYTES:
        raise ConfigError("文件超过 10 MB")
    return ext


class StickerLibrary:
    def __init__(self, data_dir: Path, rng: random.Random | None = None):
        self.dir = data_dir / "stickers"
        self.index_path = data_dir / "stickers.json"
        self.rng = rng or random.Random()
        self.items: list[Sticker] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("表情库索引 %s 损坏，已忽略", self.index_path)
            return
        for entry in raw.get("stickers", []):
            s = Sticker(**{k: entry[k] for k in ("id", "file", "label", "description", "enabled", "members", "tg_ids") if k in entry})
            s.path = self.dir / s.file
            if s.path.exists():
                self.items.append(s)

    def save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"stickers": [{k: v for k, v in asdict(s).items() if k != "path"} for s in self.items]}
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    # ---- editing ----

    def get(self, sticker_id: str) -> Sticker:
        s = next((s for s in self.items if s.id == sticker_id), None)
        if s is None:
            raise ConfigError("没有这个表情")
        return s

    def add(self, filename: str, data: bytes, label: str, description: str = "",
            members: list[str] | None = None, tg_id: str = "") -> Sticker:
        ext = check_file(filename, data)
        sid = uuid.uuid4().hex[:12]
        s = Sticker(id=sid, file=sid + ext, label=clean_label(label), description=description.strip(),
                    members=clean_members(members or []), tg_ids=[tg_id] if tg_id else [])
        self.dir.mkdir(parents=True, exist_ok=True)
        s.path = self.dir / s.file
        s.path.write_bytes(data)
        self.items.append(s)
        self.save()
        return s

    def unique_label(self, label: str) -> str:
        """``label``, or ``label2``, ``label3``… if it is taken, so a new drawing is the one sent."""
        label = clean_label(label)
        taken = {s.label for s in self.items}
        candidate, n = label, 2
        while candidate in taken:
            candidate, n = f"{label}{n}", n + 1
        return candidate

    def update(self, sticker_id: str, changes: dict) -> Sticker:
        s = self.get(sticker_id)
        if "label" in changes:
            s.label = clean_label(changes["label"])
        if "description" in changes:
            s.description = str(changes["description"] or "").strip()
        if "enabled" in changes:
            s.enabled = bool(changes["enabled"])
        if "members" in changes:
            s.members = clean_members(changes["members"])
        self.save()
        return s

    def replace_file(self, sticker_id: str, filename: str, data: bytes) -> Sticker:
        """Swap the image, keeping the name, description and members."""
        s = self.get(sticker_id)
        ext = check_file(filename, data)
        old = s.path
        s.file = f"{s.id}-{uuid.uuid4().hex[:6]}{ext}"  # a new name, so cached Telegram file ids are not reused
        s.path = self.dir / s.file
        s.path.write_bytes(data)
        s.tg_ids = []  # the old image's ids no longer match
        old.unlink(missing_ok=True)
        self.save()
        return s

    def delete(self, sticker_id: str) -> None:
        s = self.get(sticker_id)
        self.items.remove(s)
        s.path.unlink(missing_ok=True)
        self.save()

    # ---- recognising stickers sent in Telegram ----

    def find_tg(self, unique_id: str) -> Sticker | None:
        return next((s for s in self.items if unique_id and unique_id in s.tg_ids), None)

    def remember_tg(self, sticker: Sticker, unique_id: str) -> None:
        if unique_id and unique_id not in sticker.tg_ids and sticker in self.items:
            sticker.tg_ids.append(unique_id)
            self.save()

    # ---- use in chat ----

    def labels(self, member_id: str | None = None) -> dict[str, list[Sticker]]:
        out: dict[str, list[Sticker]] = {}
        for s in self.items:
            if s.enabled and s.allowed(member_id):
                out.setdefault(s.label, []).append(s)
        return out

    def pick(self, label: str, member_id: str | None = None) -> Sticker | None:
        wanted = label.strip().lower()
        matches = [s for s in self.items if s.enabled and s.allowed(member_id) and s.label.lower() == wanted]
        return self.rng.choice(matches) if matches else None

    def prompt_section(self, limit: int, member_id: str | None = None) -> str:
        """Instructions appended to a member's system prompt; empty when it has nothing to send."""
        labels = self.labels(member_id)
        if not labels or limit < 1:
            return ""
        names = []
        for label, items in list(labels.items())[:MAX_LABELS_IN_PROMPT]:
            desc = next((s.description for s in items if s.description), "")
            names.append(f"{label}（{desc}）" if desc else label)
        return (
            "表情包：\n"
            "- 你可以偶尔发表情包，写法是 [表情:名称]，名称必须是下面列表里的一个。\n"
            f"- 只在气氛合适时用，大多数消息不需要；一条消息最多 {limit} 个。可以只发表情、不写文字。\n"
            f"- 可用的表情：{'、'.join(names)}"
        )

    def extract(self, text: str, limit: int, member_id: str | None = None) -> tuple[str, list[Sticker], str]:
        """Split a reply into (text to post, stickers to send, text for the transcript).

        Unknown labels, stickers not given to ``member_id`` and anything beyond ``limit`` are dropped.
        """
        picked: list[tuple[str, Sticker]] = []

        def take(m: re.Match) -> str:
            if len(picked) < limit:
                s = self.pick(m.group(1), member_id)
                if s is not None:
                    picked.append((s.label, s))
            return " "

        plain = TAG_RE.sub(take, text)
        plain = re.sub(r"[ \t]{2,}", " ", plain)
        plain = "\n".join(line.strip() for line in plain.splitlines()).strip()
        transcript = " ".join([plain, *(f"[表情:{label}]" for label, _ in picked)]).strip()
        return plain, [s for _, s in picked], transcript


def strip_tags(text: str) -> tuple[str, list[Sticker], str]:
    """extract() for when no library is available: drop every tag."""
    plain = TAG_RE.sub(" ", text)
    plain = "\n".join(line.strip() for line in re.sub(r"[ \t]{2,}", " ", plain).splitlines()).strip()
    return plain, [], plain


def parse_set_name(value: str) -> str:
    """Accept a sticker set name or a t.me/addstickers/<name> link."""
    value = value.strip().rstrip("/")
    m = re.search(r"addstickers/([A-Za-z0-9_]+)", value)
    name = m.group(1) if m else value
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        raise ConfigError("贴纸包名称不对，请填写名称或 t.me/addstickers/… 链接")
    return name
