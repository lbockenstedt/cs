# PowerShell 5.1. Positional sim_phy is resolved by the orchestrator.
$version = '0.01'
$sim_phy = 'wireless'
if ($args.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace($args[0])) { $sim_phy = [string]$args[0] }
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
. 'C:\Scripts\network_common.ps1'
$version = '0.01'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'client_offline.ps1' $version

function Get-OfflineMinutes {
    param([string]$Value, [int]$Default)
    $minutes = 0
    if ($Value -match '^[0-9]{1,8}$' -and [int]::TryParse($Value, [ref]$minutes) -and $minutes -ge 1 -and $minutes -le 35791394) {
        return $minutes
    }
    return $Default
}
$minMinutes = Get-OfflineMinutes (get_value 'simulation' 'client_offline_min_minutes') 60
$maxMinutes = Get-OfflineMinutes (get_value 'simulation' 'client_offline_max_minutes') 240
if ($minMinutes -gt $maxMinutes) { $minMinutes, $maxMinutes = $maxMinutes, $minMinutes }
$duration = Get-Random -Minimum ($minMinutes * 60) -Maximum ($maxMinutes * 60 + 1)

switch ($sim_phy) {
    'wireless' { $adapter = Get-WifiAdapter }
    'ethernet' { $adapter = Get-EthernetAdapter }
    default { Write-SimLog "client_offline: unknown sim_phy '$sim_phy'; skipping window"; return }
}
if (-not $adapter) {
    Write-SimLog "client_offline: $sim_phy adapter missing; skipping window"
    return
}
try {
    # Enumerate first so an adapter without IPv4 is not mistaken for a query error.
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object { $_.InterfaceIndex -eq $adapter.ifIndex })
} catch {
    Write-SimLog "client_offline: cannot check management addresses on $($adapter.Name); skipping window"
    return
}
if ($addresses | Where-Object { $_.IPAddress -like '169.253.*' }) {
    Write-SimLog "client_offline: management IP active on $($adapter.Name); skipping window"
    return
}
if ($adapter.Status -eq 'Disabled' -or $adapter.Status -eq 'Not Present') {
    Write-SimLog "client_offline: $($adapter.Name) already disabled or absent; skipping window"
    return
}
$broughtDown = $false
try {
    $adapter | Disable-NetAdapter -Confirm:$false -ErrorAction Stop
    $broughtDown = $true
    Write-SimLog "client_offline: $($adapter.Name) offline for ${duration}s (management/API untouched)"
    Start-Sleep -Seconds $duration
} catch {
    Write-SimLog "client_offline: window failed: $($_.Exception.Message)"
} finally {
    if ($broughtDown) {
        try {
            $adapter | Enable-NetAdapter -Confirm:$false -ErrorAction Stop
            Write-SimLog "client_offline: restored $($adapter.Name)"
        } catch {
            Write-SimLog "client_offline: failed to restore $($adapter.Name): $($_.Exception.Message)"
        }
    }
}
