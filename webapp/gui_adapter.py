"""
Headless drop-in replacement for `gui.GUIManager`.

The upstream miner core (`twitch.py`, `channel.py`, `inventory.py`) talks to its
Tkinter GUI through a small, stable surface. This module reimplements exactly
that surface without Tk, pushing every update into `state.AppState` instead,
where the web layer picks it up.

Nothing in the original source tree is modified, so upstream changes merge
cleanly. If upstream ever calls a GUI member that is not implemented here,
`__getattr__` raises a loud, descriptive error naming the missing attribute
rather than failing somewhere deep inside the miner.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from translate import _
from constants import OUTPUT_FORMATTER
from exceptions import ExitRequest

from state import state

if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from inventory import DropsCampaign, TimedDrop
    from utils import Game


logger = logging.getLogger("TwitchDrops")


class _WebOutputHandler(logging.Handler):
    """Mirrors the miner's log records into the web console."""

    def __init__(self, manager: WebGUIManager) -> None:
        super().__init__()
        self._manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        self._manager.print(self.format(record))


# --------------------------------------------------------------------- pieces


class StatusBar:
    def update(self, text: str) -> None:
        state.status = text
        state.notify()

    def clear(self) -> None:
        self.update("")


class WebsocketStatus:
    """Mirror of gui.WebsocketStatus - websocket.py drives this."""

    def update(self, idx: int, status: str | None = None, topics: int | None = None) -> None:
        entry = state.websockets.setdefault(
            idx, {"idx": idx, "status": "Disconnected", "topics": 0}
        )
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        state.notify()

    def remove(self, idx: int) -> None:
        state.websockets.pop(idx, None)
        state.notify()


class LoginForm:
    """
    Web equivalent of the Tk login form.

    Only the device-code flow is surfaced in the browser: `ask_enter_code`
    publishes the code and activation URL to the UI and waits for the user to
    confirm. `ask_login` (username/password) is kept for API compatibility but
    is not used by the device-code client type.
    """

    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager
        self._confirm = asyncio.Event()

    # -- called from the web layer
    def confirm(self) -> None:
        self._confirm.set()

    def update(self, status: str, user_id: int | None) -> None:
        state.login = {"status": status, "user_id": user_id}
        state.notify()

    def clear(self, login: bool = False, password: bool = False, token: bool = False) -> None:
        # no persistent form fields in the web UI - nothing to clear
        return

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        await self._manager.coro_unless_closed(self._confirm.wait())

    async def ask_login(self):  # pragma: no cover - device-code flow is used instead
        raise RuntimeError(
            "Username/password login is not supported in the web version. "
            "The device-code flow is used instead."
        )

    async def ask_enter_code(self, page_url, user_code: str) -> None:
        self.update(_("gui", "login", "required"), None)
        state.auth_prompt = {"code": user_code, "url": str(page_url)}
        state.notify()
        self._manager.print(
            f"Login required - open {page_url} and enter code: {user_code}"
        )
        try:
            await self.wait_for_login_press()
        finally:
            state.auth_prompt = None
            state.notify()


class CampaignProgress:
    ALMOST_DONE_SECONDS = 3

    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager
        self._drop: TimedDrop | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._seconds: int = 0

    def _update_time(self, seconds: int) -> None:
        self._seconds = seconds
        state.countdown = {"seconds": seconds, "running": self._timer_task is not None}
        state.notify()

    async def _timer_loop(self) -> None:
        while self._seconds > 0:
            await asyncio.sleep(1)
            self._update_time(self._seconds - 1)

    def start_timer(self) -> None:
        if self._timer_task is None:
            if self._seconds == 0:
                self._update_time(60)
            self._timer_task = asyncio.create_task(self._timer_loop())
            state.countdown["running"] = True
            state.notify()

    def stop_timer(self) -> None:
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None
        state.countdown["running"] = False
        state.notify()

    def minute_almost_done(self) -> bool:
        return self._timer_task is None or self._seconds <= self.ALMOST_DONE_SECONDS

    def display(
        self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self._drop = drop
        self.stop_timer()
        if drop is None:
            state.drop = None
            self._update_time(0)
            return
        campaign = drop.campaign
        state.drop = {
            "rewards": drop.rewards_text(),
            "progress": drop.progress,
            "current_minutes": drop.current_minutes,
            "required_minutes": drop.required_minutes,
            "remaining_minutes": drop.remaining_minutes,
            "campaign_name": campaign.name,
            "campaign_game": campaign.game.name,
            "campaign_progress": campaign.progress,
            "claimed_drops": campaign.claimed_drops,
            "total_drops": campaign.total_drops,
        }
        if countdown:
            self._update_time(60)
            self.start_timer()
        elif subone:
            self._update_time(0)
        else:
            self._update_time(60)


class ConsoleOutput:
    def print(self, message: str) -> None:
        state.print(message)


class ChannelList:
    """Mirror of gui.ChannelList, backed by the shared state dict."""

    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager
        self._channel_map: dict[str, Channel] = {}

    # -- called from the web layer
    def select(self, iid: str | None) -> None:
        if iid is not None and iid not in self._channel_map:
            return
        state.selected_iid = iid
        state.notify()

    def clear_watching(self) -> None:
        for entry in state.channels.values():
            entry["watching"] = False
        state.watching_iid = None
        state.notify()

    def set_watching(self, channel: Channel) -> None:
        for iid, entry in state.channels.items():
            entry["watching"] = iid == channel.iid
        state.watching_iid = channel.iid
        state.notify()

    def get_selection(self) -> Channel | None:
        if not self._channel_map or state.selected_iid is None:
            return None
        return self._channel_map.get(state.selected_iid)

    def clear_selection(self) -> None:
        state.selected_iid = None
        state.notify()

    def clear(self) -> None:
        self._channel_map.clear()
        state.channels.clear()
        state.watching_iid = None
        state.selected_iid = None
        state.notify()

    def display(self, channel: Channel, *, add: bool = False) -> None:
        iid = channel.iid
        if not add and iid not in self._channel_map:
            return
        if channel.online:
            status = "online"
        elif channel.pending_online:
            status = "pending"
        else:
            status = "offline"
        entry = {
            "iid": iid,
            "name": channel.name,
            "url": str(channel.url),
            "status": status,
            "game": str(channel.game or ""),
            "drops_enabled": channel.drops_enabled,
            "viewers": channel.viewers,
            "acl_based": channel.acl_based,
            "watching": state.watching_iid == iid,
        }
        self._channel_map[iid] = channel
        state.channels[iid] = entry
        state.notify()

    def remove(self, channel: Channel) -> None:
        iid = channel.iid
        self._channel_map.pop(iid, None)
        state.channels.pop(iid, None)
        if state.watching_iid == iid:
            state.watching_iid = None
        if state.selected_iid == iid:
            state.selected_iid = None
        state.notify()


class TrayIcon:
    """No system tray in a container - these calls just record state."""

    def change_icon(self, icon_state: str) -> None:
        state.tray_state = icon_state
        state.notify()

    def update_title(self, drop: TimedDrop | None) -> None:
        return

    def notify(self, message: str, title: str | None = None, duration: float = 10) -> None:
        state.print(f"{title + ': ' if title else ''}{message}")

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def minimize(self) -> None:
        return

    def restore(self) -> None:
        return

    def quit(self) -> None:
        return


class InventoryOverview:
    """Mirror of gui.InventoryOverview, minus image loading (URLs go to the browser)."""

    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager

    def get_status(self, campaign: DropsCampaign) -> str:
        if campaign.active:
            return "active"
        elif campaign.upcoming:
            return "upcoming"
        return "expired"

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        state.campaigns[campaign.id] = self._serialize(campaign)
        state.notify()

    def _serialize(self, campaign: DropsCampaign) -> dict[str, Any]:
        return {
            "id": campaign.id,
            "name": campaign.name,
            "game": campaign.game.name,
            "image_url": str(campaign.image_url),
            "link_url": campaign.link_url,
            "linked": campaign.linked,
            "eligible": campaign.eligible,
            "status": self.get_status(campaign),
            "active": campaign.active,
            "upcoming": campaign.upcoming,
            "expired": campaign.expired,
            "finished": campaign.finished,
            "starts_at": campaign.starts_at.isoformat(),
            "ends_at": campaign.ends_at.isoformat(),
            "claimed_drops": campaign.claimed_drops,
            "total_drops": campaign.total_drops,
            "remaining_drops": campaign.remaining_drops,
            "progress": campaign.progress,
            "allowed_channels": [ch.name for ch in campaign.allowed_channels],
            "drops": [
                {
                    "id": drop.id,
                    "rewards": drop.rewards_text(),
                    "benefits": [
                        {"name": b.name, "image_url": str(b.image_url)} for b in drop.benefits
                    ],
                    "progress": drop.progress,
                    "current_minutes": drop.current_minutes,
                    "required_minutes": drop.required_minutes,
                    "is_claimed": drop.is_claimed,
                    "can_earn": drop.can_earn(),
                    "can_claim": drop.can_claim,
                    "preconditions_met": drop.preconditions_met,
                }
                for drop in campaign.drops
            ],
        }

    def update_drop(self, drop: TimedDrop) -> None:
        campaign = drop.campaign
        if campaign.id in state.campaigns:
            state.campaigns[campaign.id] = self._serialize(campaign)
            state.notify()

    def clear(self) -> None:
        state.campaigns.clear()
        state.notify()

    def refresh(self) -> None:
        state.notify()


class _NoOpButton:
    """
    Stands in for a ttk.Button.

    `twitch.py` toggles `gui.help._invalidate_button` between "disabled" and
    "normal" to reflect whether a login can be invalidated. The web UI derives
    that from the login state instead, so this only needs to absorb `.config`.
    """

    def __init__(self) -> None:
        self.state: str = "disabled"

    def config(self, **kwargs: Any) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]
            state.can_invalidate = self.state == "normal"
            state.notify()

    configure = config


class HelpTab:
    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager
        self._invalidate_button = _NoOpButton()


class SettingsPanel:
    def __init__(self, manager: WebGUIManager) -> None:
        self._manager = manager

    def set_games(self, games: set[Game]) -> None:
        state.games = sorted(game.name for game in games)
        state.notify()

    def clear_selection(self) -> None:
        return

    def update_size(self) -> None:
        return


# -------------------------------------------------------------------- manager


class WebGUIManager:
    """
    Drop-in replacement for `gui.GUIManager`.

    Implements the surface that `twitch.py`, `channel.py` and `inventory.py`
    rely on. Attribute access for anything not implemented raises immediately
    with a clear message, so upstream additions surface as an obvious error.
    """

    def __init__(self, twitch: Twitch) -> None:
        self._twitch: Twitch = twitch
        self._close_requested = asyncio.Event()
        self._running: bool = False

        self.status = StatusBar()
        self.websockets = WebsocketStatus()
        self.login = LoginForm(self)
        self.progress = CampaignProgress(self)
        self.output = ConsoleOutput()
        self.channels = ChannelList(self)
        self.tray = TrayIcon()
        self.inv = InventoryOverview(self)
        self.settings = SettingsPanel(self)
        self.help = HelpTab(self)

        self._handler = _WebOutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger.addHandler(self._handler)

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._running

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    def start(self) -> None:
        self._running = True
        state.running = True
        state.notify()

    def stop(self) -> None:
        self.progress.stop_timer()
        self._running = False
        state.running = False
        state.notify()

    def close(self, *args: Any) -> int:
        """Request shutdown of the miner (mirrors closing the Tk window)."""
        self._close_requested.set()
        state.closing = True
        state.notify()
        self._twitch.close()
        return 0

    def close_window(self) -> None:
        logger.removeHandler(self._handler)

    def prevent_close(self) -> None:
        self._close_requested.clear()
        state.closing = False
        state.notify()

    async def wait_until_closed(self) -> None:
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro):
        tasks = [
            asyncio.ensure_future(coro),
            asyncio.ensure_future(self._close_requested.wait()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    # ------------------------------------------------------------ passthrough

    def save(self, *, force: bool = False) -> None:
        # the Tk build persists its image cache here; nothing to do headlessly
        return

    def grab_attention(self, *, sound: bool = True) -> None:
        return

    def set_games(self, games: set[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)
        self.inv.update_drop(drop)

    def clear_drop(self) -> None:
        self.progress.display(None)

    def print(self, message: str) -> None:
        self.output.print(message)

    def unfocus(self, *args: Any) -> None:
        return

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"WebGUIManager has no attribute {name!r}. Upstream likely added a new GUI "
            f"call - implement it in webapp/gui_adapter.py to restore compatibility."
        )
