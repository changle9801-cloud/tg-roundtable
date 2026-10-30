"""What the members call the owner, from settings (提示词 → 主人的称呼).

The runtime sets it whenever settings load; prompts and labels read it when they are built.
"""

from __future__ import annotations

DEFAULT = "主人"
MAX_LENGTH = 12

_name = DEFAULT


def name() -> str:
    return _name


def set_name(value: str | None) -> None:
    global _name
    _name = clean(value)


def clean(value: str | None) -> str:
    return (str(value or "").strip()[:MAX_LENGTH]) or DEFAULT
