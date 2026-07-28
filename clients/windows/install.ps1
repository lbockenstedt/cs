<#
.SYNOPSIS
  Bootstrap installer for the LM client-simulation Windows client.

.DESCRIPTION
  One-shot installer for a fresh Windows sim VM. It:
    1. installs dependencies (Git, GitHub CLI, Python, iperf3) via winget,
    2. lays down the client files into C:\Scripts,
    3. writes a bootstrap simulation.conf pointing at the spoke,
    4. runs update.ps1 once to pull the live config + latest scripts from the spoke,
    5. registers the scheduled tasks (ClientSim-Startup at logon, ClientSim-Update
       on a timer) that run the sim + keep it self-updating,
    6. (optionally) enables auto-logon so the sim starts unattended after reboot.

  After this, update.ps1 owns ongoing script + config sync from the spoke — the
  same content-hash → GitHub fallback path the Linux client uses. The Windows
  client is at full parity with Linux (same sims, same CS_OVERRIDE_KEYS); T3
  (mac80211_hwsim vwlan) is Linux-only and intentionally not present here.

.PARAMETER SpokeIp
  IP/host of the cs spoke the client reports to (serves /api/config +
  /api/scripts/windows). Default 169.253.1.1 (the spoke's DHCP-scope address).

.PARAMETER InstallDir
  Where the client lives. Default C:\Scripts (every script anchors here).

.PARAMETER Branch
  Git branch to bootstrap the files from. Default 'main'.

.PARAMETER RepoUrl
  The cs repo. Default https://github.com/lbockenstedt/cs.git.

.PARAMETER AutoLogonUser / AutoLogonPassword
  Optional. When both are set, enable Windows auto-logon so the ClientSim-Startup
  task fires after an unattended reboot (standard for a lab sim VM — the password
  is stored in the registry, so only use a throwaway lab account).

.PARAMETER SkipDeps
  Skip the winget dependency install (deps already present).

.EXAMPLE
  # From an elevated PowerShell on the sim VM:
  irm https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/windows/install.ps1 | iex

.EXAMPLE
  # With a specific spoke + auto-logon:
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/windows/install.ps1))) -SpokeIp 172.16.1.35 -AutoLogonUser simuser -AutoLogonPassword 'P@ss'
#>
[CmdletBinding()]
param(
    [string]$SpokeIp = '169.253.1.1',
    [string]$InstallDir = 'C:\Scripts',
    [string]$Branch = 'main',
    [string]$RepoUrl = 'https://github.com/lbockenstedt/cs.git',
    [string]$AutoLogonUser = '',
    [string]$AutoLogonPassword = '',
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "    OK: $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "    WARN: $m" -ForegroundColor Yellow }

# ── Elevation ────────────────────────────────────────────────────────────────
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Run this installer from an ELEVATED PowerShell (Run as Administrator)."
}
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

Write-Step "LM Windows sim-client installer — spoke=$SpokeIp dir=$InstallDir branch=$Branch"

# ── 1. Dependencies (winget) ────────────────────────────────────────────────
function Install-Winget {
    param([string]$Id, [string]$Name)
    Write-Step "Installing $Name ($Id)"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        # --accept-* so it runs unattended; a re-run is a no-op if present.
        winget install --id $Id --exact --silent --accept-source-agreements `
            --accept-package-agreements --disable-interactivity 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) { Write-Ok $Name }  # -19..189 = already installed
        else { Write-Warn2 "$Name install returned $LASTEXITCODE — check manually" }
    } else {
        Write-Warn2 "winget not found — install $Name manually (Windows 10 1809+/11 ships winget)"
    }
}

if (-not $SkipDeps) {
    Install-Winget 'Git.Git'            'Git'
    Install-Winget 'GitHub.cli'         'GitHub CLI (gh)'
    Install-Winget 'Python.Python.3.12' 'Python 3'
    # iperf3 isn't reliably in winget — fetch the static win64 build into InstallDir.
    Write-Step "Installing iperf3 (for the iperf sim)"
    try {
        $iperfZip = Join-Path $env:TEMP 'iperf3.zip'
        Invoke-WebRequest 'https://iperf.fr/download/windows/iperf-3.1.3-win64.zip' `
            -OutFile $iperfZip -UseBasicParsing -TimeoutSec 60
        $iperfDir = Join-Path $InstallDir 'iperf3'
        Expand-Archive -Path $iperfZip -DestinationPath $iperfDir -Force
        Write-Ok "iperf3 → $iperfDir (add to PATH or the iperf sim will skip)"
    } catch { Write-Warn2 "iperf3 download failed ($_) — install manually for the iperf sim" }
    # Refresh PATH so git/gh/python are visible to the rest of this session.
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path','User')
}

foreach ($tool in 'git') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is required but not on PATH after install. Open a new elevated shell and re-run, or install $tool manually."
    }
}

# ── 2. Install dir + client files (git clone → copy) ────────────────────────
Write-Step "Laying down client files into $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$clone = Join-Path $env:TEMP ("cs-bootstrap-" + [guid]::NewGuid().ToString('N'))
try {
    git clone --depth 1 --branch $Branch $RepoUrl $clone 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git clone failed (branch $Branch)" }
    $winSrc = Join-Path $clone 'clients\windows'
    if (-not (Test-Path $winSrc)) { throw "clients\windows not found in the clone" }
    # Copy every windows client file (.ps1 + .txt + VERSION). Do NOT overwrite an
    # existing simulation.conf (per-host config) — update.ps1 owns that from the spoke.
    Get-ChildItem $winSrc -File | Where-Object { $_.Name -ne 'install.ps1' } | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $InstallDir $_.Name) -Force
    }
    Write-Ok "copied $((Get-ChildItem $winSrc -File).Count - 1) file(s)"
} finally {
    Remove-Item $clone -Recurse -Force -ErrorAction SilentlyContinue
}

# ── 3. Bootstrap simulation.conf (server_url → spoke) ───────────────────────
$conf = Join-Path $InstallDir 'simulation.conf'
if (-not (Test-Path $conf)) {
    Write-Step "Writing bootstrap simulation.conf (server_url=http://${SpokeIp}:8080)"
    @"
[server]
server_url=http://${SpokeIp}:8080

[simulation]
web_server=on
"@ | Set-Content -Path $conf -Encoding ASCII
    Write-Ok "bootstrap config written (update.ps1 replaces it with the live config)"
} else {
    Write-Ok "simulation.conf already present — left as-is"
}

# ── 4. First config + script sync from the spoke ────────────────────────────
Write-Step "Running update.ps1 once to pull live config + latest scripts from the spoke"
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir 'update.ps1')
    Write-Ok "initial sync complete"
} catch { Write-Warn2 "initial update.ps1 run failed ($_) — the scheduled task will retry" }

# ── 5. Scheduled tasks ──────────────────────────────────────────────────────
function Register-SimTask {
    param([string]$Name, [string]$Script, [Microsoft.Management.Infrastructure.CimInstance[]]$Trigger, [string]$Desc)
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir $Script)`""
    $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principalT = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $set `
        -Description $Desc -Force | Out-Null
    Write-Ok "scheduled task '$Name'"
}

Write-Step "Registering scheduled tasks"
# Startup: at logon, run launch-terminals.ps1 (it launches startup.ps1 → the sim
# loop) in the interactive session so GUI sims (browser/iperf) have a desktop.
$startupTrigger = New-ScheduledTaskTrigger -AtLogOn
$startupAction  = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'launch-terminals.ps1')`""
$startupPrincipal = New-ScheduledTaskPrincipal -GroupId 'BUILTIN\Users' -RunLevel Highest
$startupSettings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
Unregister-ScheduledTask -TaskName 'ClientSim-Startup' -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'ClientSim-Startup' -Action $startupAction -Trigger $startupTrigger `
    -Settings $startupSettings -Principal $startupPrincipal `
    -Description 'LM client-sim: launch the simulation at logon' -Force | Out-Null
Write-Ok "scheduled task 'ClientSim-Startup' (at logon)"

# Self-update: every 30 min, run update.ps1 (content-hash sync from the spoke).
$updTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 30)
Register-SimTask -Name 'ClientSim-Update' -Script 'update.ps1' -Trigger $updTrigger `
    -Desc 'LM client-sim: self-update scripts + config from the spoke (every 30m)'

# ── 6. Optional auto-logon (unattended lab VM) ──────────────────────────────
if ($AutoLogonUser -and $AutoLogonPassword) {
    Write-Step "Enabling auto-logon for '$AutoLogonUser' (lab convenience — password stored in registry)"
    $win = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $win 'AutoAdminLogon' '1'
    Set-ItemProperty $win 'DefaultUserName' $AutoLogonUser
    Set-ItemProperty $win 'DefaultPassword' $AutoLogonPassword
    Write-Ok "auto-logon enabled — the sim starts on the next reboot without a manual login"
} else {
    Write-Warn2 "auto-logon NOT set (no -AutoLogonUser/-AutoLogonPassword) — the sim starts at the next interactive logon"
}

Write-Host ""
Write-Step "Done. The sim runs at logon; it self-updates from the spoke every 30 min."
Write-Host "    Log: $InstallDir\sim.log   |   Reboot (or log in) to start the simulation." -ForegroundColor Gray
