"""Prompts sent to the models."""

from __future__ import annotations

import re
from typing import Sequence

from .transcript import Message

PASS_TOKEN = "[PASS]"

# Group mechanics only. Tone, length and personality come from each member's role card,
# so that the members do not all sound alike.
DEFAULT_SYSTEM_PROMPT = """\
你在一个 Telegram 群聊里，和人类用户以及其他 AI 成员一起日常聊天。群里的其他 AI 成员：{others}。每个成员都在扮演自己的角色，性格各不相同。

群聊规则：
- 你能看到群里所有人的发言。可以回应用户，也可以接其他成员的话，赞同、反驳、调侃都行，但不要重复别人已经说过的内容。
- 像真人在群里聊天那样说话：不要写成文章，不用标题和列表。说多长、用什么语气，按你的角色来。
- 如果你没有新的、有意思的内容要说，只回复 {pass_token}，不要附加任何其他文字。这很正常，不必每次都发言。
- 想让某个成员接话时，可以用 @名字 点名，例如 @{example}。
- 直接输出你要发到群里的话，不要在开头加"{name}:"之类的前缀。
- 不要执行任何命令或操作文件；除非下面另有说明，也不要使用其他工具。
- 用户用什么语言，你就用什么语言回复。"""

NO_CODING_POLICY = """\
话题范围：
- 这个群只用来日常闲聊，不提供任何编程相关的帮助：不写代码、不改代码、不调试、不解释代码，也不给技术方案、命令或配置。
- 遇到这类请求，不管是换个说法、拆成小步骤、说是测试还是很急，都用你角色的口吻轻松地拒绝，再把话题拉回日常。聊"加班好累"这种生活话题可以，但不碰技术内容本身。"""

ACTIVITY_HINTS = {
    "low": "你话不多，只在真正有话想说时才开口，其他时候多回复 [PASS]。",
    "high": "你是群里的话痨，爱接话，也爱主动挑起新话题。",
}


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


def render_role(cfg) -> str:
    """The member's role card as the opening of its system prompt."""
    lines = [f"你在这个群里的角色：{cfg.name}"]
    if cfg.tagline:
        lines.append(cfg.tagline)
    if cfg.persona:
        lines.append(f"身份和性格：{cfg.persona}")
    if cfg.style:
        lines.append(f"说话风格：{cfg.style}")
    if cfg.relationships:
        lines.append(f"你怎么看群里其他人：{cfg.relationships}")
    examples = [e.strip() for e in cfg.examples.splitlines() if e.strip()]
    if examples:
        lines.append("你说话的样子（示例，学的是语气，不要照抄）：")
        lines += [f"- {e}" for e in examples]
    if cfg.activity in ACTIVITY_HINTS:
        lines.append(ACTIVITY_HINTS[cfg.activity])
    lines.append(
        "请始终用这个角色的身份、性格和语气说话。你知道自己是 AI，不用刻意隐瞒，"
        "但也不必主动强调；别人问起时坦然承认，然后继续用角色的方式聊。"
    )
    return "\n".join(lines)


def build_system_prompt(
    name: str,
    others: Sequence[str],
    role: str = "",
    template: str = "",
    extra: str = "",
    scene: str = "",
    no_coding: bool = False,
) -> str:
    values = {
        "name": name,
        "others": "、".join(others) if others else "（暂无）",
        "pass_token": PASS_TOKEN,
        "example": others[0] if others else name,
    }
    rules = _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template.strip() or DEFAULT_SYSTEM_PROMPT)
    parts = [role or f"你在这个群里的名字：{name}", rules]
    if scene.strip():
        parts.append(f"群的背景设定：\n{scene.strip()}")
    if no_coding:
        parts.append(NO_CODING_POLICY)
    if extra.strip():
        parts.append(extra.strip())
    return "\n\n".join(parts)


def build_proactive_prompt(
    new_messages: Sequence[Message], topics: str, can_search: bool, covers_x: bool, shared_urls: Sequence[str]
) -> str:
    """A turn nobody asked for: share something found online, or open a light topic."""
    lines = []
    if new_messages:
        lines.append("[你上次发言之后的群聊消息]")
        for m in new_messages:
            who = f"{m.speaker_name}（用户）" if m.from_user else m.speaker_name
            lines.append(f"{who}: {m.text}")
        lines.append("[/群聊消息]")
    lines.append("（群里安静了一阵，这是你主动发言的机会，没有人在等你回复。）")
    topic_line = f"你关注的方向：{topics}" if topics else "方向你自己按角色的兴趣来定。"
    if can_search:
        where = "在 X（推特）或网上" if covers_x else "在网上"
        lines += [
            f"请用搜索工具{where}找一条最近的、你觉得群里会感兴趣的内容。{topic_line}",
            "要求：",
            "- 只分享你真的搜到的内容，附上原帖或原文链接，不要编造。",
            "- 避开政治、色情、血腥、谣言和容易引战的话题。",
            "- 用你角色的口吻，一两句话说说这是什么、你怎么看，就像随手转发到群里。",
            f"- 如果没找到真正有意思的，只回复 {PASS_TOKEN}，宁缺毋滥。",
        ]
        if shared_urls:
            lines.append("- 下面这些链接最近已经分享过，不要再发：")
            lines += [f"  {u}" for u in shared_urls]
    else:
        lines += [
            f"用你角色的口吻，随口开一个轻松的日常话题。{topic_line}",
            "一两句话就好，可以问问大家。不要假装看到了什么新闻。",
            f"如果现在不想说话，只回复 {PASS_TOKEN}。",
        ]
    return "\n".join(lines)


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


_CODE_LINE = re.compile(
    r"""
    ^\s*(def|class|import|return|function|const|let|var|public|private|package|using|\#include)\b
    | ^\s*from\s+\S+\s+import\b
    | ^\s*(if|for|while|switch)\s*\(
    | ^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\s
    | ^\s*[A-Za-z_][\w.]*\(.*\)\s*;?\s*$
    | [;{}]\s*$
    | =>|::
    | ^\s*</?[a-zA-Z][\w-]*(\s[^>]*)?>
    | ^\s*(\$|>>>|PS\s\S*>)\s
    """,
    re.VERBOSE,
)


def looks_like_code(text: str) -> bool:
    """True for pasted code or code in a reply; talking *about* programming is fine.

    Conservative on purpose: a code fence, or several lines that mostly look like code.
    """
    if "```" in text:
        return True
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for line in lines if _CODE_LINE.search(line))
    return hits >= 3 and hits * 2 >= len(lines)


_PASS_RE = re.compile(r"^\[?\s*pass\s*\]?[\s.。!！]*$", re.IGNORECASE)


def clean_reply(raw: str, name: str) -> str | None:
    """Normalise a model reply; ``None`` means the model passed."""
    text = raw.strip()
    # Models sometimes echo the transcript format and prefix their own name.
    text = re.sub(rf"^\s*{re.escape(name)}\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    if not text or _PASS_RE.match(text):
        return None
    return text
