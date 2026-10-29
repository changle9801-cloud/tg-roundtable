"""Small topic gate for optional public jokes and shared anecdotes."""

from __future__ import annotations

import re
from typing import Sequence


def chat_query(prompt: str) -> str:
    """Match the actual conversation, not the clock, style instructions or memory blocks."""
    current = re.search(r"\[群聊新消息\](.*?)\[/群聊新消息\]", prompt, re.S)
    text = current.group(1) if current else prompt
    text = re.sub(r"(?m)^.{1,40}?[:：]\s*", "", text)
    return text[-1800:]


def topic_score(query: str, text: str, names: Sequence[str] = ()) -> int:
    """Require a quoted phrase or concrete overlap; a person's name alone is not a topic."""
    def content(value):
        value = value.casefold()
        for name in sorted(names, key=len, reverse=True):
            if name:
                value = value.replace(name.casefold(), " ")
        return value

    query, text = content(query), content(text)
    quotes = re.findall(r"[「“‘\"]([^」”’\"\n]{2,60})[」”’\"]", text)
    if any(part.strip() and len(part.strip()) >= 2 and part.strip() in query for part in quotes):
        return 10

    def terms(value):
        result = set(re.findall(r"[a-z0-9_]{3,}", value))
        for run in re.findall(r"[\u4e00-\u9fff]{3,}", value):
            result.update(run[i:i + 3] for i in range(len(run) - 2))
        return result - {"不知道", "不一定", "有意思", "没什么", "为什么", "怎么说", "说什么", "的时候", "这件事"}

    overlap = terms(query) & terms(text)
    return len(overlap) if len(overlap) >= 2 else 0
