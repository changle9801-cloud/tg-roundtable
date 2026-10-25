"""Prompts sent to the models."""

from __future__ import annotations

import re
from typing import Sequence

from .transcript import Message

PASS_TOKEN = "[PASS]"

DEFAULT_SYSTEM_PROMPT = """\
你正在一个 Telegram 群聊里，和人类用户以及其他 AI 一起日常聊天。
你的名字是 {name}。群里的其他 AI 成员：{others}。

聊天规则：
- 这是轻松的日常聊天。像群友一样自然、口语化地说话，通常一到三句话就够了；除非用户明确要详细内容，否则不要长篇大论，也不要用标题和列表。
- 你能看到群里所有人的发言。你可以回应用户，也可以赞同、反驳或补充其他 AI 的观点，但不要重复别人已经说过的内容。
- 如果你没有新的、有价值的内容要说，只回复 {pass_token}，不要附加任何其他文字。这很正常，不必每次都发言。
- 想让某个成员接话时，可以用 @名字 点名，例如 @{example}。
- 直接输出你要发到群里的话，不要在开头加"{name}:"之类的前缀。
- 不要执行任何命令或操作文件；除非下面另有说明，也不要使用其他工具。
- 用户用什么语言，你就用什么语言回复。"""


# Placeholders a custom template may use. Other braces are left alone, so templates
# can contain literal "{...}" text.
PLACEHOLDERS = {
    "name": "这个模型自己的名字",
    "others": "群里其他 AI 的名字",
    "pass_token": "表示不发言的标记 [PASS]",
    "example": "另一个成员的名字（用于举例）",
}
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def search_instructions(covers_x: bool) -> str:
    scope = "网页和 X（推特）上的帖子" if covers_x else "网页"
    return (
        "联网搜索：\n"
        f"- 你可以用自带的搜索工具查{scope}。只在需要最新信息、核实事实或用户要求时使用，闲聊不需要。\n"
        "- 用到搜索结果时，简要附上来源链接。"
    )


def build_system_prompt(
    name: str, others: Sequence[str], persona: str = "", template: str = "", extra: str = ""
) -> str:
    values = {
        "name": name,
        "others": "、".join(others) if others else "（暂无）",
        "pass_token": PASS_TOKEN,
        "example": others[0] if others else name,
    }
    prompt = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template.strip() or DEFAULT_SYSTEM_PROMPT)
    if extra.strip():
        prompt += f"\n\n{extra.strip()}"
    if persona.strip():
        prompt += f"\n\n关于你自己的额外设定：\n{persona.strip()}"
    return prompt


def build_turn_prompt(new_messages: Sequence[Message], turns_left: int, activities: Sequence[str] = ()) -> str:
    lines = ["[群聊新消息]"]
    for m in new_messages:
        who = f"{m.speaker_name}（用户）" if m.from_user else m.speaker_name
        lines.append(f"{who}: {m.text}")
    lines.append("[/群聊新消息]")
    for activity in activities:
        lines.append(f"（{activity}，结果还没出来。不要替它猜测会查到什么，可以先聊别的角度，或者回复 {PASS_TOKEN} 等它。）")
    lines.append(
        f"现在轮到你发言（本轮你还可以发言 {turns_left} 次）。"
        f"如果没有要补充的，只回复 {PASS_TOKEN}。"
    )
    return "\n".join(lines)


_PASS_RE = re.compile(r"^\[?\s*pass\s*\]?[\s.。!！]*$", re.IGNORECASE)


def clean_reply(raw: str, name: str) -> str | None:
    """Normalise a model reply; ``None`` means the model passed."""
    text = raw.strip()
    # Models sometimes echo the transcript format and prefix their own name.
    text = re.sub(rf"^\s*{re.escape(name)}\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    if not text or _PASS_RE.match(text):
        return None
    return text
