"""Long-term memory: facts injected into the system prompt of every member (or one member).

Facts come from the admin page, or from models writing [记住:…] in a reply. Depending on
the learn mode, model suggestions are ignored, wait for approval, or are kept right away.

    <data_dir>/memory.json
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import ConfigError

log = logging.getLogger(__name__)

LEARN_MODES = ("off", "review", "auto")
SCOPE_ALL = "all"
MAX_TEXT = 200

TAG_RE = re.compile(r"\[\s*(?:记住|remember)\s*[:：]\s*([^\]\n]{1,%d}?)\s*\]" % MAX_TEXT, re.IGNORECASE)

LEARN_INSTRUCTIONS = (
    "- 如果用户透露了值得长期记住的信息（身份、偏好、重要的人和事、约定），"
    "可以在回复末尾写 [记住:一句话描述]，例如 [记住:阿明不吃香菜]。"
    "不要记琐碎或一次性的事，也不要重复已经在长期记忆里的内容。这个标记不会显示在群里。"
)


@dataclass
class Fact:
    id: str
    text: str
    scope: str = SCOPE_ALL  # "all" or a member id
    enabled: bool = True
    status: str = "active"  # "active" or "pending" (suggested by a model, waiting for approval)
    source: str = "manual"  # "manual" or the id of the member that suggested it
    created: float = 0.0


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise ConfigError("记忆内容不能为空")
    if len(text) > MAX_TEXT:
        raise ConfigError(f"一条记忆最多 {MAX_TEXT} 个字")
    return text


def extract_memories(text: str) -> tuple[str, list[str]]:
    """Remove [记住:…] tags from a reply; return (text without tags, suggested facts)."""
    found = [m.group(1).strip() for m in TAG_RE.finditer(text)]
    if not found:
        return text, []
    rest = TAG_RE.sub(" ", text)
    rest = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in rest.splitlines()).strip()
    return rest, [f for f in found if f]


class MemoryStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "memory.json"
        self.items: list[Fact] = []
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Fact(**item) for item in raw.get("items", [])]
        except (OSError, ValueError, TypeError):
            log.warning("记忆文件 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": [asdict(f) for f in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, fact_id: str) -> Fact:
        fact = next((f for f in self.items if f.id == fact_id), None)
        if fact is None:
            raise ConfigError("没有这条记忆")
        return fact

    def _exists(self, text: str, scope: str) -> bool:
        key = text.casefold()
        return any(f.text.casefold() == key and f.scope in (scope, SCOPE_ALL) for f in self.items)

    def add(self, text: str, scope: str = SCOPE_ALL, *, status: str = "active", source: str = "manual") -> Fact | None:
        """Add a fact; returns None when an identical one already exists."""
        text = clean_text(text)
        if self._exists(text, scope):
            return None
        fact = Fact(id=uuid.uuid4().hex[:12], text=text, scope=scope or SCOPE_ALL, status=status, source=source, created=time.time())
        self.items.append(fact)
        self.save()
        return fact

    def suggest(self, texts: list[str], source: str, mode: str) -> list[Fact]:
        """Facts proposed by a model, handled according to the learn mode."""
        if mode not in ("review", "auto"):
            return []
        added = []
        for text in texts:
            try:
                fact = self.add(text, SCOPE_ALL, status="active" if mode == "auto" else "pending", source=source)
            except ConfigError:
                continue
            if fact:
                added.append(fact)
                log.info("%s 提议记住：%s（%s）", source, fact.text, "已记住" if mode == "auto" else "待确认")
        return added

    def update(self, fact_id: str, changes: dict) -> Fact:
        fact = self.get(fact_id)
        if "text" in changes:
            fact.text = clean_text(changes["text"])
        if "scope" in changes:
            fact.scope = str(changes["scope"] or SCOPE_ALL)
        if "enabled" in changes:
            fact.enabled = bool(changes["enabled"])
        if changes.get("status") in ("active", "pending"):
            fact.status = changes["status"]
        self.save()
        return fact

    def delete(self, fact_id: str) -> None:
        self.items.remove(self.get(fact_id))
        self.save()

    def drop_scope(self, member_ids: set[str]) -> None:
        """Facts scoped to members that no longer exist become shared ones."""
        changed = False
        for f in self.items:
            if f.scope != SCOPE_ALL and f.scope not in member_ids:
                f.scope = SCOPE_ALL
                changed = True
        if changed:
            self.save()

    def prompt_section(self, member_id: str, learn_mode: str) -> str:
        facts = [
            f.text for f in self.items
            if f.enabled and f.status == "active" and f.scope in (SCOPE_ALL, member_id)
        ]
        lines = []
        if facts:
            lines.append("长期记忆（确定的事实，聊天时自然地用上，不必刻意复述）：")
            lines += [f"- {t}" for t in facts]
        if learn_mode in ("review", "auto"):
            if not facts:
                lines.append("长期记忆：")
            lines.append(LEARN_INSTRUCTIONS)
        return "\n".join(lines)

    def pending_count(self) -> int:
        return sum(1 for f in self.items if f.status == "pending")
