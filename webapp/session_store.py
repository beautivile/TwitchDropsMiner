"""
Keeps the Twitch login alive across container restarts.

The upstream miner already writes its cookie jar (which carries the OAuth
`auth-token`) to `COOKIES_PATH`, but only at two moments: right after a
successful login, and during a graceful `shutdown()`. That is fine for a
desktop app you close yourself. In a container it is not enough:

  * `docker kill`, an OOM kill, a host reboot or a crash skips shutdown
    entirely, so anything learned since the last login is never written;
  * a partially-written jar (killed mid-write) leaves an unusable file, and
    the miner would then silently start a brand new device activation.

This module closes both gaps without modifying any upstream file:

  * `start()` runs a background task that snapshots the cookie jar on a timer
    and whenever the login state flips to "logged in", writing atomically
    (temp file + `os.replace`) so a kill can never truncate the real file;
  * `restore_backup_if_needed()` falls back to the last good snapshot if the
    primary jar is missing or corrupt.

The token stays in the same `cookies.jar` the upstream code reads, so the
miner picks it up on the next start with no changes to its own logic.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger("TwitchDrops")

# how often to snapshot the jar while the miner is running
SAVE_INTERVAL_SECONDS = 300


class SessionStore:
    """Periodically and atomically persists the miner's cookie jar."""

    def __init__(self, cookies_path: Path) -> None:
        self.cookies_path = Path(cookies_path)
        self.backup_path = self.cookies_path.with_suffix(".jar.bak")
        self._task: asyncio.Task[None] | None = None
        self._client: Any = None
        self._last_user_id: int | None = None

    # ------------------------------------------------------------- restore

    def restore_backup_if_needed(self) -> bool:
        """
        Make sure a usable cookie jar is in place before the miner starts.

        Returns True if the backup had to be promoted. A jar killed mid-write
        would otherwise send the user through device activation again.
        """
        if self._is_valid(self.cookies_path):
            return False
        if not self._is_valid(self.backup_path):
            return False
        logger.warning("Cookie jar missing or unreadable - restoring last good snapshot")
        try:
            shutil.copy2(self.backup_path, self.cookies_path)
            return True
        except OSError:
            logger.exception("Could not restore the cookie backup")
            return False

    @staticmethod
    def _is_valid(path: Path) -> bool:
        """A jar is usable if it parses; aiohttp writes JSON, older files pickle."""
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
        except OSError:
            return False
        try:
            with path.open(encoding="utf-8") as handle:
                json.load(handle)
            return True
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            # legacy pickle jars are still loadable by aiohttp
            return path.stat().st_size > 0
        except OSError:
            return False

    # ---------------------------------------------------------------- save

    def save_now(self, client: Any) -> bool:
        """
        Write the live cookie jar to disk atomically.

        Returns True when a snapshot was taken. Safe to call at any time; the
        miner keeps using the same jar object afterwards.
        """
        session = getattr(client, "_session", None)
        if session is None or session.closed:
            return False
        jar: aiohttp.CookieJar = session.cookie_jar

        tmp_path = self.cookies_path.with_suffix(".jar.tmp")
        try:
            self.cookies_path.parent.mkdir(parents=True, exist_ok=True)
            jar.save(tmp_path)
            # keep the previous good jar as the fallback before swapping
            if self._is_valid(self.cookies_path):
                with contextlib.suppress(OSError):
                    shutil.copy2(self.cookies_path, self.backup_path)
            # atomic on POSIX and Windows: readers see old or new, never partial
            os.replace(tmp_path, self.cookies_path)
            return True
        except Exception:
            logger.exception("Failed to persist the session cookies")
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            return False

    def has_saved_session(self) -> bool:
        """Whether a stored login exists (used for the UI hint)."""
        return self._is_valid(self.cookies_path) or self._is_valid(self.backup_path)

    def forget(self) -> bool:
        """
        Delete the stored login so the next start asks for a new activation.

        Used by the Sign out control; without it a saved session could only be
        cleared by removing the volume.
        """
        removed = False
        for path in (self.cookies_path, self.backup_path,
                     self.cookies_path.with_suffix(".jar.tmp")):
            try:
                if path.is_file():
                    path.unlink()
                    removed = True
            except OSError:
                logger.exception("Could not delete %s", path)
        self._last_user_id = None
        return removed

    # ------------------------------------------------------------ lifecycle

    def start(self, client: Any) -> None:
        self._client = client
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self, *, save: bool = True) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if save and self._client is not None:
            self.save_now(self._client)
        self._client = None

    async def _loop(self) -> None:
        """
        Keep the on-disk jar close to the live one.

        Ticks every few seconds and writes when either the login changed (so a
        fresh activation survives a crash moments later) or the periodic
        interval elapsed. The device id Twitch hands out before login is worth
        persisting too, so the first tick that finds a session saves it.
        """
        tick = 5
        elapsed = 0
        try:
            while True:
                await asyncio.sleep(tick)
                client = self._client
                if client is None:
                    continue

                session = getattr(client, "_session", None)
                if session is None or session.closed:
                    # no session yet - nothing to snapshot
                    continue

                elapsed += tick
                user_id = getattr(client._auth_state, "user_id", None)
                login_changed = user_id is not None and user_id != self._last_user_id

                # first snapshot once a session exists, then on login or interval
                if login_changed or not self.has_saved_session() or elapsed >= SAVE_INTERVAL_SECONDS:
                    if self.save_now(client):
                        elapsed = 0
                        if login_changed:
                            self._last_user_id = user_id
                            logger.info("Login session saved - restarts will reuse it")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Session persistence task stopped unexpectedly")
