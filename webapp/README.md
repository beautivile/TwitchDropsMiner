# Twitch Drops Miner — web / container edition

Runs the upstream miner headlessly in a container and configures it from a
browser instead of the Windows desktop app. The UI uses the
[IBM Carbon Design System](https://carbondesignsystem.com/) Gray 100 dark theme.

Everything in this folder is **additive**. No file outside `webapp/` is
modified, so upstream changes from
[DevilXD/TwitchDropsMiner](https://github.com/DevilXD/TwitchDropsMiner) merge
without conflicts.

---

## Quick start

```bash
# from the repository root
docker compose -f webapp/docker-compose.yml up -d --build
```

Open <http://localhost:8080>, and the dashboard shows a Twitch activation code.
Enter it at the linked page, press **I entered the code**, and mining starts.
The token is stored in the `miner-data` volume, so you log in only once.

```bash
docker compose -f webapp/docker-compose.yml logs -f     # follow logs
docker compose -f webapp/docker-compose.yml down        # stop
```

### Without Docker

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on Linux
pip install -r webapp/requirements.txt
python webapp/server.py           # http://localhost:8080
```

---

## How it works

The upstream miner splits cleanly: `twitch.py` holds the mining logic and talks
to its Tkinter GUI through a small, stable surface (about twenty members —
`status.update`, `channels.*`, `inv.*`, `login`, `progress`, `print`, and a few
lifecycle calls).

`gui_adapter.WebGUIManager` reimplements exactly that surface without Tk,
writing into a shared state object that the web layer serves. The mining code
itself is imported **unmodified**.

```
browser  ──WebSocket──►  server.py  ──►  state.py  ◄──  gui_adapter.py
                                                             ▲
                                                             │ same interface
                                                             │ the Tk GUI had
                                          twitch.py (upstream, untouched)
```

| File | Role |
| --- | --- |
| `bootstrap.py` | Registers a stub `gui` module **before** `twitch` is imported, so `from gui import GUIManager` binds the headless adapter. Also redirects the data paths. |
| `session_store.py` | Keeps the Twitch login across restarts — atomic snapshots + a backup jar. |
| `gui_adapter.py` | The headless `GUIManager` and its sub-components. |
| `state.py` | Shared snapshot + change notifications. |
| `server.py` | FastAPI app, miner supervisor, REST + WebSocket. |
| `static/index.html` | Single-page Carbon UI (no build step). |
| `docker-compose.deploy.yml` | Standalone compose file for a host that pulls the published image. |
| `docker-compose.synology.yml` | Same, adapted for Synology DSM (bind mount + explicit uid). |
| `scripts/publish.*` | Build, smoke-test and push the image to a registry. |

### Why `bootstrap.py` exists

`twitch.py` does `from gui import GUIManager` at module scope, which binds the
name at import time — patching `gui.GUIManager` afterwards would be too late.
And importing the real `gui.py` pulls in `tkinter`, `pystray` and `PIL.ImageTk`,
which a slim container shouldn't need. Registering a stub module in
`sys.modules` first solves both: the real `gui.py` is never loaded.

> **Note:** `utils.py` imports `tkinter` at module scope and is imported by every
> core module, so the image installs `python3-tk`. Only the import is needed —
> the functions using it are never called, and no X server or `DISPLAY` is
> required.

---

## Staying logged in

You enter an activation code **once**. The OAuth token lives in the cookie jar
inside the `/data` volume and is reused on every later start — the console
shows `Restoring session from cookie` instead of asking for a new code.

Upstream writes that jar at two moments only: just after a login, and during a
graceful shutdown. That is fine for a desktop app, but in a container a
`docker kill`, an OOM kill or a host reboot skips shutdown entirely, and the
login would be lost. `session_store.py` closes the gap:

- snapshots the jar as soon as a session exists, again the moment a login
  completes, and every 5 minutes after that;
- writes atomically (temp file + `os.replace`), so a kill mid-write can never
  truncate the real jar;
- keeps the previous good jar as `cookies.jar.bak` and promotes it
  automatically if the primary one is missing or corrupt.

Verified: after `docker kill`, `cookies.jar` survives and the next start reuses
it. **Keep the volume** and the login persists; delete it and you re-activate.

To switch accounts, use **Sign out** on the dashboard (or
`POST /api/login/signout`) — it clears the stored jar and requests a new code.

> The jar holds a live Twitch token. It is written `0600` inside a Docker
> volume; treat a backup of it like a password.

### Always start it through compose

State lives in the named volume `twitchdropsminer_miner-data`, which the compose
file mounts at `/data`. Both the project name and the volume name are pinned in
`docker-compose.yml`, so they stay the same no matter where compose runs from.

A bare `docker run` with no `-v` gets an **empty** `/data` and will ask for a new
activation code. If you must run the image directly, mount the volume yourself:

```bash
docker run -d -p 8080:8080 -v twitchdropsminer_miner-data:/data twitch-drops-miner:web
```

Nothing else needs care: `docker compose up -d --build`, `down`, and image
rebuilds all keep the volume. Only `docker compose down -v` (or deleting the
volume) erases the login.

### If the login went missing

Older builds of this image carried a `VOLUME ["/data"]` line, which made every
`docker run` without `-v` create a throwaway **anonymous** volume - so the login
lived in a volume that nothing referenced again after a rebuild. The directive is
gone, but if you have an orphaned volume from before, find and recover it:

```bash
# list volumes that contain a cookie jar
for v in $(docker volume ls -q); do
  docker run --rm -v "$v":/probe alpine ls /probe/cookies.jar >/dev/null 2>&1     && echo "$v"
done

# copy the one you want into the named volume
docker volume create twitchdropsminer_miner-data
docker run --rm -v <volume-id>:/from -v twitchdropsminer_miner-data:/to   alpine cp -a /from/. /to/
```

A jar holding a real login contains an `auth-token` entry; one that only ever
reached the activation screen does not.

## Configuration

State lives in the `/data` volume (`TDM_DATA_DIR`): `settings.json`,
`cookies.jar`, `cache/`, `log.txt`. It survives image rebuilds.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TDM_PORT` | `8080` | Host port |
| `TDM_DATA_DIR` | `/data` | Persistent state |
| `TDM_VERBOSE` | `0` | Log detail, `0`–`4` |
| `TDM_LOG` | `1` | Write `log.txt` |
| `TDM_DEBUG_WS` / `TDM_DEBUG_GQL` | off | Protocol tracing |
| `TZ` | `UTC` | Container timezone |

Settings changed in the browser are written to `settings.json` immediately.
Changes affecting game selection trigger an inventory refetch; the proxy setting
applies after a restart.

### The UI

- **Dashboard** — login, current drop and campaign progress, minute countdown, channel table, miner controls
- **Inventory** — every campaign with per-drop progress, filterable; unlinked campaigns link out to Twitch
- **Settings** — priority order, exclusions, priority mode, quality, language, proxy
- **Console** — the miner's log output

### API

`GET /api/state`, `GET /api/health`, `GET /api/languages`, `WS /ws`, and
`POST` endpoints for `login/confirm`, `login/signout`, `miner/{start,stop,restart,reload}`,
`channels/{select,switch}` and `settings`. Interactive docs at `/docs`.

> The server has **no authentication**. Keep it on your LAN, or put it behind a
> reverse proxy with auth before exposing it.

---

## Publishing the image

Everything the miner needs is baked into the image — the upstream Python
modules, all 21 translations, and the web UI. The only thing mounted at runtime
is `/data`, which holds your state. Nothing is read from the source tree, so the
image runs anywhere.

The image contains **no credentials**: `.dockerignore` keeps `cookies.jar` and
`settings.json` out of the build context, and `/data` is empty in the image.

```bash
./webapp/scripts/publish.sh              # build, smoke-test, push :latest + a dated tag
./webapp/scripts/publish.sh v1.2         # also tag :v1.2
./webapp/scripts/publish.sh --no-push    # build and tag locally only
./webapp/scripts/publish.sh --multi-arch # amd64 + arm64 (needs buildx)
REPO=you/yourrepo ./webapp/scripts/publish.sh
```

Windows PowerShell:

```powershell
.\webapp\scripts\publish.ps1 [-Version v1.2] [-NoPush] [-MultiArch] [-Repo you/yourrepo]
```

It defaults to `hypeview/twitchdropsminer`, prompts for `docker login` if needed
(a private repo requires it), stamps OCI labels including the source commit, and
**runs the image and waits for a healthy response before pushing** — a broken
build never reaches the registry.

A plain build targets the architecture you build on. **ARM targets — an ARM
Synology, a Raspberry Pi, an Ampere VM, Apple Silicon — need `--multi-arch`.**
An amd64-only image on an ARM host fails to pull with "no matching manifest", or
starts and dies instantly with "exec format error".

`--multi-arch` pushes one manifest covering `linux/amd64` and `linux/arm64`, so
the same tag serves both and Docker picks the right image automatically. It
builds the non-native half under emulation, which is considerably slower than a
single-arch build.

Check what a target needs with `uname -m` on that host: `aarch64` → arm64,
`x86_64` → amd64. (Verified: the arm64 image builds and runs, reporting
`aarch64`, with all dependencies resolving.)

### Deploying it elsewhere

Copy `webapp/docker-compose.deploy.yml` to the target host — it needs no other
file from the repository:

```bash
docker login                                        # the repo is private
docker compose -f docker-compose.deploy.yml up -d
```

Then open `http://<host>:8080` and complete the device-code login once.

To update later, keeping the login:

```bash
docker compose -f docker-compose.deploy.yml pull
docker compose -f docker-compose.deploy.yml up -d
```

> The UI loads IBM Plex from Google Fonts. On a host with no internet access to
> `fonts.googleapis.com` the page still works, just with system fonts.

> There is no authentication on the web UI. On a public host, bind it to
> localhost (`127.0.0.1:8080:8080`, commented in the deploy file) and put it
> behind a reverse proxy that requires a login.

### Synology NAS (Container Manager / DSM 7)

Use `webapp/docker-compose.synology.yml` instead — DSM needs two things the
generic file does not provide: a **bind mount** (Container Manager's Project UI
cannot create Docker named volumes) and an explicit **`user:`** matching the NAS
account that owns the folder.

That file also pins `platform: linux/arm64`, since ARM Synology models report
`aarch64`. On an **Intel** Synology (`x86_64`), delete that line or set it to
`linux/amd64` — a pinned platform overrides Docker's automatic selection, so
leaving it would make the pull fail with "no matching manifest".

**On an ARM model, publish a multi-arch image first** (`uname -m` reports
`aarch64`) — otherwise there is nothing for the NAS to pull:

```bash
./webapp/scripts/publish.sh --multi-arch
```

**Container Manager cannot log in to a private registry by itself.** Authenticate
and pull the image first, then start the project:

1. **Create the data folder** — File Station, or SSH:
   `/volume1/docker/twitchdropsminer/data`

2. **Get the owning ids** over SSH: `id "$USER"` → e.g. `uid=1026`, `gid=100`.
   Put them in a `.env` file beside the compose file:

   ```
   PUID=1026
   PGID=100
   ```

3. **Sign in and pull** — either route works:

   - *Container Manager UI*: **Registry → Settings → Add → Docker Hub**, tick
     **Use credentials**, enter your Docker Hub username and an access token.
     Then find `hypeview/twitchdropsminer` and download the `latest` tag.
   - *SSH*: `sudo docker login -u <user>` then
     `sudo docker pull hypeview/twitchdropsminer:latest`

   Use a Docker Hub **access token** (Account Settings → Personal access tokens),
   not your account password.

4. **Start it** — Container Manager → **Project → Create**, pointed at the file;
   or over SSH:

   ```bash
   cd /volume1/docker/twitchdropsminer
   sudo docker compose -f docker-compose.synology.yml up -d
   ```

5. Open `http://<nas-ip>:8080` and complete the device-code login once.

To update: pull the new image (step 3), then re-run step 4. The login survives,
because it lives in the bind-mounted folder — which Hyper Backup can also include.

> Because the image may run under any NAS uid, `bootstrap.py` marks the app as a
> packaged build. Without that, `translate.py` tries to rewrite
> `lang/English.json` inside the read-only application directory on import and
> the container dies with `PermissionError` before serving anything.

> `PUID`/`PGID` must match the owner of the data folder. If they don't, the
> container exits at startup with
> `PermissionError: [Errno 13] Permission denied: '/data/log.txt'` — fix the ids,
> or `sudo chown -R <PUID>:<PGID> /volume1/docker/twitchdropsminer/data`.
> Note this `user:` override belongs with a **bind mount** only: a named volume
> is created root-owned, so overriding the uid there breaks writes instead.

---

## Staying current with upstream

```bash
./webapp/scripts/update-from-upstream.sh            # merge + rebuild
./webapp/scripts/update-from-upstream.sh --check    # just report what changed
./webapp/scripts/update-from-upstream.sh --no-build # merge only
./webapp/scripts/update-from-upstream.sh --push     # also push to origin
```

Windows PowerShell:

```powershell
.\webapp\scripts\update-from-upstream.ps1 [-Check] [-NoBuild] [-Push]
```

The script adds the `upstream` remote on first run, tags the current commit
(`pre-upstream-merge-<timestamp>`) so you can roll back with
`git reset --hard <tag>`, merges, verifies the adapter still imports the miner,
then rebuilds the image. It never pushes unless you pass `--push`.

Because upstream never touches `webapp/`, conflicts can only appear in the
original files, and the script names them.

**If upstream changes the GUI interface**, the adapter raises a clear error
naming the missing attribute:

```
AttributeError: WebGUIManager has no attribute 'foo'. Upstream likely added a
new GUI call - implement it in webapp/gui_adapter.py to restore compatibility.
```

Add the matching method to `gui_adapter.py` and rebuild.

---

## Differences from the desktop app

| | Desktop | Web |
| --- | --- | --- |
| Login | username/password or device code | device code only |
| Tray icon / notifications | yes | no (status shown in the header) |
| Drop images | cached locally | loaded by the browser from Twitch |
| Autostart | registry / `.desktop` | `restart: unless-stopped` |

Username/password login is intentionally omitted: it usually trips Twitch's
CAPTCHA from a datacenter IP, which cannot be solved headlessly. The device-code
flow avoids that entirely.
