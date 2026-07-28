$version = '0.01'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-startup.log'

function Write-StartupLog {
    param([string]$Message)
    $Message | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}

"Startup Script Version $version" | Tee-Object -FilePath $debugPath
"Startup Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

Start-Job -ScriptBlock {
    & powershell -NoProfile -ExecutionPolicy Bypass -File 'C:\Scripts\sys_mon.ps1'
} | Out-Null

. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

# Sleep / monitor-off / hibernate + SCREEN SAVER all disabled (shared helper in
# common.ps1, sourced just above). Runs in the sim user's context at logon, so
# the per-user screen-saver registry (HKCU) is the sim user's — the reliable spot.
Write-StartupLog 'Disabling sleep, monitor timeout, hibernate, and screen saver'
Set-NoSleepNoScreensaver
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionsReport

$username = Get-SimUsername
$hostname = $env:COMPUTERNAME
# Bucket via crc32(hostname) % 10 (common.ps1 Get-SimBucket) — MUST match the
# Linux client + the spoke's sim_config.bucket_for() (was SHA256%10, which diverged).
$bucketNum = Get-SimBucket
$simulation_id = "s$bucketNum"
$userSimId = get_value $username 'simulation_id'
if (-not [string]::IsNullOrWhiteSpace($userSimId)) { $simulation_id = $userSimId }

$reboot_schedule = [int](get_value 'simulation' 'reboot_schedule')
$repo_location = get_value 'simulation' 'repo_location'
$vh_server = get_value 'simulation' 'vh_server'
$sim_phy = get_value $simulation_id 'sim_phy'
$rapid_update = get_value 'simulation' 'rapid_update'

$overrideRepoLocation = get_value $username 'repo_location'
if (-not [string]::IsNullOrWhiteSpace($overrideRepoLocation)) {
    $repo_location = $overrideRepoLocation
}

$overrideVhServer = get_value $username 'vh_server'
if (-not [string]::IsNullOrWhiteSpace($overrideVhServer)) {
    $vh_server = $overrideVhServer
}

$overrideSimPhy = get_value $username 'sim_phy'
if (-not [string]::IsNullOrWhiteSpace($overrideSimPhy)) {
    $sim_phy = $overrideSimPhy
}

# Syslog configuration is not applicable on Windows; Event Log is used instead.
Write-StartupLog "Simulation ID resolved: $simulation_id"
Write-StartupLog "Repo location: $repo_location"
Write-StartupLog "VH server: $vh_server"
Write-StartupLog "Simulation phy: $sim_phy"
Write-StartupLog "Rapid update: $rapid_update"

$rn = $reboot_schedule + (Get-Random -Minimum 0 -Maximum 600)
Write-StartupLog "Scheduling reboot in $rn minutes"
shutdown /r /t ([int]$rn * 60) | Out-Null

Write-StartupLog 'Bringing up all interfaces online'
Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object {
    Enable-NetAdapter -Name $_.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}

$wladapter = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'wireless|wlan|wi-fi' -or $_.InterfaceDescription -match 'wireless|wlan|wi-fi|802\.11' } |
    Select-Object -First 1 -ExpandProperty Name
$eadapter = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'ethernet|eth' -or $_.InterfaceDescription -match 'ethernet' } |
    Select-Object -First 1 -ExpandProperty Name

if ($wladapter) {
    Write-StartupLog "WLAN Adapter name $wladapter"
}
if ($eadapter) {
    Write-StartupLog "Wired Adapter name $eadapter"
}

Write-StartupLog 'Updating Simulation from repo'
. 'C:\Scripts\update.ps1'

if ($vh_server -eq 'on') {
    Write-StartupLog 'Starting VH client'
    # Use the arch-neutral symlink created by install.ps1 (vhclient.exe → vhclientx86_64.exe or vhclientarm64.exe)
    Start-Process 'vhclient.exe' -ArgumentList '-n' -WindowStyle Hidden -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 5
}

Write-StartupLog 'Launching Simulation Script'
. 'C:\Scripts\simulation.ps1'
