# Twitch Drops Miner - web/container build.
#
# Build context is the REPOSITORY ROOT, not this folder, because the image
# needs the upstream miner modules that live there:
#
#   docker build -f webapp/Dockerfile -t twitch-drops-miner .

FROM python:3.12-slim AS base

# tk       - `utils.py` imports tkinter at module scope (import only; no X server needed)
# tini     - proper PID 1 so SIGTERM reaches the miner and it shuts down cleanly
# ca-certs - TLS verification for the Twitch API
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-tk \
        tini \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# dependencies first, so edits to the source don't invalidate this layer
COPY webapp/requirements.txt /app/webapp/requirements.txt
RUN pip install --no-cache-dir -r /app/webapp/requirements.txt

# upstream miner modules (unmodified) + translations
COPY *.py /app/
COPY lang /app/lang
COPY icons /app/icons

# the web layer
COPY webapp /app/webapp

# writable state (settings.json, cookies.jar, cache, logs) - mount a volume here
ENV TDM_DATA_DIR=/data \
    TDM_HOST=0.0.0.0 \
    TDM_PORT=8080

# The image must run under an arbitrary uid: Synology/NAS deployments bind-mount
# a host folder and pass `user: "<PUID>:<PGID>"` so /data stays writable by the
# NAS account. Two things have to tolerate that:
#   * /data - the state directory;
#   * /app/lang - `translate.py` rewrites English.json on import when running
#     unpackaged, which would otherwise fail with EACCES for any other uid.
# Giving both to the root group with group-write covers every uid, because
# every container process belongs to group 0 by default.
RUN mkdir -p /data     && useradd -r -u 1000 -d /data miner     && chown -R miner:0 /data /app     && chmod -R g+rwX /data /app/lang

COPY webapp/entrypoint.sh /usr/local/bin/entrypoint.sh
# Strip any CR characters before making it executable. If the script reaches the
# build with Windows line endings, its shebang becomes "#!/bin/sh" and Linux
# fails with "No such file or directory" - which presents as a container that
# restarts every few seconds with essentially no log output.
RUN sed -i 's/$//' /usr/local/bin/entrypoint.sh     && chmod +x /usr/local/bin/entrypoint.sh

# NOTE: intentionally staying root here. The entrypoint takes ownership of a
# bind-mounted /data (which the host owns, usually with a different uid) and
# then drops to PUID:PGID before exec'ing the app - so the container is not
# running as root in practice. Setting `user:` in compose is still supported:
# the entrypoint detects it, skips the chown it cannot perform, and prints an
# actionable message if the mount is unwritable.

# NOTE: deliberately no `VOLUME ["/data"]`.
# A VOLUME directive makes every `docker run` without an explicit -v create a
# fresh ANONYMOUS volume, so the login and settings would silently disappear on
# the next rebuild. Mount /data yourself instead (compose does this for you):
#   docker run -v twitchdropsminer_miner-data:/data ...
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "/app/webapp/server.py"]
