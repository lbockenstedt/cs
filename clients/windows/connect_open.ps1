# ============================================================================ #
# connect_open.ps1 — open (no encryption, no password) wifi connect functions.  #
#                                                                                #
# PowerShell 5.1 (Desktop) port of clients/linux/connect_open.sh.                #
# Connect-WifiPsk (connect_psk.ps1) routes here when the client's SSID is open   #
# (ssid_auth=open — the SSID matrix cell's Auth). Owns:                          #
#   Connect-WifiOpen    — the normal, genuine open associate.                    #
#   New-OpenProfileXml  — builds the open / no-encryption WLAN profile XML.      #
#                                                                                #
# Same flow as Connect-WifiPsk: radio cycle only as a last resort, wait for the  #
# SSID in the scan cache, rebuild the profile, netsh connect, poll for the       #
# connected state. The only difference is that there is no password.             #
#                                                                                #
# Dot-source AFTER network_common.ps1 (state/helpers + ini-parser + common).     #
# Depends on simulation.ps1 having set (each loop, before these are CALLED):     #
#   $script:ssid, $script:wsite, $script:site_based_ssid                         #
# ============================================================================ #
$version = '0.01'

. 'C:\Scripts\network_common.ps1'

# New-OpenProfileXml — build an open (authentication=open, encryption=none) WLAN
# profile XML for netsh. Same shape as New-WpaPskProfileXml minus the sharedKey.
function New-OpenProfileXml {
    param([string]$Ssid)
    return @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>$Ssid</name>
    <SSIDConfig><SSID><name>$Ssid</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>manual</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>open</authentication>
            <encryption>none</encryption>
            <useOneX>false</useOneX>
        </authEncryption>
    </security></MSM>
</WLANProfile>
"@
}

# Connect-WifiOpen — the open wifi associate. Same parameters as Connect-WifiPsk:
#   -WaitTime : seconds to wait for association after netsh connect. Default 180.
#   -Reset    : force a radio cycle now.
#   -ScanCap  : max seconds to wait for the SSID; 0 (default) -> Get-ScanCap ramp.
#   -Track    : $true (default) = adjust the ReconnectFails ramp; $false = don't.
function Connect-WifiOpen {
    param(
        [int]$WaitTime = 180,
        [switch]$Reset,
        [int]$ScanCap = 0,
        [bool]$Track = $true
    )

    $targetSsid = Get-SimTargetSsid
    if ([string]::IsNullOrWhiteSpace($targetSsid)) {
        Write-SimLog '  [open] target SSID is empty — nothing to connect to'
        return $false
    }
    $adapter = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) {
        Write-SimLog '  [open] no wifi adapter found'
        return $false
    }

    Invoke-RadioCycleIfNeeded -Reset:$Reset -Track:$Track

    $cap = $ScanCap
    if ($cap -le 0) { $cap = Get-ScanCap }
    Wait-SsidSeen -Ssid $targetSsid -CapSeconds $cap | Out-Null

    # Re-adding the profile replaces any saved same-name profile (e.g. a WPA2 one
    # left over from when this cell was PSK), so the connect is always open.
    $xml = New-OpenProfileXml -Ssid $targetSsid
    $profilePath = Add-WifiProfileXml -ProfileName $targetSsid -Xml $xml
    netsh wlan connect name="$targetSsid" interface="$adapter" 2>&1 | Out-Null
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue

    if (Test-ConnectOutcome -Ssid $targetSsid -CapSeconds $WaitTime) {
        if ($Track) { Register-ConnectSuccess }
        Write-SimLog "  [open] WiFi connected to $targetSsid"
        return $true
    }
    if ($Track) { Register-ConnectFailure }
    Write-SimLog "  [open] WiFi failed to connect to $targetSsid"
    return $false
}
