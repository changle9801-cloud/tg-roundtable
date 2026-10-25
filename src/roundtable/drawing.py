"""Members drawing stickers for each other.

A member that can draw writes [画:表情名|画面描述] in a reply; the tag is hidden from the
group, the picture is drawn in the background, added to the sticker library and handed
to whoever asked for it.

    <data_dir>/drawings.json   what was drawn, for whom, and today's count
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from .config import ParticipantConfig

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"\[\s*画\s*[:：]\s*([^|｜\]\n]{1,20}?)\s*[|｜]\s*([^\]]{1,400}?)\s*\]")
KEPT = 200

STYLE = (
    "风格要求：Q 版卡通表情包，方形构图，纯白背景，线条干净，颜色鲜明，人物表情夸张有趣；"
    "如果画面里有文字，用大号粗体中文，字要写对；内容健康，不画真实存在的人。"
)


def supports_drawing(pc: ParticipantConfig) -> bool:
    return pc.kind == "codex_cli"


def extract_draw_requests(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove [画:名称|描述] tags; return (text without them, [(label, description)])."""
    found = [(m.group(1).strip(), m.group(2).strip()) for m in TAG_RE.finditer(text)]
    if not found:
        return text, []
    rest = TAG_RE.sub(" ", text)
    rest = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in rest.splitlines()).strip()
    return rest, found


def drawer_instructions(remaining: int) -> str:
    if remaining <= 0:
        return "画表情包：你今天已经画不动了，有人请你画，就用你的口吻说明天再说。"
    return (
        "画表情包：\n"
        "- 你会画表情包。群里有人请你画时，如果你愿意，就在回复末尾写 [画:表情名|画面描述]，"
        "群里看不到这个标记，图会在后台画好（大约一分钟），存进群表情库，之后大家用 [表情:表情名] 就能发。\n"
        "- 表情名 2 到 8 个字；画面描述要具体：画谁、什么动作和表情、图上写什么字。画群里成员时按下面的外形来。\n"
        "- 回复里正常说一句接活的话就行（比如「行，等我一会儿」），不要提什么标记或系统。一次只画一张。\n"
        f"- 只在有人请你时才画，不要主动给人画。只画 Q 版卡通表情包，内容健康，不画真人。今天你还能画 {remaining} 张。"
    )


def requester_instructions(drawer_name: str) -> str:
    return (
        f"群里的 {drawer_name} 会画表情包。真的想要某个表情包时（比如想怼回去、想有个专属表情），"
        f"可以 @{drawer_name} 用大白话说想要什么；朋友之间偶尔帮个忙就好，别动不动就找他。"
    )


def appearance_section(members: Sequence[ParticipantConfig]) -> str:
    lines = [f"- {m.name}：{m.appearance}" for m in members if m.appearance]
    return "群里成员的外形（画他们时照这个来）：\n" + "\n".join(lines) if lines else ""


def build_draw_prompt(label: str, description: str, members: Sequence[ParticipantConfig]) -> str:
    parts = [f"画一张表情包，名字叫「{label}」。", f"画面：{description}"]
    looks = appearance_section(members)
    if looks:
        parts.append(looks)
    parts.append(STYLE)
    parts.append("直接画出来，不用解释，也不要追问。")
    return "\n".join(parts)


@dataclass
class Drawing:
    ts: float
    drawer: str
    requester: str
    label: str
    description: str
    ok: bool


class DrawLog:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "drawings.json"
        self.items: list[Drawing] = []
        self.busy: set[str] = set()  # drawers working on a picture right now
        if self.path.exists():
            try:
                self.items = [Drawing(**d) for d in json.loads(self.path.read_text(encoding="utf-8")).get("items", [])]
            except (OSError, ValueError, TypeError):
                log.warning("画图记录 %s 损坏，已忽略", self.path)

    def today(self, drawer: str, now: float | None = None) -> int:
        day = time.strftime("%Y-%m-%d", time.localtime(now or time.time()))
        return sum(1 for d in self.items if d.drawer == drawer and time.strftime("%Y-%m-%d", time.localtime(d.ts)) == day)

    def remaining(self, pc: ParticipantConfig) -> int:
        return max(0, pc.draws_per_day - self.today(pc.id))

    def record(self, drawing: Drawing) -> None:
        self.items = [*self.items, drawing][-KEPT:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": [asdict(d) for d in self.items]}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
