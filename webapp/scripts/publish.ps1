<#
.SYNOPSIS
    Build and push the miner image to a container registry.

.DESCRIPTION
    The image carries no credentials: the login lives in the /data volume, and
    .dockerignore keeps cookies.jar / settings.json out of the build context.

.EXAMPLE
    .\webapp\scripts\publish.ps1
    .\webapp\scripts\publish.ps1 -Version v1.2
    .\webapp\scripts\publish.ps1 -MultiArch
    .\webapp\scripts\publish.ps1 -NoPush
#>
[CmdletBinding()]
param(
    [string]$Version,                                  # extra tag, e.g. v1.2
    [string]$Repo = $env:REPO,                         # override destination
    [switch]$MultiArch,                                # amd64 + arm64 (needs buildx)
    [switch]$NoPush                                    # build and tag only
)

$ErrorActionPreference = 'Stop'
if (-not $Repo) { $Repo = 'hypeview/twitchdropsminer' }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $RepoRoot

function Write-Info { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "!!  $m" -ForegroundColor Yellow }
function Stop-With  { param($m) Write-Host "xx  $m" -ForegroundColor Red; exit 1 }

$dateTag = Get-Date -Format 'yyyyMMdd'
$gitSha  = (git rev-parse --short HEAD 2>$null)
if (-not $gitSha) { $gitSha = 'unknown' }
if (git status --porcelain 2>$null) {
    $gitSha = "$gitSha-dirty"
    Write-Warn "working tree has uncommitted changes (tagging as $gitSha)"
}

$tags = @("${Repo}:latest", "${Repo}:$dateTag")
if ($Version) { $tags += "${Repo}:$Version" }

Write-Info "repository : $Repo"
Write-Info "tags       : $($tags -join ', ')"
Write-Info "source     : $gitSha"

# --------------------------------------------------------------- auth check

if (-not $NoPush) {
    # a private repo needs credentials; fail early rather than after a long build
    $loggedIn = (docker system info 2>$null | Select-String -Pattern '^\s*Username:')
    if (-not $loggedIn) {
        Write-Warn 'not logged in to a registry - run:  docker login'
        $reply = Read-Host 'Log in now? [Y/n]'
        if ($reply -match '^[Nn]') { Stop-With 'aborted; log in and re-run' }
        docker login
        if (-not $?) { Stop-With 'docker login failed' }
    }
}

# ------------------------------------------------------------------- build

$created = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$buildArgs = @(
    '-f', 'webapp/Dockerfile',
    '--label', 'org.opencontainers.image.title=Twitch Drops Miner',
    '--label', 'org.opencontainers.image.description=Headless Twitch drops miner with a web UI',
    '--label', 'org.opencontainers.image.source=https://github.com/DevilXD/TwitchDropsMiner',
    '--label', "org.opencontainers.image.revision=$gitSha",
    '--label', "org.opencontainers.image.created=$created"
)
foreach ($t in $tags) { $buildArgs += @('-t', $t) }

if ($MultiArch) {
    docker buildx version *>$null
    if (-not $?) { Stop-With 'buildx is required for -MultiArch' }
    # a multi-arch manifest can only be produced by pushing directly
    if ($NoPush) { Stop-With '-MultiArch requires pushing (drop -NoPush)' }

    docker buildx inspect tdm-builder *>$null
    if (-not $?) {
        Write-Info "creating buildx builder 'tdm-builder'"
        docker buildx create --name tdm-builder --driver docker-container --bootstrap | Out-Null
    }
    Write-Info 'building for linux/amd64 + linux/arm64 and pushing'
    docker buildx build --builder tdm-builder --platform linux/amd64,linux/arm64 @buildArgs --push .
    if (-not $?) { Stop-With 'buildx build failed' }
    Write-Info 'pushed multi-arch manifest.'
    exit 0
}

$hostArch = (docker version -f '{{.Server.Arch}}' 2>$null)
Write-Warn "building for this machine's architecture only ($hostArch)."
Write-Warn 'An ARM NAS cannot run an amd64-only image - use -MultiArch for that.'
Write-Info 'building'
docker build @buildArgs .
if (-not $?) { Stop-With 'docker build failed' }

# a quick smoke test beats discovering a broken image after deployment
Write-Info 'smoke-testing the image'
$cid = (docker run -d -P "${Repo}:latest").Trim()
try {
    $portLine = (docker port $cid 8080/tcp | Select-Object -First 1)
    $port = $portLine.Split(':')[-1]
    $ok = $false
    foreach ($i in 1..40) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
} finally {
    docker rm -f $cid *>$null
}
if (-not $ok) { Stop-With 'the built image did not become healthy - not pushing' }
Write-Info 'smoke test passed.'

if ($NoPush) {
    Write-Info '-NoPush given; built and tagged locally only.'
    exit 0
}

# -------------------------------------------------------------------- push

foreach ($t in $tags) {
    Write-Info "pushing $t"
    docker push $t
    if (-not $?) { Stop-With "push failed for $t" }
}

Write-Host ''
Write-Info 'done. Deploy elsewhere with:'
Write-Host @"
    docker pull ${Repo}:latest
    docker run -d --name twitch-drops-miner --restart unless-stopped ``
      -p 8080:8080 -v twitchdropsminer_miner-data:/data ${Repo}:latest
"@
