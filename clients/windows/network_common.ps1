# ============================================================================ #
# network_common.ps1 — shared network helpers + connect state for the Windows   #
# sim client.                                                                    #
#                                                                                #
# PowerShell 5.1 (Desktop) port of clients/linux/network_common.sh. A SOURCED    #
# LIBRARY: dot-sourced by connect_psk.ps1 / connect_1x.ps1 (which are in turn    #
# dot-sourced by simulation.ps1). Holds the connection-state variables and the   #
# low-level wait/detect helpers that every connect path uses.                    #
#                                                                                #
# All WLAN operations go through netsh / Get-NetAdapter / Get-NetRoute so we     #
# stay on the stock Windows toolchain (no ternary / no ?? — PS 5.1 only).        #
#                                                                                #
# Dot-source AFTER ini-parser.ps1 (get_value) and common.ps1 (Write-SimLog,     #
# Get-WlanAdapter, Get-DnsDefaultGateway, Test-DnsGatewayAlive). We dot-source   #
# both here so this file stands alone when pulled in by a connect_*.ps1.         #
# ============================================================================ #
$version = '0.01'

. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

# ---------------------------------------------------------------------------- #
# Connection state — shared across all connect paths.                          #
# ---------------------------------------------------------------------------- #
# Consecutive genuine-connect failures since the last success — drives the
# scan-wait ramp (different drivers scan at different speeds; a flat long wait
# would delay every healthy connect) and GATES the radio cycle (a fresh attempt
# doesn't cycle; a retry-after-failure does — cycling is a reset-on-failure, not
# routine). Bumped by Register-ConnectFailure on a failed connect, reset to 0 by
# Register-ConnectSuccess. Script-scoped so it persists across loop iterations
# (one sourced while-loop), resets on re-exec. The fail-sim fast paths and the
# auth_fail flap pass -Track:$false and are EXCLUDED from this counter.
if ($null -eq $script:ReconnectFails) { $script:ReconnectFails = 0 }

# How many consecutive genuine-connect failures before we escalate to a HARD
# radio cycle (Disable-NetAdapter / Enable-NetAdapter). The early retries just
# ride the scan-wait ramp with the scan cache kept WARM — a radio bounce wipes
# that cache, so on a slow driver (SSID can take ~1 min to surface) bouncing on
# every retry resets discovery to zero and the client never lands. Only a
# persistent failure (>= this many) triggers the reset. An explicit -Reset still
# forces a cycle immediately (the "reset the adapter" recovery site). Mirrors the
# Linux _RADIO_CYCLE_AFTER=5.
$script:RadioCycleAfter = 5

# ---------------------------------------------------------------------------- #
# Target SSID / connected SSID.                                                 #
# ---------------------------------------------------------------------------- #
# Get-SimTargetSsid — the SSID we actually connect to. With site_based_ssid=on
# the bucket SSID is prefixed with the site name ($wsite-$ssid) so one bucket can
# map to different SSIDs per site. Mirrors the target_ssid block in the bash
# connect paths. Reads $script:ssid / $script:wsite / $script:site_based_ssid,
# which simulation.ps1 sets each loop.
function Get-SimTargetSsid {
    if ($script:site_based_ssid -eq 'on') {
        return "$($script:wsite)-$($script:ssid)"
    }
    return $script:ssid
}

# Get-SimConnectedSsid — the SSID the wlan adapter is currently associated to
# (empty when disconnected). Parses `netsh wlan show interfaces`, ignoring the
# BSSID row. Mirrors nmcli's "connected" read.
function Get-SimConnectedSsid {
    $out = netsh wlan show interfaces 2>$null
    foreach ($line in $out) {
        if ($line -match '^\s*SSID\s*:\s*(.+)$' -and $line -notmatch 'BSSID') {
            $v = $matches[1].Trim()
            if (-not [string]::IsNullOrWhiteSpace($v)) { return $v }
        }
    }
    return ''
}

# ---------------------------------------------------------------------------- #
# Adapter state probes.                                                         #
# ---------------------------------------------------------------------------- #
# Test-WifiBusy — is the wlan adapter actively connecting or already associated?
# Returns $true (BUSY -> do NOT cycle the radio) when netsh reports the interface
# state as connecting / authenticating / associating / connected. Returns $false
# (idle -> cycling allowed) when disconnected/unavailable or no adapter. Used to
# VETO a radio cycle: bouncing the radio mid-association tears that down and wipes
# the scan cache, forcing a slow driver to rediscover from scratch — the exact
# thrash we avoid. Mirrors bash _wifi_busy.
function Test-WifiBusy {
    if ([string]::IsNullOrWhiteSpace((Get-WlanAdapter))) { return $false }
    $out = netsh wlan show interfaces 2>$null
    foreach ($line in $out) {
        if ($line -match '^\s*State\s*:\s*(.+)$') {
            $st = $matches[1].Trim()
            if ($st -match 'connect|authenticat|associat') { return $true }
            return $false
        }
    }
    return $false
}

# Test-WifiConnected — is the wlan adapter fully associated (State: connected)?
# Used by the "skip sims but stay associated" path to decide whether a reconnect
# is actually needed, so we don't tear down a working link every iteration the
# sim-load gate trips. Mirrors bash _is_wifi_connected.
function Test-WifiConnected {
    if ([string]::IsNullOrWhiteSpace((Get-WlanAdapter))) { return $false }
    $out = netsh wlan show interfaces 2>$null
    foreach ($line in $out) {
        if ($line -match '^\s*State\s*:\s*connected\s*$') { return $true }
    }
    return $false
}

# ---------------------------------------------------------------------------- #
# Waits (plain polls — every one returns the instant its condition is met).     #
# ---------------------------------------------------------------------------- #
# Wait-RadioReady — poll until the wlan adapter is back Up/Disconnected (ready to
# associate) after a Disable/Enable cycle, instead of a blind Start-Sleep 15.
# Returns the instant the adapter leaves the Disabled/not-present limbo (usually
# 1-2s); returns after -CapSeconds (default 15) and the caller proceeds anyway.
# Mirrors bash _wait_radio_ready.
function Wait-RadioReady {
    param([int]$CapSeconds = 15)
    Write-SimLog "  [radio] waiting up to ${CapSeconds}s for wifi radio to become ready..."
    for ($i = 0; $i -lt $CapSeconds; $i++) {
        $name = Get-WlanAdapter
        if (-not [string]::IsNullOrWhiteSpace($name)) {
            $ad = Get-NetAdapter -Name $name -ErrorAction SilentlyContinue
            if ($ad -and $ad.Status -ne 'Disabled' -and $ad.Status -ne 'Not Present') {
                Write-SimLog "  [radio] ready after ${i}s (status: $($ad.Status))"
                return $true
            }
        }
        Start-Sleep -Seconds 1
    }
    Write-SimLog "  [radio] STILL not ready after ${CapSeconds}s — proceeding anyway"
    return $false
}

# Wait-SsidSeen — poll the scan cache until the target SSID's beacon has been
# heard. Wait-RadioReady only confirms the radio is up; it does NOT mean a scan
# completed, so connecting before the SSID is known fails ("network not found")
# and the recovery path misreads that as a dead adapter -> thrash. This polls
# `netsh wlan show networks mode=bssid` directly and returns the instant the SSID
# appears (usually 2-5s); returns after -CapSeconds (default 20) and the caller
# connects blind (netsh's own scan+connect is the backstop). A fresh scan is
# kicked up front and again every 3s so a quiet channel doesn't strand us on the
# stale cache left by a radio cycle. Mirrors bash _wait_ssid_seen.
function Wait-SsidSeen {
    param([string]$Ssid, [int]$CapSeconds = 20)
    if ([string]::IsNullOrWhiteSpace($Ssid)) { return $false }
    Write-SimLog "  [scan] waiting up to ${CapSeconds}s for SSID '$Ssid' (reconnect-fails=$($script:ReconnectFails))..."
    $pattern = '^\s*SSID\s+\d+\s*:\s*' + [regex]::Escape($Ssid) + '\s*$'
    # netsh has no explicit rescan verb; re-reading "show networks" drives a scan.
    netsh wlan show networks mode=bssid 2>$null | Out-Null
    for ($i = 0; $i -lt $CapSeconds; $i++) {
        $networks = netsh wlan show networks mode=bssid 2>$null
        if ($networks | Select-String -Pattern $pattern -Quiet) {
            Write-SimLog "  [scan] SSID '$Ssid' seen after ${i}s"
            return $true
        }
        if ($i -gt 0 -and ($i % 3) -eq 0) {
            netsh wlan show networks mode=bssid 2>$null | Out-Null
        }
        Start-Sleep -Seconds 1
    }
    Write-SimLog "  [scan] SSID '$Ssid' NOT seen after ${CapSeconds}s — connecting blind (netsh backstop)"
    return $false
}

# Wait-Gateway — poll the default gateway until it answers. The connect helpers
# confirm netsh reached "connected" (association + DHCP), so the route is present;
# the remaining unknown is whether the gateway has answered ARP / is pingable yet.
# Returns the instant it replies, else after -CapSeconds (default 10). Reuses the
# common.ps1 Test-DnsGatewayAlive single-ping check. Mirrors bash _wait_gateway.
function Wait-Gateway {
    param([string]$Gateway, [int]$CapSeconds = 10)
    if ([string]::IsNullOrWhiteSpace($Gateway)) { return $false }
    for ($i = 0; $i -lt $CapSeconds; $i++) {
        if (Test-DnsGatewayAlive $Gateway) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# ---------------------------------------------------------------------------- #
# Scan-wait ramp + radio cycle (the reconnect-fail ramp).                       #
# ---------------------------------------------------------------------------- #
# Get-ScanCap — the scan-wait cap for this attempt. Ramps +5s per consecutive
# failure from a 20s base, capped at 60s; back to 20s once ReconnectFails resets
# on success. Mirrors the bash `scan_cap=$(( 20 + 5 * _reconnect_fails ))`.
function Get-ScanCap {
    $cap = 20 + 5 * $script:ReconnectFails
    if ($cap -gt 60) { $cap = 60 }
    return $cap
}

# Register-ConnectSuccess — genuine connect succeeded: reset the ramp to 20s.
function Register-ConnectSuccess {
    $script:ReconnectFails = 0
    Write-SimLog '  [connect] SUCCESS — scan-wait ramp reset to 20s'
}

# Register-ConnectFailure — genuine connect failed: bump the ramp and log the
# next cap.
function Register-ConnectFailure {
    $script:ReconnectFails = $script:ReconnectFails + 1
    $next = 20 + 5 * $script:ReconnectFails
    if ($next -gt 60) { $next = 60 }
    Write-SimLog "  [connect] FAILED — reconnect-fails now $($script:ReconnectFails); next scan-wait up to ${next}s"
}

# Invoke-RadioCycleIfNeeded — the RECONNECT-FAIL RAMP gate. Cycle the radio
# (Disable-NetAdapter / Enable-NetAdapter, then Wait-RadioReady) only when the
# caller forces it (-Reset) OR we're a tracked attempt AND ReconnectFails has hit
# RadioCycleAfter (5) since the last success. Early retries just extend the
# scan-wait ramp with the cache kept warm — a bounce wipes the cache, which
# stranded slow drivers. Never cycle while the adapter is mid-association
# (Test-WifiBusy) — that would tear down a working connect. Mirrors the radio-
# cycle block shared by connect_wifi / _connect_1x_normal.
function Invoke-RadioCycleIfNeeded {
    param([switch]$Reset, [switch]$Track)
    $due = $Reset -or ($Track -and $script:ReconnectFails -ge $script:RadioCycleAfter)
    if (-not $due) { return }
    if (Test-WifiBusy) {
        Write-SimLog '  [radio] cycle deferred — adapter mid-connect/associated, letting it finish'
        return
    }
    $name = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($name)) { return }
    Write-SimLog "  [radio] cycling radio (reset=$([bool]$Reset), reconnect-fails=$($script:ReconnectFails))"
    Disable-NetAdapter -Name $name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 2
    Enable-NetAdapter -Name $name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Wait-RadioReady 15 | Out-Null
}

# ---------------------------------------------------------------------------- #
# Connect outcome + profile hygiene.                                            #
# ---------------------------------------------------------------------------- #
# Test-ConnectOutcome — after `netsh wlan connect`, wait up to -CapSeconds for the
# interface to reach State: connected AND associate to $Ssid. netsh connect is
# async (returns immediately), so unlike nmcli -w we have to poll for the ACTIVATED
# state ourselves. Returns $true the instant we see the target SSID connected,
# $false at the cap (silent-AP / reject backstop). This is the Windows analogue of
# bash _connect_outcome (which just waited on the nmcli PID).
function Test-ConnectOutcome {
    param([string]$Ssid, [int]$CapSeconds = 30)
    for ($i = 0; $i -lt $CapSeconds; $i++) {
        if ((Get-SimConnectedSsid) -eq $Ssid) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

# Remove-MatchingWifiProfiles — delete every saved WLAN profile whose name
# contains $Match (default 'PSK'... but our profiles are named by SSID, so callers
# pass the target SSID). Forces a fresh association on the next connect (each
# attempt is then a distinct AP event, not a cached-profile reuse). Used by the
# wrong-password / auth-fail fast loops and the reset-adapter recovery path.
# Mirrors bash delete_matching_connections.
function Remove-MatchingWifiProfiles {
    param([string]$Match = 'PSK')
    $out = netsh wlan show profiles 2>$null
    foreach ($line in $out) {
        if ($line -match '^\s*All User Profile\s*:\s*(.+)$') {
            $name = $matches[1].Trim()
            if ($name -like "*$Match*") {
                netsh wlan delete profile name="$name" 2>&1 | Out-Null
            }
        }
    }
}

# Remove-WifiProfileByName — delete one profile by exact name (no-op when empty).
function Remove-WifiProfileByName {
    param([string]$ProfileName)
    if ([string]::IsNullOrWhiteSpace($ProfileName)) { return }
    netsh wlan delete profile name="$ProfileName" 2>&1 | Out-Null
}

# Add-WifiProfileXml — write $Xml to a temp file and `netsh wlan add profile`
# (all-user). Returns the profile-file path (deleted by the caller after connect).
# Shared by the PSK and 1X connect paths so the add step lives in one place.
function Add-WifiProfileXml {
    param([string]$ProfileName, [string]$Xml)
    $tempDir = 'C:\Temp'
    if (-not (Test-Path -LiteralPath $tempDir)) {
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
    }
    $safe = ($ProfileName -replace '[^A-Za-z0-9._-]', '_')
    $path = Join-Path $tempDir ("wifi-$safe.xml")
    Set-Content -LiteralPath $path -Value $Xml -Encoding ASCII
    netsh wlan add profile filename="$path" user=all 2>&1 | Out-Null
    return $path
}
