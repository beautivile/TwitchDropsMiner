"""
Web server + miner supervisor.

Runs the upstream Twitch miner headlessly inside an asyncio task and exposes:
  * `GET  /`            - the Carbon-themed single page UI
  * `GET  /api/state`   - full state snapshot (JSON)
  * `WS   /ws`          - pushes a new snapshot whenever anything changes
  * `POST /api/*`       - control + settings endpoints

The miner core is imported unmodified from the repository root; only the GUI is
swapped out (see `gui_adapter.WebGUIManager`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# the upstream modules live one directory up
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import truststore  # noqa: E402

truststore.inject_into_ssl()

# must run before anything imports `twitch`: swaps the Tk GUI for the headless one
import bootstrap  # noqa: E402

bootstrap.install()

from translate import _  # noqa: E402
from constants import (  # noqa: E402
    FILE_FORMATTER,
    LOG_PATH,
    LOGGING_LEVELS,
    PriorityMode,
    State,
)
from exceptions import CaptchaRequired, ExitRequest  # noqa: E402
from settings import Settings  # noqa: E402
from version import __version__  # noqa: E402

from state import state  # noqa: E402
from session_store import SessionStore  # noqa: E402
import gui_adapter  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger("TwitchDrops")


# --------------------------------------------------------------------- config


def check_data_dir() -> None:
    """
    Fail loudly and usefully if the data directory is not writable.

    This is the most common deployment mistake: a bind-mounted host folder
    (Synology and friends) is owned by one uid while the container runs as
    another, via `user: "<PUID>:<PGID>"`. Without this check the first write
    raises deep inside logging, the process exits, and `restart: unless-stopped`
    turns it into a restart loop every few seconds, with only a traceback to
    explain it.
    """
    from constants import COOKIES_PATH

    data_dir = COOKIES_PATH.parent
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            chr(10).join([
                "",
                f"Cannot create the data directory {data_dir}: {exc}",
                "Create it on the host and make it writable by this container.",
                "",
            ])
        ) from exc

    probe = data_dir / ".write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        try:
            st = data_dir.stat()
            owner = f"{st.st_uid}:{st.st_gid}"
        except OSError:
            owner = "unknown"
        running_as = f"{os.getuid()}:{os.getgid()}"
        raise SystemExit(
            chr(10).join([
                "",
                f"{data_dir} is not writable by this container: {exc}",
                "",
                f"  running as : {running_as}",
                f"  directory  : owned by {owner}",
                "",
                "The container user must match the folder's owner. Either set",
                f"PUID/PGID to {owner} in your .env, or fix the folder on the host:",
                f"    chown -R {running_as} <host folder>",
                "",
            ])
        ) from exc


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class WebArgs:
    """
    Stands in for `main.ParsedArgs`.

    `Settings.__getattr__` checks the args object before the settings file, so
    this supplies exactly the attributes the miner reads from the CLI.
    """

    def __init__(self) -> None:
        self._verbose: int = int(os.environ.get("TDM_VERBOSE", "0"))
        self.log: bool = _env_flag("TDM_LOG", True)
        self.tray: bool = False
        self.dump: bool = False
        self._debug_ws: bool = _env_flag("TDM_DEBUG_WS")
        self._debug_gql: bool = _env_flag("TDM_DEBUG_GQL")

    @property
    def logging_level(self) -> int:
        return LOGGING_LEVELS[min(self._verbose, 4)]

    @property
    def debug_ws(self) -> int:
        if self._debug_ws:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET

    @property
    def debug_gql(self) -> int:
        if self._debug_gql:
            return logging.DEBUG
        elif self._verbose >= 4:
            return logging.INFO
        return logging.NOTSET


# ---------------------------------------------------------------- supervisor


class MinerSupervisor:
    """Owns the miner instance and its lifecycle."""

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.client: Any = None
        self.task: asyncio.Task[None] | None = None
        self._stopping: bool = False
        # bootstrap has already pointed COOKIES_PATH at the data volume
        from constants import COOKIES_PATH

        self.sessions = SessionStore(COOKIES_PATH)

    # -- helpers

    def _build_client(self):
        # `bootstrap.install()` has already put the headless GUIManager in place
        # of the Tk one, so this import binds our adapter.
        from twitch import Twitch

        return Twitch(self.settings)

    def _configure_logging(self) -> None:
        assert self.settings is not None
        if self.settings.logging_level > logging.DEBUG:
            logging.getLogger().addHandler(logging.NullHandler())
        logger.setLevel(self.settings.logging_level)
        if self.settings.log:
            # A read-only /data must not take the whole app down - losing the
            # log file is survivable, and check_data_dir() has already explained
            # the problem far more usefully than a traceback would.
            try:
                handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
                handler.setFormatter(FILE_FORMATTER)
                logger.addHandler(handler)
            except OSError as exc:
                state.print(f"Could not open the log file ({exc}); continuing without it.")
        logging.getLogger("TwitchDrops.gql").setLevel(self.settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(self.settings.debug_ws)

    # -- lifecycle

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self._stopping = False
        self.settings = Settings(WebArgs())
        with suppress(ValueError):
            _.set_language(self.settings.language)
        self._configure_logging()
        # repair a jar truncated by a crash before the miner reads it
        self.sessions.restore_backup_if_needed()
        state.has_saved_session = self.sessions.has_saved_session()
        self.client = self._build_client()
        self.task = asyncio.create_task(self._run())
        self.sessions.start(self.client)

    async def _run(self) -> None:
        assert self.client is not None
        try:
            await self.client.run()
        except CaptchaRequired:
            self.client.prevent_close()
            state.print(_("error", "captcha"))
        except ExitRequest:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self.client.prevent_close()
            state.print("Fatal error encountered:\n" + traceback.format_exc())
        finally:
            state.print(_("gui", "status", "exiting"))
            # snapshot while the aiohttp session still exists
            with suppress(Exception):
                self.sessions.save_now(self.client)
            with suppress(Exception):
                await self.client.shutdown()
            if not self._stopping:
                state.status = "Miner stopped"
                state.tray_state = "error"
            state.running = False
            state.notify()

    async def stop(self) -> None:
        self._stopping = True
        if self.client is not None:
            with suppress(Exception):
                await self.sessions.stop(save=True)
            with suppress(Exception):
                self.client.gui.close()
        if self.task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self.task), timeout=20)
            if not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.task
        if self.client is not None:
            with suppress(Exception):
                self.client.save(force=True)
            with suppress(Exception):
                self.client.gui.stop()
                self.client.gui.close_window()
        self.task = None
        self.client = None
        state.running = False
        state.notify()

    async def restart(self) -> None:
        await self.stop()
        state.console.clear()
        await self.start()


supervisor = MinerSupervisor()


# ---------------------------------------------------------------------- app


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_data_dir()
    await supervisor.start()
    try:
        yield
    finally:
        await supervisor.stop()


app = FastAPI(title="Twitch Drops Miner", version=__version__, lifespan=lifespan)


def _settings_payload() -> dict[str, Any]:
    s = supervisor.settings
    if s is None:
        return {}
    return {
        "priority": list(s.priority),
        "exclude": sorted(s.exclude),
        "priority_mode": s.priority_mode.name,
        "priority_modes": [m.name for m in PriorityMode],
        "language": s.language,
        "proxy": str(s.proxy),
        "connection_quality": s.connection_quality,
        "enable_badges_emotes": s.enable_badges_emotes,
        "available_drops_check": s.available_drops_check,
        "tray_notifications": s.tray_notifications,
    }


def _snapshot() -> dict[str, Any]:
    snap = state.snapshot()
    snap["settings"] = _settings_payload()
    snap["version"] = __version__
    return snap


@app.get("/api/state")
async def get_state() -> JSONResponse:
    return JSONResponse(_snapshot())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = state.subscribe()
    try:
        await ws.send_json(_snapshot())
        last_sent = state.revision
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                # periodic keepalive; also picks up anything missed
                pass
            if state.revision != last_sent:
                last_sent = state.revision
                await ws.send_json(_snapshot())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception:
        logger.debug("websocket closed", exc_info=True)
    finally:
        state.unsubscribe(queue)


# ------------------------------------------------------------------ controls


def _gui() -> Any:
    if supervisor.client is None:
        return None
    return supervisor.client.gui


@app.post("/api/login/confirm")
async def login_confirm() -> dict[str, Any]:
    gui = _gui()
    if gui is None:
        return {"ok": False, "error": "miner is not running"}
    gui.login.confirm()
    return {"ok": True}


@app.post("/api/login/signout")
async def login_signout() -> dict[str, Any]:
    """
    Forget the stored Twitch login.

    Stops the miner, deletes the cookie jar and its backup, then starts again -
    which triggers a fresh device activation.
    """
    await supervisor.stop()
    removed = supervisor.sessions.forget()
    state.console.clear()
    state.print("Signed out - a new activation code will be requested.")
    await supervisor.start()
    return {"ok": True, "removed": removed}


@app.post("/api/miner/start")
async def miner_start() -> dict[str, Any]:
    await supervisor.start()
    return {"ok": True}


@app.post("/api/miner/stop")
async def miner_stop() -> dict[str, Any]:
    await supervisor.stop()
    state.status = "Stopped"
    state.notify()
    return {"ok": True}


@app.post("/api/miner/restart")
async def miner_restart() -> dict[str, Any]:
    await supervisor.restart()
    return {"ok": True}


@app.post("/api/miner/reload")
async def miner_reload() -> dict[str, Any]:
    """Re-fetch the inventory without restarting the whole client."""
    client = supervisor.client
    if client is None:
        return {"ok": False, "error": "miner is not running"}
    client.change_state(State.INVENTORY_FETCH)
    return {"ok": True}


class ChannelSwitch(BaseModel):
    iid: str | None = None


@app.post("/api/channels/select")
async def channels_select(body: ChannelSwitch) -> dict[str, Any]:
    gui = _gui()
    if gui is None:
        return {"ok": False, "error": "miner is not running"}
    gui.channels.select(body.iid)
    return {"ok": True}


@app.post("/api/channels/switch")
async def channels_switch(body: ChannelSwitch) -> dict[str, Any]:
    """Select a channel and ask the miner to switch to it."""
    client = supervisor.client
    if client is None:
        return {"ok": False, "error": "miner is not running"}
    client.gui.channels.select(body.iid)
    client.change_state(State.CHANNEL_SWITCH)
    return {"ok": True}


# ------------------------------------------------------------------ settings


class SettingsPatch(BaseModel):
    priority: list[str] | None = None
    exclude: list[str] | None = None
    priority_mode: str | None = None
    language: str | None = None
    proxy: str | None = None
    connection_quality: int | None = None
    enable_badges_emotes: bool | None = None
    available_drops_check: bool | None = None
    tray_notifications: bool | None = None


@app.post("/api/settings")
async def update_settings(body: SettingsPatch) -> dict[str, Any]:
    s = supervisor.settings
    if s is None:
        return {"ok": False, "error": "settings are not loaded"}
    from yarl import URL

    needs_reload = False
    if body.priority is not None:
        s.priority = list(body.priority)
        needs_reload = True
    if body.exclude is not None:
        s.exclude = set(body.exclude)
        needs_reload = True
    if body.priority_mode is not None:
        try:
            s.priority_mode = PriorityMode[body.priority_mode]
            needs_reload = True
        except KeyError:
            return {"ok": False, "error": f"unknown priority mode {body.priority_mode!r}"}
    if body.language is not None:
        try:
            _.set_language(body.language)
            s.language = body.language
        except ValueError:
            return {"ok": False, "error": f"unknown language {body.language!r}"}
    if body.proxy is not None:
        s.proxy = URL(body.proxy)
    if body.connection_quality is not None:
        s.connection_quality = int(body.connection_quality)
    if body.enable_badges_emotes is not None:
        s.enable_badges_emotes = body.enable_badges_emotes
        needs_reload = True
    if body.available_drops_check is not None:
        s.available_drops_check = body.available_drops_check
    if body.tray_notifications is not None:
        s.tray_notifications = body.tray_notifications
    s.save(force=True)
    state.notify()

    if needs_reload and supervisor.client is not None:
        supervisor.client.change_state(State.INVENTORY_FETCH)
    return {"ok": True, "settings": _settings_payload()}


@app.get("/api/languages")
async def languages() -> dict[str, Any]:
    return {"languages": sorted(_.languages)}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "running": state.running,
        "logged_in": state.login.get("user_id") is not None,
    }


# --------------------------------------------------------------------- pages

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def main() -> None:
    import uvicorn

    host = os.environ.get("TDM_HOST", "0.0.0.0")
    port = int(os.environ.get("TDM_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
