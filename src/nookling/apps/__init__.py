"""Chat apps on the house computer: how the family talks with the owner outside the house.

Each app keeps its own connections and hands the conversations a ``Sink`` (see
``conversation.Sink``) to speak through. Telegram is the first; Discord and others
plug in the same way.
"""

from __future__ import annotations

from typing import Protocol

from ..conversation import Sink


class App(Protocol):
    sink: Sink

    async def sync(self) -> dict[str, str]:
        """Settings changed: connect each member's account. Returns what is off, per member."""

    async def run(self) -> None:
        """Receive messages until cancelled; reconnect by itself when settings change."""

    @property
    def connected(self) -> bool:
        """Messages can go out right now."""

    def status(self) -> dict: ...

    async def shutdown(self) -> None: ...
