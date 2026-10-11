"""
Shared application state between the miner core and the web layer.

This module holds a single `AppState` instance that the headless GUI adapter
writes into, and that the HTTP/WebSocket layer reads from. Every mutation
bumps a revision counter and notifies subscribers, so the frontend only
re-renders when something actually changed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any


MAX_CONSOLE_LINES = 500


class AppState:
    """Thread-unsafe, asyncio-loop-affine snapshot of everything the UI shows."""

    def __init__(self) -> None:
        self.status: str = "Initializing..."
        self.tray_state: str = "idle"
        self.login: dict[str, Any] = {"status": "Logged out", "user_id": None}
        # device-code login prompt, surfaced to the browser
        self.auth_prompt: dict[str, Any] | None = None
        self.drop: dict[str, Any] | None = None
        self.countdown: dict[str, Any] = {"seconds": 0, "running": False}
        self.channels: dict[str, dict[str, Any]] = {}
        self.watching_iid: str | None = None
        self.selected_iid: str | None = None
        self.campaigns: dict[str, dict[str, Any]] = {}
        self.games: list[str] = []
        self.websockets: dict[int, dict[str, Any]] = {}
        self.console: deque[str] = deque(maxlen=MAX_CONSOLE_LINES)
        self.settings: dict[str, Any] = {}
        self.can_invalidate: bool = False
        self.has_saved_session: bool = False
        self.running: bool = False
        self.closing: bool = False

        self._revision: int = 0
        self._subscribers: set[asyncio.Queue[int]] = set()

    # ---------------------------------------------------------------- events

    @property
    def revision(self) -> int:
        return self._revision

    def subscribe(self) -> asyncio.Queue[int]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[int]) -> None:
        self._subscribers.discard(queue)

    def notify(self) -> None:
        """Mark the state dirty and wake every listener."""
        self._revision += 1
        for queue in self._subscribers:
            if queue.full():
                # a wake-up is already pending; the listener will read the
                # newest state anyway, so dropping this one loses nothing
                continue
            try:
                queue.put_nowait(self._revision)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------ mutations

    def print(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        for line in str(message).splitlines() or [""]:
            self.console.append(f"{stamp}  {line}")
        self.notify()

    # ----------------------------------------------------------- serializing

    def snapshot(self) -> dict[str, Any]:
        """Full state as JSON-safe primitives, for the initial WS payload."""
        channels = sorted(
            self.channels.values(),
            key=lambda c: (
                not c["watching"],
                {"online": 0, "pending": 1, "offline": 2}.get(c["status"], 3),
                -(c["viewers"] or 0),
                c["name"].lower(),
            ),
        )
        campaigns = sorted(
            self.campaigns.values(),
            key=lambda c: (not c["active"], c["game"].lower(), c["name"].lower()),
        )
        return {
            "revision": self._revision,
            "status": self.status,
            "tray_state": self.tray_state,
            "login": self.login,
            "auth_prompt": self.auth_prompt,
            "drop": self.drop,
            "countdown": self.countdown,
            "channels": channels,
            "watching_iid": self.watching_iid,
            "selected_iid": self.selected_iid,
            "campaigns": campaigns,
            "games": self.games,
            "websockets": list(self.websockets.values()),
            "console": list(self.console),
            "settings": self.settings,
            "can_invalidate": self.can_invalidate,
            "has_saved_session": self.has_saved_session,
            "running": self.running,
            "closing": self.closing,
        }


state = AppState()
