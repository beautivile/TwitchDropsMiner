"""
Import-time shim that makes the upstream miner run without Tkinter.

`twitch.py` does `from gui import GUIManager` at module scope, and the real
`gui.py` imports `tkinter`, `pystray` and `PIL.ImageTk` at the top. In a slim
container none of those exist, and even where they do we never want a display
connection.

So before anything imports `twitch`, we register a synthetic `gui` module in
`sys.modules` whose `GUIManager` is our headless `WebGUIManager`. The real
`gui.py` is then never loaded, and `twitch.py` binds our class instead.

`cache.py` gets the same treatment: it imports `PIL.ImageTk` and is only used by
the Tk inventory view, which we replace wholesale.

Import this module before importing `twitch`.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _mark_packaged() -> None:
    """
    Tell the upstream code it is running from a packaged build.

    `translate.py` rewrites `lang/English.json` on import whenever
    `constants.IS_PACKAGED` is false - a convenience for developers editing the
    template. In a container that write targets read-only application code, and
    it fails outright when the image runs under an arbitrary uid (as a Synology
    bind-mount deployment does, via `user: "<PUID>:<PGID>"`).

    A container image *is* a packaged build, so setting the marker upstream
    already checks for is both accurate and enough to skip that write. It has to
    happen before `constants` is imported, since the flag is evaluated there at
    module scope.
    """
    if not hasattr(sys, "_MEIPASS"):
        # the value is only used to locate bundled resources when frozen;
        # constants._resource_path() prefers WORKING_DIR, which is correct here
        sys._MEIPASS = str(ROOT)  # type: ignore[attr-defined]


def _fix_paths() -> None:
    """
    Point the upstream path constants at the right directories.

    `constants.py` derives `WORKING_DIR` from `sys.argv[0]`, which would be
    `webapp/server.py` here - making the miner look for `lang/` and write
    `settings.json` / `cookies.jar` inside `webapp/`. We pretend to be the
    repository's `main.py` so bundled resources resolve, then redirect the
    mutable files to `TDM_DATA_DIR` (a mounted volume in the container) so
    state survives image rebuilds.
    """
    sys.argv[0] = str(ROOT / "main.py")

    data_dir = Path(os.environ.get("TDM_DATA_DIR", ROOT / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    import constants

    constants.LOG_PATH = data_dir / "log.txt"
    constants.DUMP_PATH = data_dir / "dump.dat"
    constants.LOCK_PATH = data_dir / "lock.file"
    constants.CACHE_PATH = data_dir / "cache"
    constants.CACHE_DB = constants.CACHE_PATH / "mapping.json"
    constants.COOKIES_PATH = data_dir / "cookies.jar"
    constants.SETTINGS_PATH = data_dir / "settings.json"


def install() -> None:
    """Register the stub modules. Safe to call more than once."""
    _mark_packaged()
    _fix_paths()
    if isinstance(sys.modules.get("gui"), types.ModuleType) and getattr(
        sys.modules["gui"], "_TDM_WEB_STUB", False
    ):
        return

    # `cache` is imported by the real gui only; stub it so PIL.ImageTk is never
    # required. Skip if something already imported the genuine module.
    if "cache" not in sys.modules:
        cache_stub = types.ModuleType("cache")
        cache_stub._TDM_WEB_STUB = True  # type: ignore[attr-defined]

        class ImageCache:  # noqa: D401 - stand-in for the Tk image cache
            """No-op image cache; the browser fetches images directly."""

            def __init__(self, manager: object) -> None:
                self._manager = manager

            async def get(self, url: str, size: object = None) -> None:
                return None

            def save(self, *, force: bool = False) -> None:
                return

        cache_stub.ImageCache = ImageCache  # type: ignore[attr-defined]
        sys.modules["cache"] = cache_stub

    from gui_adapter import WebGUIManager

    gui_stub = types.ModuleType("gui")
    gui_stub._TDM_WEB_STUB = True  # type: ignore[attr-defined]
    gui_stub.GUIManager = WebGUIManager  # type: ignore[attr-defined]
    # `twitch.py` type-hints `LoginForm` under TYPE_CHECKING, but keep the name
    # available in case anything reaches for it at runtime.
    import gui_adapter

    gui_stub.LoginForm = gui_adapter.LoginForm  # type: ignore[attr-defined]
    sys.modules["gui"] = gui_stub
