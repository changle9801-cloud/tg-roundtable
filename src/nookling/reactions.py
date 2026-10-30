"""Emoji reactions: a member can react to a message instead of (or as well as) replying.

Models write [回应:🤣]; the speaker's own bot sets that reaction on the message it is answering.
Telegram only accepts its fixed set of reaction emoji, one per bot per message.
"""

from __future__ import annotations

import re

# Telegram's standard reaction emoji (Bot API ReactionTypeEmoji), without variation selectors.
ALLOWED = set(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 "
    "🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 "
    "😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)
# What models reach for, mapped to the closest one Telegram allows.
ALIASES = {
    "😂": "🤣", "😆": "😁", "😄": "😁", "😃": "😁", "😅": "😁", "😀": "😁", "😊": "🥰", "☺": "🥰",
    "🥲": "😢", "😿": "😢", "🥺": "😢", "💀": "😱", "😮": "😱", "😲": "😱", "🙄": "🤨", "😒": "😐",
    "😑": "😐", "♥": "❤", "💕": "❤", "💖": "❤", "🫠": "🥴", "😵": "🤯", "👀": "👀", "🤦": "🤷",
}
SUGGESTED = "👍 ❤ 😁 🤣 😭 🤔 😱 🤯 🔥 👏 🎉 💯 🙈 👀 🤡 😴 🥱 🫡 🐳 🙏 😎 🗿"

TAG_RE = re.compile(r"\[\s*(?:回应|反应|reaction)\s*[:：]\s*([^\]\n]{1,12}?)\s*\]", re.IGNORECASE)


def normalize(emoji: str) -> str | None:
    """The Telegram reaction for ``emoji``, or None if there is no close one."""
    emoji = emoji.strip().replace("️", "")
    emoji = ALIASES.get(emoji, emoji)
    return emoji if emoji in ALLOWED else None


def extract_reaction(text: str) -> tuple[str, str | None]:
    """Remove [回应:…] tags; returns the text and the first usable reaction (one per message)."""
    found: list[str] = []

    def take(m: re.Match) -> str:
        emoji = normalize(m.group(1))
        if emoji:
            found.append(emoji)
        return " "

    plain = TAG_RE.sub(take, text)
    plain = "\n".join(line.strip() for line in re.sub(r"[ \t]{2,}", " ", plain).splitlines()).strip()
    return plain, (found[0] if found else None)


def reaction_instructions() -> str:
    return (
        "表情回应：\n"
        f"- 你可以像真人一样给刚才那条消息点个表情回应，写法是 [回应:🤣]，可选：{SUGGESTED}。\n"
        "- 觉得好笑、赞同、无语，但又不值得专门回一句时，就只回复 [回应:🤣] 这样一个标记，不用说话；"
        "也可以说一句再加一个回应。\n"
        "- 大多数时候不用回应，一条消息最多一个。"
    )
