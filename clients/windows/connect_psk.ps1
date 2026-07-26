# ============================================================================ #
# connect_psk.ps1 — PSK (personal) wifi connect functions for simulation.ps1.   #
#                                                                                #
# PowerShell 5.1 (Desktop) port of clients/linux/connect_psk.sh. Owns the two    #
# PSK connect paths:                                                             #
#   Connect-WifiPsk      — the normal, genuine associate (also the 1X dispatcher)#
#   Connect-WifiPskFail  — the fast wrong-password loop for the ssidpw_fail sim. #
#                                                                                #
# Dot-source AFTER network_common.ps1 (it provides the state/helpers below and   #
# itself dot-sources ini-parser.ps1 + common.ps1). All WLAN ops via netsh.       #
#                                                                                #
# Depends on simulation.ps1 having set (each loop, before these are CALLED):     #
#   $script:ssid, $script:ssidpw, $script:wsite, $script:site_based_ssid         #
# Plus the shared state/helpers in network_common.ps1: ReconnectFails,           #
# RadioCycleAfter, Test-WifiBusy, Wait-RadioReady, Wait-SsidSeen,                #
# Invoke-RadioCycleIfNeeded, Test-ConnectOutcome, Register-Connect*,             #
# Get-SimTargetSsid, Add-WifiProfileXml, Remove-*WifiProfile*.                   #
# ============================================================================ #
$version = '0.01'

. 'C:\Scripts\network_common.ps1'

# New-WpaPskProfileXml — build the WPA2-PSK WLAN profile XML for netsh. Same
# WPA2PSK / AES / passPhrase shape simulation.ps1 already uses (kept identical so
# behavior matches); duplicated here so this file stands alone as a connect path.
function New-WpaPskProfileXml {
    param([string]$Ssid, [string]$Password)
    return @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>$Ssid</name>
    <SSIDConfig><SSID><name>$Ssid</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>manual</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>WPA2PSK</authentication>
            <encryption>AES</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
        <sharedKey>
            <keyType>passPhrase</keyType>
            <protected>false</protected>
            <keyMaterial>$Password</keyMaterial>
        </sharedKey>
    </security></MSM>
</WLANProfile>
"@
}

# Connect-WifiPsk — the primary PSK wifi associate.
#   -WaitTime  : seconds to wait for association after netsh connect (the silent-
#                AP backstop; we return the instant it associates, not the whole
#                window). Default 180.
#   -Reset     : force a radio cycle now (the explicit "reset the adapter" site).
#   -ScanCap   : max seconds to wait for the SSID to appear in the scan cache.
#                0 (default) -> ramp up from ReconnectFails via Get-ScanCap.
#   -Track     : $true (default) = adjust the ReconnectFails ramp on success/
#                failure. $false = don't (the fail-sim / flap paths, whose
#                expected failures must not pollute the genuine-reconnect ramp).
function Connect-WifiPsk {
    param(
        [int]$WaitTime = 180,
        [switch]$Reset,
        [int]$ScanCap = 0,
        [bool]$Track = $true
    )

    # An 802.1X (enterprise) SSID is flagged by ssid == "1X" in the SSID matrix —
    # route it to the 1X path in connect_1x.ps1. Every other SSID is PSK below.
    if ($script:ssid -eq '1X') {
        Connect-Wifi1x -WaitTime $WaitTime -Reset:$Reset
        return
    }

    $targetSsid = Get-SimTargetSsid
    if ([string]::IsNullOrWhiteSpace($targetSsid)) {
        Write-SimLog '  [connect] target SSID is empty — nothing to connect to'
        return $false
    }

    $adapter = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) {
        Write-SimLog '  [connect] no wifi adapter found'
        return $false
    }

    # ---- Radio cycle (only as a LAST resort) --------------------------------
    # Cycle only on -Reset OR after RadioCycleAfter (5) consecutive tracked
    # failures; early retries just ride the scan-wait ramp with the cache warm.
    Invoke-RadioCycleIfNeeded -Reset:$Reset -Track:$Track

    # ---- Wait for the SSID to appear in the scan cache ----------------------
    # Radio ready != scan complete. Cap ramps +5s/failure up to 60s, reset on
    # success (Get-ScanCap); an explicit -ScanCap overrides.
    $cap = $ScanCap
    if ($cap -le 0) { $cap = Get-ScanCap }
    Wait-SsidSeen -Ssid $targetSsid -CapSeconds $cap | Out-Null

    # ---- Connect ------------------------------------------------------------
    # Rebuild + add the profile, then `netsh wlan connect`, then poll for the
    # ACTIVATED (associated) state up to -WaitTime (Test-ConnectOutcome — the
    # Windows analogue of the bash background-nmcli + wait-on-PID trick).
    $xml = New-WpaPskProfileXml -Ssid $targetSsid -Password $script:ssidpw
    $profilePath = Add-WifiProfileXml -ProfileName $targetSsid -Xml $xml
    netsh wlan connect name="$targetSsid" interface="$adapter" 2>&1 | Out-Null
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue

    if (Test-ConnectOutcome -Ssid $targetSsid -CapSeconds $WaitTime) {
        if ($Track) { Register-ConnectSuccess }
        Write-SimLog "  [connect] WiFi connected to $targetSsid"
        return $true
    }
    if ($Track) { Register-ConnectFailure }
    Write-SimLog "  [connect] WiFi failed to connect to $targetSsid"
    return $false
}

# Connect-WifiPskFail — fast wrong-PSK loop for the ssidpw_fail sim.
#   -CapSeconds : association backstop in seconds (default 5).
# The normal Connect-WifiPsk cycles the radio + scan-waits, capping the wrong-
# password loop far too slow to fire the "WPA Passphrase Incorrect" insight. To
# trigger it >=10x/min we need <=6s/attempt. The AP records the failed WPA 4-way
# handshake within ~1-2s of the association request, so a SHORT cap still
# registers the event. We drop the radio cycle + scan-wait, DELETE the saved
# profile first (each attempt is then a distinct AP event), re-add it, connect,
# and cap the association wait at -CapSeconds. Does NOT touch the ReconnectFails
# ramp. PSK-only here — 1X wrong-password uses Connect-Wifi1xFail. Mirrors bash
# connect_wifi_fail.
function Connect-WifiPskFail {
    param([int]$CapSeconds = 5)

    $targetSsid = Get-SimTargetSsid
    if ([string]::IsNullOrWhiteSpace($targetSsid)) { return }

    $adapter = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) { return }

    # Delete the saved profile so each attempt is a fresh association / distinct
    # AP event (the real "WPA Passphrase Incorrect" trigger).
    Remove-WifiProfileByName -ProfileName $targetSsid

    $xml = New-WpaPskProfileXml -Ssid $targetSsid -Password $script:ssidpw
    $profilePath = Add-WifiProfileXml -ProfileName $targetSsid -Xml $xml
    netsh wlan connect name="$targetSsid" interface="$adapter" 2>&1 | Out-Null
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue

    # Short backstop: the AP rejects the bad PSK within ~1-2s; we just wait out
    # the cap (we never expect success here) so the loop sustains its rate.
    Test-ConnectOutcome -Ssid $targetSsid -CapSeconds $CapSeconds | Out-Null
}
