<#
.SYNOPSIS
    Sync this fork with DevilXD/TwitchDropsMiner, then rebuild the container.

.DESCRIPTION
    Everything under webapp/ is ours, so upstream never touches it. Conflicts
    can only appear in the original files, and the script stops and says where.

.EXAMPLE
    .\webapp\scripts\update-from-upstream.ps1
    .\webapp\scripts\update-from-upstream.ps1 -Check
    .\webapp\scripts\update-from-upstream.ps1 -NoBuild
    .\webapp\scripts\update-from-upstream.ps1 -Push
#>
[CmdletBinding()]
param(
    [switch]$Check,    # report what changed upstream, merge nothing
    [switch]$NoBuild,  # merge but skip the container rebuild
    [switch]$Push      # push to origin after a successful merge
)

$ErrorActionPreference = 'Stop'

$UpstreamUrl    = 'https://github.com/DevilXD/TwitchDropsMiner.git'
$UpstreamRemote = 'upstream'
$UpstreamBranch = 'master'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

function Write-Info { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "!!  $m" -ForegroundColor Yellow }
function Stop-With  { param($m) Write-Host "xx  $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- preflight

git rev-parse --git-dir *>$null
if (-not $?) { Stop-With "not a git repository: $RepoRoot" }

git remote get-url $UpstreamRemote *>$null
if (-not $?) {
    Write-Info "adding '$UpstreamRemote' -> $UpstreamUrl"
    git remote add $UpstreamRemote $UpstreamUrl
}

$CurrentBranch = (git rev-parse --abbrev-ref HEAD).Trim()

$dirty = git status --porcelain
if ($dirty) {
    Write-Warn 'you have uncommitted changes:'
    git status --short
    if (-not $Check) { Stop-With 'commit or stash them first, then re-run' }
}

# ------------------------------------------------------------------- fetch

Write-Info "fetching $UpstreamRemote/$UpstreamBranch"
git fetch $UpstreamRemote $UpstreamBranch --quiet
if (-not $?) { Stop-With 'fetch failed - check your network connection' }

$behind = [int](git rev-list --count "HEAD..$UpstreamRemote/$UpstreamBranch").Trim()

if ($behind -eq 0) {
    Write-Info 'already up to date with upstream.'
    exit 0
}

Write-Info "$behind new upstream commit(s):"
git --no-pager log --oneline --no-decorate "HEAD..$UpstreamRemote/$UpstreamBranch" |
    ForEach-Object { "    $_" }

Write-Host ''
Write-Info 'files upstream changed:'
git --no-pager diff --stat "HEAD...$UpstreamRemote/$UpstreamBranch" |
    ForEach-Object { "    $_" }

if ($Check) {
    Write-Host ''
    Write-Info '-Check given; nothing merged.'
    exit 0
}

# ------------------------------------------------------------------- merge

$backup = "pre-upstream-merge-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
git tag $backup
Write-Info "tagged current state as '$backup' (roll back with: git reset --hard $backup)"

Write-Host ''
Write-Info "merging $UpstreamRemote/$UpstreamBranch into $CurrentBranch"
git merge "$UpstreamRemote/$UpstreamBranch" -m "Merge upstream DevilXD/TwitchDropsMiner into $CurrentBranch"

if (-not $?) {
    Write-Host ''
    Write-Warn 'merge stopped on conflicts in:'
    git --no-pager diff --name-only --diff-filter=U | ForEach-Object { "    $_" }
    Write-Host ''
    Write-Warn 'resolve them, then:  git add <files>; git commit'
    Write-Warn 'or abandon the merge: git merge --abort'
    exit 1
}

Write-Info 'merged cleanly.'

# --------------------------------------------------------- sanity + rebuild

# the web layer imports the upstream modules directly, so a rename upstream
# would surface here rather than at runtime in the container
$py = (Get-Command python -ErrorAction SilentlyContinue)
if ($py) {
    Write-Info 'checking the web adapter still imports the miner'
    Push-Location webapp
    $probe = @'
import sys; sys.path.insert(0, ".")
import bootstrap; bootstrap.install()
import twitch
assert twitch.GUIManager.__name__ == "WebGUIManager"
print("    adapter OK")
'@
    $probe | & $py.Source - 2>$null
    if (-not $?) {
        Write-Warn 'could not verify the adapter locally (missing deps is fine -'
        Write-Warn 'the container has them). If the build fails, check'
        Write-Warn 'webapp/gui_adapter.py against any GUI changes upstream made.'
    }
    Pop-Location
}

if (-not $NoBuild) {
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        Write-Info 'rebuilding the container image'
        docker compose -f webapp/docker-compose.yml build
        if ($?) {
            Write-Info 'image rebuilt. Restart with:'
            Write-Host '    docker compose -f webapp/docker-compose.yml up -d'
        } else {
            Stop-With 'docker build failed - the merge is committed; fix the build and retry'
        }
    } else {
        Write-Warn 'docker not found; skipping rebuild.'
    }
}

if ($Push) {
    Write-Info "pushing $CurrentBranch to origin"
    git push origin $CurrentBranch
} else {
    Write-Host ''
    Write-Info "not pushed. When you are happy:  git push origin $CurrentBranch"
}
