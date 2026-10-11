#!/usr/bin/env bash
#
# Build and push the miner image to a container registry.
#
#   ./webapp/scripts/publish.sh                 # build + push :latest and a dated tag
#   ./webapp/scripts/publish.sh v1.2            # also tag :v1.2
#   ./webapp/scripts/publish.sh --multi-arch    # build for amd64 + arm64 (needs buildx)
#   ./webapp/scripts/publish.sh --no-push       # build and tag only
#
# Override the destination with REPO=someuser/somerepo.
#
# The image carries no credentials: the login lives in the /data volume, and
# .dockerignore keeps cookies.jar / settings.json out of the build context.

set -euo pipefail

REPO="${REPO:-hypeview/twitchdropsminer}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

VERSION_TAG=""
MULTI_ARCH=0
DO_PUSH=1
for arg in "$@"; do
  case "$arg" in
    --multi-arch) MULTI_ARCH=1 ;;
    --no-push)    DO_PUSH=0 ;;
    -h|--help)    sed -n '2,14p' "$0"; exit 0 ;;
    -*)           echo "unknown option: $arg" >&2; exit 2 ;;
    *)            VERSION_TAG="$arg" ;;
  esac
done

info() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

DATE_TAG="$(date +%Y%m%d)"
# record what the image was built from, for traceability
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  GIT_SHA="${GIT_SHA}-dirty"
  warn "working tree has uncommitted changes (tagging as $GIT_SHA)"
fi

TAGS=("$REPO:latest" "$REPO:$DATE_TAG")
[ -n "$VERSION_TAG" ] && TAGS+=("$REPO:$VERSION_TAG")

info "repository : $REPO"
info "tags       : ${TAGS[*]}"
info "source     : $GIT_SHA"

# --------------------------------------------------------------- auth check

if [ "$DO_PUSH" -eq 1 ]; then
  # a private repo needs credentials; fail early rather than after a long build
  if ! docker system info 2>/dev/null | grep -qi "^ *Username:"; then
    warn "not logged in to a registry - run:  docker login"
    read -r -p "Log in now? [Y/n] " reply
    case "${reply:-Y}" in
      [Nn]*) die "aborted; log in and re-run" ;;
      *)     docker login || die "docker login failed" ;;
    esac
  fi
fi

# ------------------------------------------------------------------- build

BUILD_ARGS=(
  -f webapp/Dockerfile
  --label "org.opencontainers.image.title=Twitch Drops Miner"
  --label "org.opencontainers.image.description=Headless Twitch drops miner with a web UI"
  --label "org.opencontainers.image.source=https://github.com/DevilXD/TwitchDropsMiner"
  --label "org.opencontainers.image.revision=$GIT_SHA"
  --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
)
for t in "${TAGS[@]}"; do BUILD_ARGS+=(-t "$t"); done

if [ "$MULTI_ARCH" -eq 1 ]; then
  command -v docker >/dev/null || die "docker not found"
  docker buildx version >/dev/null 2>&1 || die "buildx is required for --multi-arch"

  # a multi-arch manifest can only be produced by pushing directly
  [ "$DO_PUSH" -eq 1 ] || die "--multi-arch requires pushing (drop --no-push)"

  if ! docker buildx inspect tdm-builder >/dev/null 2>&1; then
    info "creating buildx builder 'tdm-builder'"
    docker buildx create --name tdm-builder --driver docker-container --bootstrap >/dev/null
  fi
  info "building for linux/amd64 + linux/arm64 and pushing"
  docker buildx build --builder tdm-builder \
    --platform linux/amd64,linux/arm64 \
    "${BUILD_ARGS[@]}" --push .
  info "pushed multi-arch manifest."
  exit 0
fi

warn "building for this machine's architecture only ($(docker version -f '{{.Server.Arch}}' 2>/dev/null || uname -m))."
warn "An ARM NAS cannot run an amd64-only image - use --multi-arch for that."
info "building"
docker build "${BUILD_ARGS[@]}" .

# a quick smoke test beats discovering a broken image after deployment
info "smoke-testing the image"
CID="$(docker run -d -P "$REPO:latest")"
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
PORT="$(docker port "$CID" 8080/tcp | head -1 | sed 's/.*://')"
ok=0
for _ in $(seq 1 40); do
  if curl -fsS -m 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
docker rm -f "$CID" >/dev/null 2>&1 || true
trap - EXIT
[ "$ok" -eq 1 ] || die "the built image did not become healthy - not pushing"
info "smoke test passed."

if [ "$DO_PUSH" -eq 0 ]; then
  info "--no-push given; built and tagged locally only."
  exit 0
fi

# -------------------------------------------------------------------- push

for t in "${TAGS[@]}"; do
  info "pushing $t"
  docker push "$t"
done

echo
info "done. Deploy elsewhere with:"
cat <<EOF
    docker pull $REPO:latest
    docker run -d --name twitch-drops-miner --restart unless-stopped \\
      -p 8080:8080 -v twitchdropsminer_miner-data:/data $REPO:latest
EOF
