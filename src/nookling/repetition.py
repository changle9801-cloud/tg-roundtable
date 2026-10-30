"""Spot a member's verbal tics: the same opener or phrase in several of its recent messages.

Models copy example sentences and their own recent wording ("喂喂……", "宇宙级常识"). Telling
them once in the rules is not enough; pointing at the actual repeats right before they speak is.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

RECENT = 8  # how many of the member's own messages to look at
MAX_HITS = 3

_TAG = re.compile(r"\[[^\]\n]{0,40}\]|https?://\S+")
_OPENER = re.compile(r"^\s*([^\s，。！？、…,.!?~～—\-:：;；（）()“”\"'「」]{1,4})[\s，。！？、…,.!?~～—]")
_CJK_RUN = re.compile(r"[一-鿿]+")

# Everyday function phrases that repeat naturally.
COMMON = {
    "是不是", "有没有", "怎么样", "为什么", "什么时候", "不知道", "我觉得", "一下子", "这么多", "那么多",
    "怎么了", "可以的", "没问题", "真的是", "就是说", "还是说", "其实是", "一个人", "今天的", "明天的",
}


def _clean(text: str) -> str:
    return _TAG.sub(" ", text)


def repeated_bits(texts: Sequence[str], others: Sequence[str] = ()) -> list[str]:
    """Openers and phrases that show up in several of ``texts`` (one member's own messages).

    Phrases the rest of the chat also uses (``others``) are the topic, not a tic, and are skipped.
    """
    texts = [_clean(t) for t in texts[-RECENT:]]
    others = [_clean(t) for t in others]
    hits: list[str] = []

    openers = Counter(m.group(1) for t in texts if (m := _OPENER.match(t)))
    hits += [o for o, n in openers.most_common() if n >= 2]

    # Phrases: CJK substrings found in several messages (3 characters need three messages).
    seen_in: Counter[str] = Counter()
    for t in texts:
        found = set()
        for run in _CJK_RUN.findall(t):
            for size in range(3, 9):
                found.update(run[i:i + size] for i in range(len(run) - size + 1))
        seen_in.update(found)
    phrases = [
        p for p, n in seen_in.items()
        if p not in COMMON and n >= (3 if len(p) == 3 else 2) and not any(p in o for o in others)
    ]
    phrases.sort(key=lambda p: (-len(p), -seen_in[p]))
    for p in phrases:
        if not any(p in h or h in p for h in hits):  # keep the longest form only
            hits.append(p)
    return hits[:MAX_HITS]


def reminder(bits: Sequence[str]) -> str:
    if not bits:
        return ""
    said = "".join(f"「{b}」" for b in bits)
    return f"（你最近几条常说{said}，这次换个说法，别再用了。）"
