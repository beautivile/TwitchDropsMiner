#!/bin/sh
# Container entrypoint.
#
# Its whole job is to make sure /data is writable before handing off to the app,
# and to say something useful on stdout if it cannot be. A bind-mounted host
# folder (Synology and similar) is owned by whatever uid the host chose, which
# rarely matches the uid the container runs as - and the resulting failure is a
# silent restart loop that is genuinely hard to diagnose from a NAS UI.
#
# Three paths:
#   * root, no PUID/PGID set (the default) - stay root. Root can always write a
#     bind-mounted host folder, whatever uid the host gave it, so this is the
#     configuration that "just works" on a NAS;
#   * root with PUID/PGID set - chown /data to them, then drop privileges;
#   * started as a fixed uid (`user:` in compose) - we cannot chown, so verify
#     writability and explain clearly if it is wrong.

set -e

DATA_DIR="${TDM_DATA_DIR:-/data}"
# PUID/PGID are optional. Unset means "stay root", which is the default and the
# most reliable option on a NAS: root can always write a bind-mounted folder, no
# matter which uid the host gave it. Set them only if you specifically want the
# app to run unprivileged.
PUID="${PUID:-}"
PGID="${PGID:-}"

log() { echo "[entrypoint] $*"; }

mkdir -p "$DATA_DIR" 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    if [ -n "$PUID" ] && [ -n "$PGID" ]; then
        # caller asked for an unprivileged uid: hand the data over first
        log "preparing $DATA_DIR for ${PUID}:${PGID}"
        if ! chown -R "${PUID}:${PGID}" "$DATA_DIR" 2>/dev/null; then
            log "WARNING: could not chown $DATA_DIR (read-only mount?); continuing"
        fi
        if command -v setpriv >/dev/null 2>&1; then
            log "dropping privileges to ${PUID}:${PGID}"
            # NOTE: no --init-groups here. It resolves supplementary groups via
            # /etc/passwd, which fails for an arbitrary NAS uid that has no
            # account in the image ("uid 1026 not found"). --clear-groups works
            # for any numeric id.
            exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$@"
        fi
        log "WARNING: setpriv unavailable; continuing as root"
        exec "$@"
    fi

    # default: stay root. Nothing to verify - root can write the mount.
    log "running as root; $DATA_DIR is writable"
    exec "$@"
fi

# running as a fixed, unprivileged uid - verify rather than fix
if ! ( : > "$DATA_DIR/.write-test" ) 2>/dev/null; then
    OWNER="$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo unknown)"
    cat >&2 <<EOF

============================================================
 $DATA_DIR is NOT writable, so the miner cannot start.

   running as : $(id -u):$(id -g)
   folder      : owned by $OWNER

 The container user must match the folder's owner. Either:

   1. Fix the folder on the NAS:
        sudo chown -R $(id -u):$(id -g) <host folder>

   2. Or set PUID/PGID to $OWNER in your .env

   3. Or remove the "user:" line from the compose file - the
      container then starts as root and fixes this itself.
============================================================

EOF
    exit 1
fi
rm -f "$DATA_DIR/.write-test" 2>/dev/null || true

exec "$@"
