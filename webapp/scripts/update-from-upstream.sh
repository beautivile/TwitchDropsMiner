#!/usr/bin/env bash
#
# Sync this fork with DevilXD/TwitchDropsMiner, then rebuild the container.
#
#   ./webapp/scripts/update-from-upstream.sh              # merge + rebuild
#   ./webapp/scripts/update-from-upstream.sh --check      # report only
#   ./webapp/scripts/update-from-upstream.sh --no-build   # merge, skip rebuild
#   ./webapp/scripts/update-from-upstream.sh --push       # also push to origin
#
# Everything under webapp/ is ours, so upstream never touches it. Conflicts can
# only appear in the original files, and the script stops and tells you where.

set -euo pipefail

UPSTREAM_URL="https://github.com/DevilXD/TwitchDropsMiner.git"
UPSTREAM_REMOTE="upstream"
UPSTREAM_BRANCH="master"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
DO_BUILD=1
DO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --check)    CHECK_ONLY=1 ;;
    --no-build) DO_BUILD=0 ;;
    --push)     DO_PUSH=1 ;;
    -h|--help)  sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

info() { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[0;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository: $REPO_ROOT"

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  info "adding '$UPSTREAM_REMOTE' -> $UPSTREAM_URL"
  git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  warn "you have uncommitted changes:"
  git status --short
  [ "$CHECK_ONLY" -eq 1 ] || die "commit or stash them first, then re-run"
fi

# ------------------------------------------------------------------- fetch

info "fetching $UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
git fetch "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH" --quiet

BEHIND="$(git rev-list --count "HEAD..$UPSTREAM_REMOTE/$UPSTREAM_BRANCH")"

if [ "$BEHIND" -eq 0 ]; then
  info "already up to date with upstream."
  exit 0
fi

info "$BEHIND new upstream commit(s):"
git --no-pager log --oneline --no-decorate "HEAD..$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" | sed 's/^/    /'

echo
info "files upstream changed:"
git --no-pager diff --stat "HEAD...$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" | sed 's/^/    /'

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo
  info "--check given; nothing merged."
  exit 0
fi

# ------------------------------------------------------------------- merge

BACKUP="pre-upstream-merge-$(date +%Y%m%d-%H%M%S)"
git tag "$BACKUP"
info "tagged current state as '$BACKUP' (roll back with: git reset --hard $BACKUP)"

echo
info "merging $UPSTREAM_REMOTE/$UPSTREAM_BRANCH into $CURRENT_BRANCH"
if ! git merge "$UPSTREAM_REMOTE/$UPSTREAM_BRANCH" -m "Merge upstream DevilXD/TwitchDropsMiner into $CURRENT_BRANCH"; then
  echo
  warn "merge stopped on conflicts in:"
  git --no-pager diff --name-only --diff-filter=U | sed 's/^/    /'
  echo
  warn "resolve them, then:  git add <files> && git commit"
  warn "or abandon the merge: git merge --abort"
  exit 1
fi

info "merged cleanly."

# --------------------------------------------------------- sanity + rebuild

# the web layer imports the upstream modules directly, so a rename upstream
# would surface here rather than at runtime in the container
if command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python || command -v python3)"
  info "checking the web adapter still imports the miner"
  if ! (cd webapp && "$PY" -c "
import sys; sys.path.insert(0, '.')
import bootstrap; bootstrap.install()
import twitch
assert twitch.GUIManager.__name__ == 'WebGUIManager'
print('    adapter OK')
" 2>/dev/null); then
    warn "could not verify the adapter locally (missing deps is fine - the"
    warn "container has them). If the build fails, check webapp/gui_adapter.py"
    warn "against any GUI changes upstream made."
  fi
fi

if [ "$DO_BUILD" -eq 1 ]; then
  if command -v docker >/dev/null 2>&1; then
    info "rebuilding the container image"
    if docker compose -f webapp/docker-compose.yml build; then
      info "image rebuilt. Restart with:"
      echo "    docker compose -f webapp/docker-compose.yml up -d"
    else
      die "docker build failed - the merge is committed; fix the build and retry"
    fi
  else
    warn "docker not found; skipping rebuild."
  fi
fi

if [ "$DO_PUSH" -eq 1 ]; then
  info "pushing $CURRENT_BRANCH to origin"
  git push origin "$CURRENT_BRANCH"
else
  echo
  info "not pushed. When you are happy:  git push origin $CURRENT_BRANCH"
fi
