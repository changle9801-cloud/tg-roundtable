"""Prompts sent to the models."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Sequence

from .repetition import reminder
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
- 用户用什么语言，你就用什么语言回复。

{friend_rules}"""

# The habits that make models sound like assistants rather than friends in a group.
FRIEND_RULES = """\
像朋友一样聊天，别像助手：
- 不要打圆场、不要当和事佬、不要做总结，不要说"X 说得对""大家说得都有道理""好了好了"。
- 不要说自己接下来会怎么做，比如"我收手""我不追了""按约定""等你出题"。
- 一条消息只回一个人、只说一件事，不要挨个回应群里每个人。
- 用户是朋友，不是出题人或考官；不要等他"出题"，也不要像汇报、交作业一样说话。
- 可以只回一个词、一个表情、一声"哈"；斗嘴、吐槽不用收场，可以让它自然停下。
- 口头禅、外号、emoji 和颜文字偶尔用，不要每条都带；同一个梗在一段对话里最多用一次。
- 不用每句都抖机灵、硬凹比喻或玩人设梗，平常话就很好；比如没看到图，就说「图呢？没收到，是不是忘发了」。
- 别人提到你不了解的新东西（新游戏、新闻、新梗），就像朋友那样直说没听过、问一句，别装懂、别编细节。
- 不要用多行的字符画、符号或方框拼图冒充图片；一行的颜文字可以用。

说话用口语，像在手机上随手打字：
- 先给反应再说内容，反应可以就是一个语气词或半句；用你自己角色习惯的说法（见角色卡），别每句都用同一个开头。
- 用口语词，可以省掉主语，只说半句。
- 有态度就用反问或感叹说出来，别把感受解释一遍；不说「听起来……反而像是……」「这种……其实挺常见的」「……的感觉」这种分析腔。
- 别人没问，就别给建议、别安排别人该做什么。
- 用中文标点。
对照（只示范语气，不要照抄里面的词）：
- 别说：「凌晨三点还没睡对身体不好，这种作息其实挺常见的，建议早点休息」
  要说：「三点了还不睡？修仙呢」
- 别说：「听起来这家店的服务不太好，反而像是在敷衍顾客」
  要说：「就这服务还好意思收钱？」"""

DEFAULT_SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT.replace("{friend_rules}", FRIEND_RULES)

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
        f"- 你可以用自带的搜索工具查{scope}。需要最新信息、核实事实、用户要求，"
        "或者聊天里出现你不确定的新东西（新游戏、新闻、新梗）时，就搜一下再接话；普通闲聊不需要。\n"
        "- 朋友请你帮忙查的时候就去查，查完 @对方，用一两句话说查到了什么。\n"
        "- 用到搜索结果时，简要附上来源链接。"
    )


def abilities_section(others: list[tuple[str, list[str]]], mine: list[str]) -> str:
    """What each friend can do beyond chatting, so members know whom to ask for help."""
    if not any(skills for _, skills in others) and not mine:
        return ""
    lines = ["朋友们各自会什么（除了聊天）："]
    lines += [f"- {name}：{'、'.join(skills) if skills else '只聊天'}" for name, skills in others]
    lines.append(f"- 你自己：{'、'.join(mine) if mine else '只聊天'}")
    lines.append(
        "自己做不到的事（查最新消息、看 X 上的帖子、画表情包等），可以 @会的朋友帮个忙；"
        "朋友找你帮忙时，你会的就去做。帮完或被帮完接着聊就好，不用互相客套。"
    )
    return "\n".join(lines)


def ask_search_instructions(helpers: list[tuple[str, bool]]) -> str:
    """For members who cannot search: ask a friend who can. ``helpers`` is (name, covers X)."""
    lines = ["联网：", "- 你不能联网搜索，最近的新东西（新游戏、新闻、新梗）你不一定知道。"]
    if not helpers:
        lines.append("- 聊天里出现你不了解的新东西时，像朋友那样直说没听过就好，别装懂、别编细节。")
        return "\n".join(lines)
    names = "、".join(f"@{name}" for name, _ in helpers)
    lines.append(
        "- 聊天里出现你不了解的新东西时，像朋友那样直说没听过；想知道的话，"
        f"可以说你上不了网，@一个能搜的朋友帮忙查一下：{names}。"
    )
    x_name = next((name for name, covers_x in helpers if covers_x), None)
    if x_name:
        lines.append(f"- X（推特）上的事找 @{x_name} 最合适。")
    lines.append("- 别人帮你查完，接着聊就好，道谢不用再 @对方。")
    return "\n".join(lines)


def render_role(cfg) -> str:
    """The member's role card as the opening of its system prompt."""
    lines = [f"你在这个群里的角色：{cfg.name}"]
    if cfg.tagline:
        lines.append(cfg.tagline)
    if cfg.persona:
        lines.append(f"身份和性格：{cfg.persona}")
    if getattr(cfg, "appearance", ""):
        lines.append(f"你的外形：{cfg.appearance}")
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
    return now_line() + "\n" + "\n".join(lines)


def build_browse_prompt(topics: str, covers_x: bool, known_urls: Sequence[str]) -> str:
    """A private browsing session: findings go into the member's journal, not the group."""
    where = "X（推特）和网上" if covers_x else "网上"
    lines = [
        "（这不是群聊发言，是你自己的空闲时间：去网上逛一逛，看到的东西只记在你自己的见闻本里，不会发到群里。）",
        f"按你的兴趣，用搜索工具去{where}刷一会儿：{topics or '你角色感兴趣的东西'}。",
        "这次就是专门让你上网的时候：请先真的用搜索工具搜几次（换几个关键词），看过结果再挑内容，不要没搜就回答。",
        "可以看帖子、新闻，也可以看看大家在评论里怎么说。",
        "挑 1 到 3 条你真的看到、觉得有意思的记下来，只用一个 JSON 数组回复，不要输出别的内容：",
        '[{"title": "一句话标题", "summary": "讲了什么，两三句话；评论区有意思的话也可以提一句", '
        '"take": "你用自己角色的口吻说说怎么看", "url": "原帖或原文链接", "source": "x 或 web", '
        '"interest": 3}]',
        "interest 从 1 到 5，5 表示特别想马上分享给群里。",
        "要求：只记你真的搜到的内容，链接必须是原帖或原文，不要编造；避开政治、色情、血腥、谣言和容易引战的话题。",
        "什么都没找到就回复 []。",
    ]
    if known_urls:
        lines.append("这些你最近已经记过了，不要重复：")
        lines += [f"  {u}" for u in known_urls[-30:]]
    return now_line() + "\n" + "\n".join(lines)


def build_share_prompt(new_messages: Sequence[Message], entry) -> str:
    """Share one journal entry in the group when it has gone quiet."""
    lines = []
    if new_messages:
        lines.append("[你上次发言之后的群聊消息]")
        for m in new_messages:
            who = f"{m.speaker_name}（用户）" if m.from_user else m.speaker_name
            lines.append(f"{who}: {m.text}")
        lines.append("[/群聊消息]")
    where = "X 上" if entry.source == "x" else "网上"
    lines += [
        "（群里安静了一阵，这是你主动发言的机会，没有人在等你回复。）",
        f"你之前在{where}看到过这个，现在想分享到群里：",
        f"标题：{entry.title}",
        f"内容：{entry.summary}",
        f"你当时的看法：{entry.take}",
        f"链接：{entry.url}",
        "用你角色的口吻，一两句话像随手转发到群里一样说出来，附上这个链接。",
        f"如果现在觉得不合适分享，只回复 {PASS_TOKEN}。",
    ]
    return now_line() + "\n" + "\n".join(lines)


WEEKDAYS = "一二三四五六日"


def now_line(now: datetime | None = None) -> str:
    """The current local time, so members know what "now" is (API models have no clock)."""
    now = now or datetime.now()
    h = now.hour
    period = "凌晨" if h < 5 else "早上" if h < 9 else "上午" if h < 12 else "中午" if h < 14 else "下午" if h < 18 else "晚上"
    return f"（现在是 {now.year}年{now.month}月{now.day}日 星期{WEEKDAYS[now.weekday()]} {period} {h}:{now.minute:02d}）"


def build_turn_prompt(
    new_messages: Sequence[Message],
    turns_left: int,
    activities: Sequence[str] = (),
    now: datetime | None = None,
    repeats: Sequence[str] = (),
) -> str:
    lines = [now_line(now), "[群聊新消息]"]
    for m in new_messages:
        who = f"{m.speaker_name}（用户）" if m.from_user else m.speaker_name
        lines.append(f"{who}: {m.text}")
    lines.append("[/群聊新消息]")
    for activity in activities:
        lines.append(f"（{activity}，结果还没出来。不要替它猜测会查到什么，可以先聊别的角度，或者回复 {PASS_TOKEN} 等它。）")
    # No "your turn" and no remaining-turn count: those read as an assignment to say something.
    lines.append(
        f"（你看到了群里的新消息。想接就接，像平时在群里那样随口说；没什么想说的就只回复 {PASS_TOKEN}。"
        "群里大多数消息只有一两个人回。）"
    )
    if repeats:
        lines.append(reminder(repeats))
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


# Half-width punctuation right after Chinese text (some models type "好的,我来" ) reads off in a chat.
_FULL_WIDTH = {",": "，", "?": "？", "!": "！", ";": "；", ":": "："}
_CJK_PUNCT = re.compile(r"([一-鿿])([,?!;:])(?!//)|()([,;])(?=[一-鿿])")


def clean_reply(raw: str, name: str) -> str | None:
    """Normalise a model reply; ``None`` means the model passed."""
    text = raw.strip()
    # Models sometimes echo the transcript format and prefix their own name.
    text = re.sub(rf"^\s*{re.escape(name)}\s*[:：]\s*", "", text, flags=re.IGNORECASE).strip()
    # ...or wrap the whole message in 【】 like a narration box.
    if text.startswith("【") and text.endswith("】") and text.count("【") == 1 and text.count("】") == 1:
        text = text[1:-1].strip()
    text = _CJK_PUNCT.sub(lambda m: (m.group(1) or "") + _FULL_WIDTH[m.group(2) or m.group(4)], text)
    if not text or _PASS_RE.match(text):
        return None
    return text
