"""Memories of the games members played, with how they felt about them.

A climb (one Slay the Spire run, from floor 0 to death or victory) usually spans several
play sessions. When it ends, the facts are read from the recorded actions
(``games.story.signals``) and a cheap model writes 1–3 memory blocks in the member's
voice: the build that came together, the card that never came, the monster that killed it.

Feelings fade (half-life of a few days); the strongest recent ones colour the member's mood.
What keeps happening adds up to lasting opinions ("力量流太看脸", "跟 Jaw Worm 有仇"),
which also go into the member's next game.

After a climb the member also decides, in its own voice, what next: another round right
away, later, put the game aside for a few days, ask a friend how to beat it, or wish for a
different game. That intention is kept and followed (a game put aside is not offered in its
daily life until the time is up), so playing is its own choice rather than the owner's.
Stored in ``game_memories.json``.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .config import ConfigError
from .games.spire_bridge import atomic_json
from .recall import topic_score

KINDS = ("流派", "遭遇", "手气", "时刻", "成绩")
FEELINGS = {  # feeling -> icon on the admin page
    "兴奋": "🤩", "得意": "😎", "满足": "😌", "好笑": "😂",
    "遗憾": "😞", "郁闷": "😮‍💨", "不服": "😤", "记仇": "😠", "后怕": "😰", "无语": "😑",
}
SOUR = {"遗憾", "郁闷", "不服", "记仇", "无语"}
HALF_LIFE_DAYS = 3.0
MOOD_MIN = 0.4  # memories at least this strong right now set the member's mood
MAX_PER_MEMBER = 200
MAX_STORY = 60
MEME_AFTER = 3  # the same sour memory this many times: suggest it for the meme library
# What a member may decide after a climb, and how long the decision holds.
CHOICES = {"again": "马上再来一局", "later": "过阵子再玩", "rest": "这游戏先放几天",
           "help": "想问问朋友怎么打", "other": "想玩点别的游戏"}
HOLDS = {"again": 3 / 24, "later": 1, "rest": 2, "help": 2, "other": 3}  # days, unless it said how many
BLOCKS = {"rest", "other"}  # these keep the game out of its daily life while they hold
GAME_WORDS = re.compile(r'游戏|杀戮|爬塔|尖塔|spire|steam|流派|卡组|牌组|boss|玩[了过的啥什]|忙[啥什]|近况|干[嘛啥什]', re.I)
_NUMBER = re.compile(r"\d+")


@dataclass
class GameMemory:
    id: str
    member: str
    climb: str
    kind: str
    subject: str
    feeling: str
    intensity: float
    story: str
    facts: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    at: float = 0.0
    status: str = "active"  # active / pinned (never fades) / hidden (kept for stats only)
    watched: str = ""  # whose game it was, when the member only watched

    def strength(self, now: float) -> float:
        if self.status == "pinned":
            return self.intensity
        days = max(0.0, now - self.at) / 86400
        return self.intensity * 0.5 ** (days / HALF_LIFE_DAYS)


class GameMemoryStore:
    def __init__(self, data_dir: Path, now: Callable[[], float] = time.time):
        self.path = data_dir / "game_memories.json"
        self.now = now
        self.memories: list[GameMemory] = []
        self.opinions: dict[str, dict] = {}
        self.climbs: dict[str, dict] = {}
        self.nominated: list[str] = []
        self.intentions: dict[str, dict] = {}  # member -> what it decided after its latest climb
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.memories = [GameMemory(**m) for m in data.get("memories", [])]
            self.opinions = data.get("opinions", {})
            self.climbs = data.get("climbs", {})
            self.nominated = data.get("nominated", [])
            self.intentions = data.get("intentions", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, {"memories": [asdict(m) for m in self.memories], "opinions": self.opinions,
                                "climbs": self.climbs, "nominated": self.nominated, "intentions": self.intentions})

    # ---- climbs ----

    def attach(self, receipt: dict) -> tuple[str, bool]:
        """Put one play session into its climb; returns (climb id, whether the climb just ended)."""
        rid, member = receipt["id"], receipt["member"]
        for cid, climb in self.climbs.items():
            if rid in climb["receipts"]:
                return cid, False
        first = receipt.get("first") or {}
        open_climbs = [c for c, v in self.climbs.items() if v["member"] == member and not v["ended"]]
        if first.get("floor") == 0 or not open_climbs:
            for c in open_climbs:  # started over: the old one was abandoned
                self.climbs[c]["ended"] = True
            cid = "c_" + rid
            self.climbs[cid] = {"member": member, "receipts": [], "ended": False, "remembered": False,
                                "started_at": receipt.get("started_at", self.now())}
        else:
            cid = max(open_climbs, key=lambda c: self.climbs[c]["started_at"])
        climb = self.climbs[cid]
        climb["receipts"].append(rid)
        if receipt.get("stopped_because") == "game_over" or receipt.get("status") == "finished":
            climb["ended"] = True
            climb["game_over"] = receipt.get("game_over") or {}
        self.save()
        return cid, climb["ended"]

    # ---- memories ----

    def add(self, blocks: Sequence[dict]) -> list[GameMemory]:
        added = []
        for b in blocks:
            m = GameMemory(uuid.uuid4().hex[:12], b["member"], b["climb"], b["kind"], b["subject"], b["feeling"],
                           round(float(b["intensity"]), 2), b["story"], b.get("facts", {}), b.get("tags", []),
                           b.get("at") or self.now(), watched=b.get("watched", ""))
            self.memories.append(m)
            added.append(m)
        for member in {m.member for m in added}:
            mine = [m for m in self.memories if m.member == member]
            if len(mine) > MAX_PER_MEMBER:
                now = self.now()
                drop = sorted((m for m in mine if m.status != "pinned"), key=lambda m: m.strength(now))
                gone = {m.id for m in drop[: len(mine) - MAX_PER_MEMBER]}
                self.memories = [m for m in self.memories if m.id not in gone]
        self.save()
        return added

    def items(self, member: str | None = None) -> list[GameMemory]:
        return [m for m in self.memories if member is None or m.member == member]

    def get(self, mid: str) -> GameMemory:
        for m in self.memories:
            if m.id == mid:
                return m
        raise ConfigError("没有这条游戏记忆")

    def set_status(self, mid: str, status: str) -> GameMemory:
        if status not in ("active", "pinned", "hidden"):
            raise ConfigError("不认识的状态")
        m = self.get(mid)
        m.status = status
        self.save()
        return m

    def edit(self, mid: str, story: str) -> GameMemory:
        story = " ".join((story or "").split())
        if not story:
            raise ConfigError("记忆内容不能为空")
        if len(story) > MAX_STORY * 2:
            raise ConfigError(f"一条记忆最多 {MAX_STORY * 2} 个字")
        m = self.get(mid)
        m.story = story
        self.save()
        return m

    def delete(self, mid: str) -> None:
        self.get(mid)
        self.memories = [m for m in self.memories if m.id != mid]
        self.save()

    def _shown(self, member: str) -> list[GameMemory]:
        return [m for m in self.memories if m.member == member and m.status != "hidden"]

    # ---- opinions: what keeps happening ----

    def record_climb(self, member: str, sig: dict) -> dict:
        """Add a finished climb to the member's tallies; returns what stood out compared to before."""
        op = self.opinions.setdefault(member, {"builds": {}, "monsters": {}, "wanted": {}, "best_floor": 0, "climbs": 0})
        now = self.now()
        floor = sig.get("floor") or 0
        stood_out = {"new_record": op["climbs"] > 0 and floor > op["best_floor"], "nemesis": ""}
        op["climbs"] += 1
        op["best_floor"] = max(op["best_floor"], floor)
        build = sig.get("build")
        if build:
            b = op["builds"].setdefault(build["name"], {"tries": 0, "formed": 0, "deaths": 0, "best_floor": 0})
            b["tries"] += 1
            b["formed"] += bool(build["formed"])
            b["deaths"] += bool(sig.get("died"))
            b["best_floor"] = max(b["best_floor"], floor)
            b["at"] = now
        for fight in sig.get("fights", []):
            for name in fight.get("monsters", []):
                op["monsters"].setdefault(name, {"met": 0, "killed_me": 0})["met"] += 1
        for name in sig.get("killer", []):
            mon = op["monsters"].setdefault(name, {"met": 1, "killed_me": 0})
            mon["killed_me"] += 1
            mon["at"] = now
            if mon["killed_me"] >= 2:
                stood_out["nemesis"] = name
        for card in sig.get("wanted", []):
            w = op["wanted"].setdefault(card, {"count": 0})
            w["count"] += 1
            w["at"] = now
        self.save()
        return stood_out

    def clear_opinion(self, member: str, subject: str) -> None:
        op = self.opinions.get(member, {})
        for part in ("builds", "monsters", "wanted"):
            op.get(part, {}).pop(subject, None)
        self.save()

    def opinion_lines(self, member: str, limit: int = 3) -> list[str]:
        op = self.opinions.get(member)
        if not op:
            return []
        lines: list[tuple[float, str]] = []
        for name, b in op["builds"].items():
            if b["tries"] >= 3 and b["formed"] / b["tries"] < 0.3:
                lines.append((b.get("at", 0), f"觉得{name}流太看脸（试了 {b['tries']} 次只成过 {b['formed']} 次）"))
            elif b["formed"] and b["best_floor"] >= op["best_floor"] > 0:
                lines.append((b.get("at", 0), f"最喜欢{name}流（打得最远的一次就是它）"))
        for name, mon in op["monsters"].items():
            if mon["killed_me"] >= 2:
                lines.append((mon.get("at", 0), f"跟 {name} 有仇（被它打死过 {mon['killed_me']} 次）"))
        for card, w in op["wanted"].items():
            if w["count"] >= 2:
                lines.append((w.get("at", 0), f"每次都等不到 {card}"))
        return [text for _, text in sorted(lines, reverse=True)[:limit]]

    def opinions_view(self, member: str) -> dict:
        """For the admin page: the tallies plus the sentences they turn into."""
        op = self.opinions.get(member, {})
        return {"lines": self.opinion_lines(member, 10), "plan": self.intention_line(member),
                "best_floor": op.get("best_floor", 0),
                "climbs": op.get("climbs", 0), "subjects": sorted({*op.get("builds", {}), *op.get("monsters", {}),
                                                                   *op.get("wanted", {})})}

    # ---- what it wants to do next ----

    def decide(self, member: str, decision: dict) -> dict:
        now = self.now()
        days = decision.get("days") or HOLDS[decision["choice"]]
        intent = {**decision, "at": now, "until": now + days * 86400, "status": "active"}
        self.intentions[member] = intent
        self.save()
        return intent

    def intention(self, member: str) -> dict | None:
        intent = self.intentions.get(member)
        if not intent or intent["status"] != "active" or intent["until"] <= self.now():
            return None
        return intent

    def resting(self, member: str) -> bool:
        """It put the game aside for now: daily life does not offer it."""
        intent = self.intention(member)
        return bool(intent and intent["choice"] in BLOCKS)

    def done(self, member: str) -> None:
        """It played again: the old decision no longer holds."""
        intent = self.intentions.get(member)
        if intent and intent["status"] == "active":
            intent["status"] = "done"
            self.save()

    def intention_line(self, member: str) -> str:
        intent = self.intention(member)
        if not intent:
            return ""
        until = time.strftime("%m-%d %H:%M", time.localtime(intent["until"]))
        return f"你上一局打完时的打算：{CHOICES[intent['choice']]}（{intent['why']}），到 {until} 前有效。"

    def play_note(self, member: str) -> str:
        """For its daily-life choice: its own plan, which it may keep or change for a reason."""
        line = self.intention_line(member)
        return ("\n" + line + "照自己的打算来；真想改主意也行，但要有缘由。") if line else ""

    # ---- into prompts ----

    def mood(self, member: str) -> str:
        now = self.now()
        strong = sorted((m for m in self._shown(member) if m.strength(now) >= MOOD_MIN),
                        key=lambda m: m.strength(now), reverse=True)[:2]
        if not strong:
            return ""
        return "（你最近的游戏心情：" + "；".join(f"{m.story}，{m.feeling}" for m in strong) + "。）"

    def prompt_section(self, member: str) -> str:
        """Mood and opinions for the member's system prompt; colour, not something to bring up."""
        parts = []
        mood = self.mood(member)
        if mood:
            parts.append(mood)
        lines = self.opinion_lines(member)
        if lines:
            parts.append("你玩《杀戮尖塔》攒下的看法：" + "；".join(lines) + "。")
        plan = self.intention_line(member)
        if plan:
            parts.append(plan)
        if not parts:
            return ""
        parts.append("这些只影响你的语气和想不想提，不用主动说。")
        return "\n".join(parts)

    def recall(self, member: str, query: str, names: Sequence[str] = ()) -> str:
        """Memories the current chat brings back: matching tags first, then the latest when games come up."""
        now = self.now()
        q = query.casefold()
        scored = []
        for m in self._shown(member):
            text = " ".join([*m.tags, m.subject, m.story])
            score = topic_score(query, text, names)
            if any(len(t) >= 2 and t.casefold() in q for t in [*m.tags, m.subject]):
                score = max(score, 10)
            if score:
                scored.append((score * m.strength(now), m))
        picked = [m for _, m in sorted(scored, key=lambda x: x[0], reverse=True)[:2]]
        if not picked and GAME_WORDS.search(query):
            picked = sorted(self._shown(member), key=lambda m: m.at)[-2:]
        if not picked:
            return ""
        lines = []
        for m in picked:
            day = time.strftime("%m-%d", time.localtime(m.at))
            whose = f"（看 {m.watched} 玩的）" if m.watched else ""
            lines.append(f"- {day}{whose} {m.feeling}：{m.story}")
        return ("[你的游戏记忆]\n" + "\n".join(lines)
                + "\n这是你自己玩过（或看朋友玩过）的真实经历和当时的感受。想提可以随口提一句，不想就不提；"
                  "不要补出记录里没有的胜利、关卡或牌。[/游戏记忆]")

    def profile(self, member: str) -> str:
        """The player's own habits, for the start of its next game."""
        op = self.opinions.get(member)
        lines = self.opinion_lines(member)
        if not op or (not lines and not op.get("best_floor")):
            return ""
        best = f"最远打到第 {op['best_floor']} 层" if op.get("best_floor") else ""
        said = "；".join([*lines, best] if best else lines)
        out = f"你以前玩过 {op['climbs']} 局：{said}。这些是你自己的偏好，可以照着来，也可以这次偏要试试别的。\n"
        if self.resting(member):  # the owner started it anyway
            out += self.intention_line(member) + "这次是阿明让你开的，玩得开不开心照实说，不想玩可以 pause。\n"
        return out

    def nominations(self, member: str, name: str) -> list[str]:
        """Sour memories that keep coming back, worded as a group joke; each suggested once."""
        counts: dict[tuple[str, str], int] = {}
        for m in self.memories:
            if m.member == member and m.feeling in SOUR and not m.watched:
                counts[(m.kind, m.subject)] = counts.get((m.kind, m.subject), 0) + 1
        out = []
        for (kind, subject), n in counts.items():
            key = f"{member}|{kind}|{subject}"
            if n < MEME_AFTER or key in self.nominated:
                continue
            self.nominated.append(key)
            if kind == "流派":
                out.append(f"{name}的{subject}从来没成过")
            elif kind == "遭遇":
                out.append(f"{name}又死在 {subject} 手里了")
            elif kind == "手气":
                out.append(f"{name}永远等不到 {subject}")
            else:
                out.append(f"{name}又{subject}了")
        if out:
            self.save()
        return out


# ---- deciding what next ----

def decision_prompt(name: str, persona: str, sig: dict, mood: str, opinions: Sequence[str]) -> str:
    facts = {k: sig.get(k) for k in ("floor", "died", "won_game", "killer", "death_floor", "build", "wanted",
                                     "close_calls", "stood_out")}
    return (
        f"你是 {name}。你刚打完一局《杀戮尖塔》。\n你的性格：{persona[:300]}\n"
        + (mood + "\n" if mood else "")
        + (f"你攒下的看法：{'；'.join(opinions)}\n" if opinions else "")
        + "这局的真实记录：" + json.dumps(facts, ensure_ascii=False) + "\n"
        "现在你自己怎么想？按你的性格和这局的真实经历来定：不是每次都要再来，也不是输了就一定不玩。"
        "差一点就过了的人常想马上再来；连着输、一直卡住会烦；赢了可能想乘胜追击，也可能心满意足歇会儿。\n"
        f"choice 只能是：{'；'.join(f'{k}={v}' for k, v in CHOICES.items())}。\n"
        "days：rest 或 other 时想放几天（1 到 7），其他可不填。\n"
        "why：一句话，不超过 30 个字，你自己的理由。\n"
        "say：想发群里的一句话，可以空。help 时写你想问大家的具体问题；other 时可以说想玩什么、问阿明能不能装。"
        "只能提记录里有的牌名、敌人名和数字；平时大多数时候不用发。\n"
        '只输出 JSON：{"choice":"…","days":null,"why":"…","say":""}'
    )


def parse_decision(raw: str, sig: dict) -> dict | None:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    choice = data.get("choice")
    why = " ".join(str(data.get("why") or "").split())[:60]
    say = " ".join(str(data.get("say") or "").split())[:120]
    if choice not in CHOICES or not why:
        return None
    if say and not _check_story(say, sig):
        say = ""  # don't post something the climb didn't have
    days = data.get("days")
    days = min(7, max(1, int(days))) if isinstance(days, (int, float)) and choice in BLOCKS else None
    return {"choice": choice, "days": days, "why": why, "say": say}


def fallback_decision(sig: dict) -> dict:
    """When the model's answer was unusable: a plain guess from what happened."""
    stood = sig.get("stood_out") or {}
    if sig.get("won_game"):
        return {"choice": "later", "days": None, "why": "打通了，先歇会儿", "say": ""}
    if stood.get("nemesis"):
        return {"choice": "rest", "days": 2, "why": f"又输给 {stood['nemesis']}，先不想打了", "say": ""}
    if sig.get("close_calls") or stood.get("new_record"):
        return {"choice": "again", "days": None, "why": "差一点，想再来", "say": ""}
    return {"choice": "later", "days": None, "why": "这局结束了，过阵子再说", "say": ""}


# ---- writing the memories ----

def memory_prompt(name: str, persona: str, sig: dict, recent: Sequence[GameMemory], watched: str = "") -> str:
    facts = {k: sig.get(k) for k in ("class", "floor", "act", "hp", "max_hp", "died", "won_game", "killer",
                                     "death_floor", "hp_before_death_fight", "build", "wanted", "close_calls",
                                     "key_plays", "skipped_rewards", "stood_out")}
    who = (f"你是 {name}。你刚才在旁边看 {watched} 玩《杀戮尖塔》，下面是你看到的那部分的真实记录。"
           if watched else f"你是 {name}。你刚打完一局《杀戮尖塔》，下面是这局的真实记录（程序从操作日志里算出来的）。")
    before = [f"{m.feeling}：{m.story}" for m in recent[-5:]]
    return (
        who + f"\n你的性格：{persona[:300]}\n"
        "把这局（或这段）里你自己会记住的事写成 1 到 3 条记忆，用你自己的口吻，像过后跟朋友提起时会说的一句话。\n"
        "规则：\n"
        f"- kind 只能是：{'、'.join(KINDS)}。流派=这局走了什么、成没成；遭遇=某个敌人或事件；手气=等不到的牌或天降好牌；"
        "时刻=险胜、关键一手；成绩=新纪录、死得特别早。\n"
        f"- feeling 只能是：{'、'.join(FEELINGS)}。\n"
        "- subject 是这条记忆的主角：流派名（如「力量流」）、敌人名、牌名，照记录里的写法。\n"
        f"- story 一句话，不超过 40 个字。只能用记录里有的牌名、敌人名和数字，不要补出没发生的事。\n"
        "- intensity 是 0 到 1 的小数，这件事让你多上心。\n"
        "- tags 是 2 到 5 个关键词：流派、牌名、敌人名。\n"
        "- 没什么值得记的就返回空列表。\n"
        + (f"你以前的游戏记忆（别重复一样的话）：{json.dumps(before, ensure_ascii=False)}\n" if before else "")
        + "记录：" + json.dumps(facts, ensure_ascii=False)
        + '\n只输出 JSON：[{"kind":"…","subject":"…","feeling":"…","intensity":0.6,"story":"…","tags":["…"]}]'
    )


def _check_story(story: str, sig: dict) -> bool:
    folded = story.casefold()
    allowed = {n.casefold() for n in sig.get("fact_names", [])}
    for n in sig.get("names_seen", []):
        if len(n) >= 3 and n.casefold() in folded and n.casefold() not in allowed:
            return False  # a card or monster that was around but isn't part of what happened
    numbers = {str(n) for n in sig.get("numbers", [])}
    return all(x in numbers for x in _NUMBER.findall(story))


def parse_memories(raw: str, sig: dict, member: str, climb: str, watched: str = "") -> list[dict]:
    """Valid blocks from the model's answer; anything not backed by the facts is dropped."""
    match = re.search(r"\[.*\]", raw or "", re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    out = []
    bonus = 0.2 if (sig.get("stood_out") or {}).get("new_record") or (sig.get("stood_out") or {}).get("nemesis") else 0.0
    for item in items[:3] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        story = " ".join(str(item.get("story", "")).split())
        kind, feeling = item.get("kind"), item.get("feeling")
        if kind not in KINDS or feeling not in FEELINGS or not story or len(story) > MAX_STORY:
            continue
        if not _check_story(story, sig):
            continue
        try:
            intensity = min(1.0, max(0.1, float(item.get("intensity", 0.5)) + bonus))
        except (TypeError, ValueError):
            intensity = 0.5
        tags = [str(t)[:30] for t in item.get("tags", []) if str(t).strip()][:5] if isinstance(item.get("tags"), list) else []
        out.append({"member": member, "climb": climb, "kind": kind, "subject": str(item.get("subject") or kind)[:30],
                    "feeling": feeling, "intensity": intensity, "story": story, "tags": tags,
                    "facts": _facts(sig), "watched": watched})
    return out


def _facts(sig: dict) -> dict:
    build = sig.get("build") or {}
    return {"floor": sig.get("floor"), "act": sig.get("act"), "died": sig.get("died"), "won_game": sig.get("won_game"),
            "killer": sig.get("killer"), "build": build.get("name"), "formed": build.get("formed")}


def template_memories(sig: dict, member: str, climb: str, watched: str = "") -> list[dict]:
    """Plain wording straight from the facts, when the model's answer was unusable."""
    base = {"member": member, "climb": climb, "facts": _facts(sig), "watched": watched}
    stood = sig.get("stood_out") or {}
    build = sig.get("build")
    floor = sig.get("floor")
    out = []
    if sig.get("won_game"):
        out.append({**base, "kind": "成绩", "subject": "通关", "feeling": "得意", "intensity": 0.9,
                    "story": f"打通了，最后在第 {floor} 层", "tags": ["通关"]})
    elif sig.get("died") and sig.get("killer"):
        killer = sig["killer"][0]
        feeling = "记仇" if stood.get("nemesis") == killer else "不服"
        out.append({**base, "kind": "遭遇", "subject": killer, "feeling": feeling, "intensity": 0.6,
                    "story": f"第 {sig.get('death_floor')} 层死在 {killer} 手里", "tags": [killer]})
    if build:
        formed = build["formed"]
        out.append({**base, "kind": "流派", "subject": f"{build['name']}流", "feeling": "兴奋" if formed else "遗憾",
                    "intensity": 0.6 if formed else 0.5,
                    "story": f"{build['name']}流{'成型了' if formed else '没成型'}，打到第 {floor} 层",
                    "tags": [build["name"], *build["cards"][:3]]})
    if sig.get("wanted"):
        card = sig["wanted"][0]
        out.append({**base, "kind": "手气", "subject": card, "feeling": "郁闷", "intensity": 0.4,
                    "story": f"一直等 {card}，一张都没来", "tags": [card]})
    if stood.get("new_record") and not sig.get("won_game"):
        out.append({**base, "kind": "成绩", "subject": "新纪录", "feeling": "得意", "intensity": 0.7,
                    "story": f"打到第 {floor} 层，是我最远的一次", "tags": ["纪录"]})
    return [{**b, "intensity": min(1.0, b["intensity"] + (0.2 if stood.get("nemesis") or stood.get("new_record") else 0))}
            for b in out[:3]]
