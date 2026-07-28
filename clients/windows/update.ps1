# ============================================================================ #
# update.ps1 — Windows sim-client updater. Parity port of clients/linux/update.sh #
#                                                                               #
# Sync order mirrors the Linux tiers:                                           #
#   Tier 1  Web server (spoke API)  — content-hash manifest decides same/changed #
#   Tier 3  GitHub (git reset --hard + self-heal re-clone)                       #
#   Tier 4  SMB share (last resort)                                              #
# Config (simulation.conf + user-overrides.conf) is ALWAYS pulled from the spoke #
# when reachable, independent of the script-update tier — the [username] engine/ #
# quota assignments live only on the spoke and never in GitHub.                  #
#                                                                               #
# Target: Windows PowerShell 5.1 (Desktop) — no ternary / no ?? / no 7-only.    #
# Dot-sourced by startup.ps1 and simulation.ps1 — keep no param() block and the  #
# top-level $version banner so those callers run it unchanged.                   #
# ============================================================================ #
$version = '0.01'
$scriptRoot = 'C:\Scripts'
$logPath = Join-Path $scriptRoot 'sim.log'
$debugPath = Join-Path $scriptRoot 'debug-update.log'

if (-not (Test-Path -LiteralPath $scriptRoot)) {
    New-Item -ItemType Directory -Path $scriptRoot -Force | Out-Null
}

# common.ps1 gives us Write-SimLog (and the shared C:\Scripts anchoring); ini-parser
# gives us Parse-IniFile / get_value. Dot-source both (idempotent if a caller already did).
. (Join-Path $scriptRoot 'common.ps1')
. (Join-Path $scriptRoot 'ini-parser.ps1')

# Write-UpdateLog: keep the debug-update.log trail AND fan out to sim.log via the
# shared Write-SimLog helper so all client logs stay consistent.
function Write-UpdateLog {
    param([string]$Message)
    try { Add-Content -LiteralPath $debugPath -Value $Message -ErrorAction SilentlyContinue } catch {}
    Write-SimLog $Message
}

"Update Script Version $version" | Tee-Object -FilePath $debugPath | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Out-Null
Write-UpdateLog "Update Script Version $version"

Stop-Process -Name firefox -ErrorAction SilentlyContinue

$global:iniConfig = Parse-IniFile (Join-Path $scriptRoot 'simulation.conf')

#------------------------------------------------------------
# Read config values
#------------------------------------------------------------
# web_server defaults ON (hub mode); flip to off ONLY when the conf literally says
# "off". A missing/unreadable conf therefore stays ON so the client still ATTEMPTS
# to self-update out of a bad state — matches update.sh line 22.
$web_server = get_value 'simulation' 'web_server'
if ($web_server -ne 'off') { $web_server = 'on' }

$server_url = get_value 'server' 'server_url'
if ([string]::IsNullOrWhiteSpace($server_url)) { $server_url = 'http://169.253.1.1:8080' }
$server_url = $server_url.TrimEnd('/')

# GitHub gate: Windows siblings (simulation.ps1/dashboard.ps1) use `public_repo`;
# Linux uses `github_repo`. Accept EITHER = on so parity works both ways.
$public_repo = get_value 'simulation' 'public_repo'
$github_repo = get_value 'simulation' 'github_repo'
$repo_location = get_value 'simulation' 'repo_location'
$repo_branch = get_value 'simulation' 'repo_branch'
$smb_repo = get_value 'simulation' 'smb_repo'
$smb_location = get_value 'address' 'smb_address'

$sourceFound = $false

#------------------------------------------------------------
# Compare two dotted-numeric versions. Returns $true when EQUAL, $false otherwise.
# A leading dot means an implicit 0 major (".103" -> 0.103). Callers treat any
# difference (newer OR older) as "changed" — a rollback/reset of the served VERSION
# must land on the fleet; the served build is authoritative (see update.sh 42-49).
#------------------------------------------------------------
function Test-VersionEqual {
    param([string]$Remote, [string]$Local)
    $a = $Remote; $b = $Local
    if ($a -like '.*') { $a = '0' + $a }
    if ($b -like '.*') { $b = '0' + $b }
    $av = @($a -split '\.'); $bv = @($b -split '\.')
    $max = [Math]::Max($av.Count, $bv.Count)
    for ($i = 0; $i -lt $max; $i++) {
        $ai = 0; $bi = 0
        if ($i -lt $av.Count) { $sa = [regex]::Replace($av[$i], '[^0-9]', ''); if ($sa) { $ai = [int]$sa } }
        if ($i -lt $bv.Count) { $sb = [regex]::Replace($bv[$i], '[^0-9]', ''); if ($sb) { $bi = [int]$sb } }
        if ($ai -ne $bi) { return $false }
    }
    return $true
}

#------------------------------------------------------------
# Health check (mirrors update.sh check_api_up): HTTP 200 on /api/health AND the
# body reports "status":"ok". A reachable-but-not-OK endpoint counts as DOWN.
#------------------------------------------------------------
function Test-ApiUp {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -eq 200 -and $r.Content -match '"status"\s*:\s*"ok"') {
            Write-UpdateLog 'API confirmed UP'
            return $true
        }
        Write-UpdateLog 'HTTP 200 but body missing status:ok — API is DOWN'
    } catch {
        Write-UpdateLog "Web server not reachable — $($_.Exception.Message)"
    }
    return $false
}

#------------------------------------------------------------
# SHA256 of a local file, lowercase hex (matches the manifest's sha256 hashes).
#------------------------------------------------------------
function Get-FileSha256 {
    param([string]$Path)
    try { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower() } catch { return '' }
}

#------------------------------------------------------------
# CONTENT-HASH sync decision — port of update.sh _scripts_sync_decision.
# GETs <server_url>/api/scripts/manifest?platform=windows (flat JSON: file -> sha256).
# Returns a PSCustomObject:
#   Decision = 'changed' — a served script is missing locally or differs -> full sync
#   Decision = 'same'    — every served script matches -> config-only
#   Decision = 'unknown' — no/empty/non-JSON manifest -> caller falls back to VERSION
#   Changed  = the subset of files to (re)download (missing + mismatched)
# No version number / downgrade guard: the client mirrors the served bytes in EITHER
# direction, so a rollback/reset/frozen-VERSION all self-correct.
# Assumed manifest shape: a flat object mapping bare filename -> lowercase sha256 hex.
#------------------------------------------------------------
function Get-ScriptsSyncDecision {
    param([string]$Url)
    $result = [PSCustomObject]@{ Decision = 'unknown'; Changed = @() }

    $body = $null
    try {
        $resp = Invoke-WebRequest -Uri "$Url/api/scripts/manifest?platform=windows" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
        $body = $resp.Content
    } catch {
        return $result   # no manifest endpoint / error -> unknown
    }
    if ([string]::IsNullOrWhiteSpace($body)) { return $result }
    $trimmed = $body.Trim()
    if ($trimmed -eq '{}') { return $result }              # empty object -> unknown
    if (-not $trimmed.StartsWith('{')) { return $result }  # not a JSON object -> unknown

    $manifest = $null
    try { $manifest = $trimmed | ConvertFrom-Json } catch { return $result }
    if ($null -eq $manifest) { return $result }

    $props = @($manifest.PSObject.Properties | Where-Object { $_.MemberType -eq 'NoteProperty' })
    if ($props.Count -eq 0) { return $result }             # no files -> unknown

    $changed = New-Object System.Collections.Generic.List[string]
    foreach ($p in $props) {
        $fname = $p.Name
        if ([string]::IsNullOrWhiteSpace($fname)) { continue }
        $want = ''
        if ($null -ne $p.Value) { $want = ([string]$p.Value).Trim().ToLower() }
        if ([string]::IsNullOrEmpty($want)) { continue }
        $local = Join-Path $scriptRoot $fname
        if (-not (Test-Path -LiteralPath $local)) {
            $changed.Add($fname)
            continue
        }
        $have = Get-FileSha256 $local
        if ($have -ne $want) { $changed.Add($fname) }
    }

    if ($changed.Count -gt 0) {
        $result.Decision = 'changed'
        $result.Changed = $changed.ToArray()
    } else {
        $result.Decision = 'same'
    }
    return $result
}

#------------------------------------------------------------
# Download <Url> to <Dest> atomically (temp file then Move-Item). Returns $true only
# on HTTP 200 (IWR throws on non-2xx) with a NON-EMPTY body. On failure the existing
# file is left untouched. Mirrors update.sh's "only overwrite on 200 + non-empty".
#------------------------------------------------------------
function Save-UrlToFile {
    param([string]$Url, [string]$Dest, [int]$TimeoutSec = 30)
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ((Test-Path -LiteralPath $tmp) -and (Get-Item -LiteralPath $tmp).Length -gt 0) {
            Move-Item -LiteralPath $tmp -Destination $Dest -Force
            return $true
        }
    } catch {}
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    return $false
}

#------------------------------------------------------------
# Always-on config sync from the spoke: simulation.conf (with per-host overrides
# injected server-side via ?hostname=) + user-overrides.conf. Each is only replaced
# on 200 + non-empty; a 404 on overrides is acceptable (kept). Port of update.sh
# 292-328 / 356-398. Assumes /api/config and /api/config/overrides return the file body.
#------------------------------------------------------------
function Sync-SpokeConfig {
    param([string]$Url)
    $hostName = [uri]::EscapeDataString($env:COMPUTERNAME)
    if (Save-UrlToFile "$Url/api/config?hostname=$hostName" (Join-Path $scriptRoot 'simulation.conf') 15) {
        Write-UpdateLog 'simulation.conf synced from spoke'
    } else {
        Write-UpdateLog 'Config fetch failed or empty — keeping existing simulation.conf'
    }
    if (Save-UrlToFile "$Url/api/config/overrides" (Join-Path $scriptRoot 'user-overrides.conf') 15) {
        Write-UpdateLog 'user-overrides.conf synced from spoke'
    } else {
        Write-UpdateLog 'user-overrides.conf not available — keeping existing'
    }
}

#============================================================
# Config sync when web_server is OFF — pull /api/config anyway so a GitHub/SMB
# client still receives its spoke-injected engine/quota assignment (update.sh 299).
#============================================================
if ($web_server -ne 'on' -and $server_url) {
    if (Test-ApiUp $server_url) {
        Write-UpdateLog "Config sync from spoke ($server_url) — web_server off, pulling /api/config anyway"
        Sync-SpokeConfig $server_url
    }
}

#============================================================
# TIER 1 — Web Server (spoke API)
#============================================================
Write-UpdateLog 'Updating Scripts'

if ($web_server -eq 'on' -and $server_url) {
    Write-UpdateLog "Tier 1: Trying Web Server ($server_url)..."

    if (Test-ApiUp $server_url) {
        # Content-hash decision first; VERSION compare only when there is no manifest.
        $sync = Get-ScriptsSyncDecision $server_url
        $decision = $sync.Decision
        $changedFiles = $sync.Changed

        if ($decision -eq 'unknown') {
            $localVer = ''
            try { $localVer = (Get-Content -LiteralPath (Join-Path $scriptRoot 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1) } catch {}
            if ($localVer) { $localVer = $localVer.Trim() }
            $remoteVer = ''
            try {
                $vr = Invoke-WebRequest -Uri "$server_url/api/scripts/windows/VERSION" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
                $remoteVer = ([string]$vr.Content).Trim()
            } catch {}
            Write-UpdateLog "manifest unavailable — VERSION fallback: local=$localVer remote=$remoteVer"
            if ([string]::IsNullOrWhiteSpace($remoteVer)) {
                $decision = 'skip'
            } elseif (Test-VersionEqual $remoteVer $localVer) {
                $decision = 'same'
            } else {
                $decision = 'changed'   # full-list re-pull (Changed stays empty -> pull all)
                $changedFiles = @()
            }
        } else {
            Write-UpdateLog "content-hash sync decision: $decision"
        }

        if ($decision -eq 'skip') {
            Write-UpdateLog 'remote VERSION empty — skipping script sync'
            Sync-SpokeConfig $server_url
            $sourceFound = $true
        } elseif ($decision -eq 'same') {
            Write-UpdateLog 'Scripts already up to date — checking config only'
            Sync-SpokeConfig $server_url
            $sourceFound = $true
        } else {
            # changed: download the mismatched/missing files (content-hash path), or the
            # full windows file list (VERSION fallback path, when Changed is empty).
            if ($changedFiles -and $changedFiles.Count -gt 0) {
                Write-UpdateLog "Scripts differ from spoke — syncing $($changedFiles.Count) changed file(s)"
                $fileList = $changedFiles
            } else {
                Write-UpdateLog 'Scripts differ from spoke (VERSION fallback) — re-pulling full windows file list'
                $fileList = @()
                try {
                    $listResp = Invoke-WebRequest -Uri "$server_url/api/scripts/list?platform=windows" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
                    $fileList = @($listResp.Content | ConvertFrom-Json)
                } catch {
                    Write-UpdateLog "WARNING: could not get script list from spoke ($($_.Exception.Message))"
                }
            }

            $syncOk = $true
            foreach ($filename in $fileList) {
                if ([string]::IsNullOrWhiteSpace($filename)) { continue }
                if (Save-UrlToFile "$server_url/api/scripts/windows/$filename" (Join-Path $scriptRoot $filename) 30) {
                    Write-UpdateLog "  + $filename"
                } else {
                    Write-UpdateLog "  ! WARNING: failed to fetch $filename"
                    $syncOk = $false
                }
            }

            # Config is pulled regardless of script-download outcome.
            Sync-SpokeConfig $server_url

            if ($syncOk -and $fileList -and @($fileList).Count -gt 0) {
                Write-UpdateLog 'Web server sync succeeded'
                $sourceFound = $true
            } elseif ($syncOk) {
                # Nothing to download (empty list) but config synced — still a success.
                $sourceFound = $true
            } else {
                Write-UpdateLog 'Web server reachable but script sync incomplete — falling through'
            }
        }
    } else {
        Write-UpdateLog 'Web server unreachable — skipping Tier 1'
    }
}

#============================================================
# TIER 3 — GitHub (git reset --hard + self-heal re-clone + remote-URL fix)
#============================================================
if (-not $sourceFound -and ($public_repo -eq 'on' -or $github_repo -eq 'on')) {
    Write-UpdateLog "Tier 3: Trying GitHub ($repo_location)..."
    $originalLocation = Get-Location
    try {
        if ([string]::IsNullOrWhiteSpace($repo_location)) {
            throw 'repo_location is not defined'
        }
        $repoDir = Join-Path $env:USERPROFILE 'client-sim'

        Set-Location $env:USERPROFILE
        if ((Test-Path -LiteralPath $repoDir) -and -not (Test-Path -LiteralPath (Join-Path $repoDir '.git'))) {
            Write-UpdateLog 'Directory exists but is not a git repo. Removing directory.'
            Remove-Item -LiteralPath $repoDir -Recurse -Force
        }

        if (-not (Test-Path -LiteralPath $repoDir)) {
            Write-UpdateLog 'Cloning repository...'
            git clone $repo_location $repoDir 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Clone failed for $repo_location" }
        }

        Set-Location $repoDir
        $insideRepo = [string](git rev-parse --is-inside-work-tree 2>$null)
        if ($insideRepo) { $insideRepo = $insideRepo.Trim() }
        if ($LASTEXITCODE -ne 0 -or $insideRepo -ne 'true') {
            Write-UpdateLog 'Repo appears corrupted. Re-cloning...'
            Set-Location $env:USERPROFILE
            Remove-Item -LiteralPath $repoDir -Recurse -Force -ErrorAction SilentlyContinue
            git clone $repo_location $repoDir 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Re-clone failed for $repo_location" }
            Set-Location $repoDir
        }

        $currentRemote = [string](git remote get-url origin 2>$null)
        if ($currentRemote) { $currentRemote = $currentRemote.Trim() }
        if ($LASTEXITCODE -ne 0 -or $currentRemote -ne $repo_location) {
            Write-UpdateLog 'Remote URL mismatch. Fixing...'
            git remote remove origin 2>$null | Out-Null
            git remote add origin $repo_location 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        }

        git config http.connectTimeout 5
        git config http.lowSpeedLimit 100
        git config http.lowSpeedTime 30
        git config http.maxRequests 2
        git config pull.rebase true

        git fetch origin 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'git fetch origin failed' }

        git show-ref --verify --quiet "refs/heads/$repo_branch"
        if ($LASTEXITCODE -eq 0) {
            Write-UpdateLog "Switching to branch: $repo_branch"
            git switch $repo_branch 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        } else {
            git ls-remote --exit-code --heads origin $repo_branch 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-UpdateLog "Creating branch: $repo_branch"
                git switch -c $repo_branch "origin/$repo_branch" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            } else {
                throw "Branch '$repo_branch' not found"
            }
        }

        git reset --hard "origin/$repo_branch" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git reset failed for origin/$repo_branch" }

        $windowsDir = Join-Path $repoDir 'windows'
        if (-not (Test-Path -LiteralPath $windowsDir)) { throw 'windows directory not found in repository' }

        Copy-Item (Join-Path $windowsDir '*.ps1') $scriptRoot -Force -ErrorAction Stop
        Copy-Item (Join-Path $windowsDir '*.txt') $scriptRoot -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath (Join-Path $windowsDir 'VERSION')) {
            Copy-Item (Join-Path $windowsDir 'VERSION') (Join-Path $scriptRoot 'VERSION') -Force -ErrorAction SilentlyContinue
        }

        $configsDir = Join-Path $repoDir 'configs'
        if (Test-Path -LiteralPath (Join-Path $configsDir 'simulation.conf')) {
            Copy-Item (Join-Path $configsDir 'simulation.conf') (Join-Path $scriptRoot 'simulation.conf') -Force -ErrorAction Stop
        } else {
            throw 'simulation.conf not found in configs directory'
        }
        if (Test-Path -LiteralPath (Join-Path $configsDir 'user-overrides.conf')) {
            Copy-Item (Join-Path $configsDir 'user-overrides.conf') (Join-Path $scriptRoot 'user-overrides.conf') -Force -ErrorAction SilentlyContinue
        }

        Write-UpdateLog 'GitHub sync succeeded'
        $sourceFound = $true
    } catch {
        Write-UpdateLog "GitHub sync failed: $($_.Exception.Message)"
    } finally {
        Set-Location $originalLocation
    }
}

#============================================================
# TIER 4 — SMB Share (last resort)
#============================================================
if (-not $sourceFound -and $smb_repo -eq 'on' -and -not [string]::IsNullOrWhiteSpace($smb_location)) {
    Write-UpdateLog "Tier 4: Trying SMB ($smb_location)..."
    $smbPath = $smb_location
    if ($smbPath -like '//*') {
        $smbPath = '\\' + $smbPath.TrimStart('/').Replace('/', '\')
    }
    try {
        New-PSDrive -Name SimSMB -PSProvider FileSystem -Root $smbPath -ErrorAction Stop | Out-Null
        try {
            Copy-Item 'SimSMB:\Scripts\*' $scriptRoot -Recurse -Force -ErrorAction Stop
            if (Test-Path -LiteralPath 'SimSMB:\configs\simulation.conf') {
                Copy-Item 'SimSMB:\configs\simulation.conf' (Join-Path $scriptRoot 'simulation.conf') -Force -ErrorAction Stop
            }
            if (Test-Path -LiteralPath 'SimSMB:\configs\user-overrides.conf') {
                Copy-Item 'SimSMB:\configs\user-overrides.conf' (Join-Path $scriptRoot 'user-overrides.conf') -Force -ErrorAction SilentlyContinue
            }
            Write-UpdateLog 'SMB sync succeeded'
            $sourceFound = $true
        } finally {
            Remove-PSDrive -Name SimSMB -ErrorAction SilentlyContinue
        }
    } catch {
        Write-UpdateLog "SMB sync failed: $($_.Exception.Message)"
    }
}

#============================================================
# Result
#============================================================
if (-not $sourceFound) {
    Write-UpdateLog 'ERROR: All update sources failed — no files updated'
}

# Re-assert no-sleep / no-screensaver every update cycle so a GPO refresh or a
# user toggle can't leave a sim VM asleep or blanked (Set-NoSleepNoScreensaver
# is idempotent). Best-effort — never fail the update over it.
try { Set-NoSleepNoScreensaver } catch { Write-UpdateLog "no-sleep re-assert skipped: $($_.Exception.Message)" }

Write-UpdateLog 'Update complete'
