"""Long-term memory: facts injected into the system prompt of every member (or one member).

Facts come from the admin page, from models writing [记住:…] in a reply, and from the nightly
consolidation: once a day has ended, a cheap model reads that day's group chat and writes down
what the user said about himself (job, plans, likes…), each with his own words as proof and,
when it updates an older fact, which one it replaces. Depending on the learn mode, these are
ignored, wait for approval, or are kept right away.

While all facts fit in PROMPT_BUDGET characters they all go into the system prompt. Beyond
that only the ones marked "常驻", the owner's own and the most recent go in; the rest come
back when the chat touches on them (``recall``).

    <data_dir>/memory.json
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .config import ConfigError
from .recall import topic_score

log = logging.getLogger(__name__)

LEARN_MODES = ("off", "review", "auto")
SCOPE_ALL = "all"
MAX_TEXT = 200
CONSOLIDATED = "整理"  # source of facts from the nightly consolidation
RUN_AFTER_HOUR = 23  # a day is read after 23:00, or any time later
LOOK_BACK_DAYS = 7  # a missed day is caught up within this long
MIN_USER_MESSAGES = 3  # the user hardly spoke: nothing to learn about him
MIN_QUOTE = 4
PROMPT_BUDGET = 1500  # characters of facts in the system prompt before the rest is only recalled
RECALL_LIMIT = 4

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
    source: str = "manual"  # "manual", the id of the member that suggested it, or CONSOLIDATED
    created: float = 0.0
    about: str = ""  # who it is about, e.g. "阿明"
    said_at: float = 0.0  # when it was said (consolidated facts)
    quote: str = ""  # the user's own words it rests on
    replaces: str = ""  # id of the older fact it updates; that one is switched off once this is kept
    core: bool = False  # always in the system prompt, however many facts there are


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
        self.done_days: list[str] = []  # days the nightly consolidation has read
        self.load()

    def load(self) -> None:
        self.items = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Fact(**item) for item in raw.get("items", [])]
            self.done_days = raw.get("consolidated_days", [])
        except (OSError, ValueError, TypeError):
            log.warning("记忆文件 %s 损坏，已忽略", self.path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"items": [asdict(f) for f in self.items], "consolidated_days": self.done_days}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, fact_id: str) -> Fact:
        fact = next((f for f in self.items if f.id == fact_id), None)
        if fact is None:
            raise ConfigError("没有这条记忆")
        return fact

    def _exists(self, text: str, scope: str) -> bool:
        key = text.casefold()
        return any(f.text.casefold() == key and f.scope in (scope, SCOPE_ALL) for f in self.items)

    def add(self, text: str, scope: str = SCOPE_ALL, *, status: str = "active", source: str = "manual",
            about: str = "", said_at: float = 0.0, quote: str = "", replaces: str = "") -> Fact | None:
        """Add a fact; returns None when an identical one already exists."""
        text = clean_text(text)
        if self._exists(text, scope):
            return None
        fact = Fact(id=uuid.uuid4().hex[:12], text=text, scope=scope or SCOPE_ALL, status=status, source=source,
                    created=time.time(), about=about, said_at=said_at, quote=quote, replaces=replaces)
        self.items.append(fact)
        self._retire(fact)
        self.save()
        return fact

    def _retire(self, fact: Fact) -> None:
        """A fact that updates an older one: an unapproved older one just goes; a kept one is
        switched off once the new one is kept too."""
        old = next((f for f in self.items if f.id == fact.replaces), None) if fact.replaces else None
        if old is None or old is fact:
            return
        if old.status == "pending":
            self.items.remove(old)
            fact.replaces = old.replaces  # what the dropped one was going to update, if anything
            self._retire(fact)
        elif fact.status == "active":
            old.enabled = False

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
        if "core" in changes:
            fact.core = bool(changes["core"])
        if changes.get("status") in ("active", "pending"):
            fact.status = changes["status"]
        self._retire(fact)
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

    def _visible(self, member_id: str) -> list[Fact]:
        return [f for f in self.items if f.enabled and f.status == "active" and f.scope in (SCOPE_ALL, member_id)]

    def in_prompt(self, member_id: str) -> list[Fact]:
        """The facts that fit in the system prompt: all of them, or the core, the owner's and the newest."""
        facts = self._visible(member_id)
        if sum(len(f.text) for f in facts) <= PROMPT_BUDGET:
            return facts
        ranked = sorted(facts, key=lambda f: (not f.core, f.source != "manual", -(f.said_at or f.created)))
        chosen, used = set(), 0
        for f in ranked:
            if f.core or used + len(f.text) <= PROMPT_BUDGET:
                chosen.add(f.id)
                used += len(f.text)
        return [f for f in facts if f.id in chosen]

    def recall(self, member_id: str, query: str, names: Sequence[str] = ()) -> str:
        """Facts left out of the system prompt that the current chat touches on."""
        shown = {f.id for f in self.in_prompt(member_id)}
        scored = [(topic_score(query, f.text, names), f) for f in self._visible(member_id) if f.id not in shown]
        picked = [f for score, f in sorted(scored, key=lambda x: x[0], reverse=True) if score][:RECALL_LIMIT]
        if not picked:
            return ""
        return ("[想起来的长期记忆]\n" + "\n".join("- " + _line(f) for f in picked)
                + "\n和刚才的话题有关，用得上就自然用，不必复述。[/长期记忆]")

    def prompt_section(self, member_id: str, learn_mode: str) -> str:
        facts = [_line(f) for f in self.in_prompt(member_id)]
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

    # ---- nightly consolidation ----

    def day_to_consolidate(self, days_with_chat: Sequence[str], now: datetime) -> str | None:
        """The oldest finished day, within the last week, that has not been read yet."""
        today = now.date().isoformat()
        oldest = datetime.fromordinal(now.date().toordinal() - LOOK_BACK_DAYS).date().isoformat()
        for day in sorted(set(days_with_chat)):
            if day in self.done_days or day < oldest:
                continue
            if day < today or (day == today and now.hour >= RUN_AFTER_HOUR):
                return day
        return None

    def record_consolidation(self, day: str, found: list[dict], mode: str, said_at: float) -> list[Fact]:
        """Keep what the consolidation found, per the learn mode; the day counts as read either way."""
        added = []
        if mode in ("review", "auto"):
            for item in found:
                try:
                    fact = self.add(item["text"], SCOPE_ALL, status="active" if mode == "auto" else "pending",
                                    source=CONSOLIDATED, about=item["about"], said_at=said_at, quote=item["quote"],
                                    replaces=item["replaces"])
                except ConfigError:
                    continue
                if fact:
                    added.append(fact)
        if day not in self.done_days:
            self.done_days = sorted([*self.done_days, day])[-60:]
        self.save()
        return added


def _line(f: Fact) -> str:
    return f.text + (f"（{datetime.fromtimestamp(f.said_at):%m-%d} 说的）" if f.said_at else "")


def _known(facts: Sequence[Fact]) -> list[Fact]:
    """What the consolidation compares against: kept facts and those still waiting for approval."""
    return [f for f in facts if f.enabled and f.status in ("active", "pending")]


def consolidation_prompt(day: str, lines: Sequence[str], known: Sequence[Fact], user_names: Sequence[str]) -> str:
    facts = [{"id": f.id, "text": f.text, **({"状态": "待确认"} if f.status == "pending" else {})} for f in _known(known)]
    return (
        f"下面是 {day} 一个朋友群里的聊天记录。标着（用户）的是群主 {'、'.join(user_names) or '用户'}，"
        "标着（群友）的是群主拉进群的朋友（他们说的事不是群主的，不要记），"
        "其余是群里的 AI 成员，她们在扮演各自的角色。\n"
        "从用户自己说的话里，整理出值得朋友长期记住的事实：身份、工作、收入、住在哪、家人朋友、作息习惯、"
        "喜欢和讨厌的东西、正在做的事、打算和约定、重要的经历。\n"
        "规则：\n"
        "- 只记用户亲口说的、关于他自己（或他身边的人）的事。成员们说的、玩笑、反话、假设、问句都不算。\n"
        "- 每条一句话，第三人称，带主语，用大家平时叫他的名字（看聊天和已有记忆里怎么叫）。要具体："
        "写「阿明月薪一万五左右」，不写「阿明有工作」。\n"
        f"- quote 抄一段用户的原话（至少 {MIN_QUOTE} 个字，一字不改），证明他确实这么说过。\n"
        "- 一次性的小事（今天吃了什么、几点睡）不记，除非能看出是习惯。\n"
        "- 已有的长期记忆在下面（标着待确认的是之前整理出来、还没确认的）。意思一样或被它包含的不要再写；"
        "新消息更新、补充或推翻了其中一条时，写一条合并后的新说法，replaces 填那条的 id。\n"
        "- 没有值得记的就返回空列表。\n"
        f"已有的长期记忆：{json.dumps(facts, ensure_ascii=False)}\n"
        "聊天记录：\n" + "\n".join(lines) + "\n"
        '只输出 JSON：[{"about":"阿明","text":"…","quote":"…","replaces":""}]'
    )


def parse_consolidation(raw: str, user_texts: Sequence[str], known: Sequence[Fact]) -> list[dict]:
    """Facts whose quote really is the user's own words that day; anything else is dropped."""
    match = re.search(r"\[.*\]", raw or "", re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    said = [" ".join(t.split()) for t in user_texts]
    ids = {f.id for f in _known(known)}
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        quote = " ".join(str(item.get("quote") or "").split())
        if not text or len(text) > MAX_TEXT or len(quote) < MIN_QUOTE or not any(quote in t for t in said):
            continue
        replaces = str(item.get("replaces") or "")
        out.append({"about": str(item.get("about") or "")[:20], "text": text, "quote": quote[:200],
                    "replaces": replaces if replaces in ids else ""})
    return out
