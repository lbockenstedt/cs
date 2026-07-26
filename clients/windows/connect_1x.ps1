# ============================================================================ #
# connect_1x.ps1 — 802.1X (WPA2-Enterprise) wifi connect functions.             #
#                                                                                #
# PowerShell 5.1 (Desktop) port of clients/linux/connect_1x.sh — a NEW           #
# capability on Windows (the previous Windows client was PSK-only). Owns:        #
#   Connect-Wifi1x       — the normal, genuine 1X associate.                     #
#   Connect-Wifi1xFail   — the fast wrong-password 1X loop for ssidpw_fail.      #
#   New-Peap1xProfileXml — builds the WPA2-Enterprise PEAP-MSCHAPv2 profile XML. #
#   Set-Peap1xUserCredentials — headless credential injection (see note below).  #
#                                                                                #
# netsh's `wlan connect` alone cannot store PEAP user credentials, so 802.1X     #
# needs an explicit WLAN profile with an <MSM><security><authEncryption> +       #
# <OneX><EAPConfig> (EapHostConfig) block. The profile is rebuilt every run so   #
# identity / EAP method / SSID always re-apply, mirroring the bash rebuild.      #
#                                                                                #
# Dot-source AFTER network_common.ps1 (state/helpers + ini-parser + common).     #
# Reads simulation config via get_value + Get-SimUsername:                       #
#   ssid, wsite, site_based_ssid   — the SSID + site prefix                      #
#   dot1x_eap                      — 'peap' (default) or 'tls' (EAP-TLS, TODO)   #
#   dot1x_password                 — PEAP password (per-user override honored),  #
#                                    falling back to the shared $script:ssidpw   #
#   identity                       — Get-SimUsername (hostname prefix)           #
# ============================================================================ #
$version = '0.01'

. 'C:\Scripts\network_common.ps1'

# ---------------------------------------------------------------------------- #
# Credential + method resolution.                                              #
# ---------------------------------------------------------------------------- #
# Get-Dot1xEap — 'peap' (default) or 'tls'. Mirrors bash ${dot1x_eap:-peap}.
function Get-Dot1xEap {
    $eap = get_value 'simulation' 'dot1x_eap'
    if ([string]::IsNullOrWhiteSpace($eap)) { return 'peap' }
    return $eap.Trim().ToLower()
}
# Get-Dot1xPassword — the PEAP password: [username] override wins, else the
# [simulation] dot1x_password, else the shared bucket ssidpw. Mirrors bash
# ${dot1x_password:-$ssidpw}.
function Get-Dot1xPassword {
    $pw = Apply-SimOverride 'dot1x_password' (get_value 'simulation' 'dot1x_password')
    if ([string]::IsNullOrWhiteSpace($pw)) { $pw = $script:ssidpw }
    return $pw
}

# ---------------------------------------------------------------------------- #
# Profile XML.                                                                  #
# ---------------------------------------------------------------------------- #
# New-Peap1xProfileXml — WPA2-Enterprise (AES) PEAP-MSCHAPv2 profile. Server-cert
# validation is DISABLED (lab: DisableUserPromptForServerValidation=true, empty
# ServerNames) so the client never blocks on an unknown RADIUS cert — matching
# the bash "802-1x.system-ca-certs no / no phase2 CA" lab posture. EAP type 25 =
# PEAP, inner type 26 = MSCHAPv2. $Identity is embedded so a machine with cached
# creds can reconnect; the actual password is applied out-of-band by
# Set-Peap1xUserCredentials (netsh has no verb for EAP user data).
function New-Peap1xProfileXml {
    param([string]$Ssid, [string]$Identity)
    return @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>$Ssid</name>
    <SSIDConfig><SSID><name>$Ssid</name></SSID></SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM><security>
        <authEncryption>
            <authentication>WPA2</authentication>
            <encryption>AES</encryption>
            <useOneX>true</useOneX>
        </authEncryption>
        <PMKCacheMode>enabled</PMKCacheMode>
        <OneX xmlns="http://www.microsoft.com/networking/OneX/v1">
            <authMode>user</authMode>
            <EAPConfig>
                <EapHostConfig xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
                    <EapMethod>
                        <Type xmlns="http://www.microsoft.com/provisioning/EapCommon">25</Type>
                        <VendorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorId>
                        <VendorType xmlns="http://www.microsoft.com/provisioning/EapCommon">0</VendorType>
                        <AuthorId xmlns="http://www.microsoft.com/provisioning/EapCommon">0</AuthorId>
                    </EapMethod>
                    <Config xmlns="http://www.microsoft.com/provisioning/EapHostConfig">
                        <Eap xmlns="http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1">
                            <Type>25</Type>
                            <EapType xmlns="http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV1">
                                <ServerValidation>
                                    <DisableUserPromptForServerValidation>true</DisableUserPromptForServerValidation>
                                    <ServerNames></ServerNames>
                                </ServerValidation>
                                <FastReconnect>true</FastReconnect>
                                <InnerEapOptional>false</InnerEapOptional>
                                <Eap xmlns="http://www.microsoft.com/provisioning/BaseEapConnectionPropertiesV1">
                                    <Type>26</Type>
                                    <EapType xmlns="http://www.microsoft.com/provisioning/MsChapV2ConnectionPropertiesV1">
                                        <UseWinLogonCredentials>false</UseWinLogonCredentials>
                                    </EapType>
                                </Eap>
                                <EnableQuarantineChecks>false</EnableQuarantineChecks>
                                <RequireCryptoBinding>false</RequireCryptoBinding>
                                <PeapExtensions>
                                    <PerformServerValidation xmlns="http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV2">false</PerformServerValidation>
                                    <AcceptServerName xmlns="http://www.microsoft.com/provisioning/MsPeapConnectionPropertiesV2">false</AcceptServerName>
                                </PeapExtensions>
                            </EapType>
                        </Eap>
                    </Config>
                </EapHostConfig>
            </EAPConfig>
        </OneX>
    </security></MSM>
</WLANProfile>
"@
}

# Set-Peap1xUserCredentials — inject the PEAP MSCHAPv2 username/password into an
# already-added profile so association is HEADLESS (no Windows credential popup).
#
# NOTE — this is the ONE WLAN step netsh cannot do: netsh exposes no verb for EAP
# *user data*, so we call the WlanApi WlanSetProfileEapXmlUserData directly via a
# small P/Invoke. Everything else (add profile, connect, disconnect, scan) stays
# on netsh per the client convention. Wrapped in try/catch: if the interop fails
# on a given box we log and fall through to the plain connect (a lab RADIUS with
# cached creds may still associate) rather than aborting the sim.
function Set-Peap1xUserCredentials {
    param([string]$ProfileName, [string]$Username, [string]$Password)
    $userDataXml = @"
<EapHostUserCredentials xmlns="http://www.microsoft.com/provisioning/EapHostUserCredentials"
    xmlns:eapCommon="http://www.microsoft.com/provisioning/EapCommon"
    xmlns:baseEap="http://www.microsoft.com/provisioning/BaseEapMethodUserCredentials">
    <EapMethod>
        <eapCommon:Type>25</eapCommon:Type>
        <eapCommon:AuthorId>0</eapCommon:AuthorId>
    </EapMethod>
    <Credentials xmlns:baseEap="http://www.microsoft.com/provisioning/BaseEapUserPropertiesV1"
        xmlns:MsPeap="http://www.microsoft.com/provisioning/MsPeapUserPropertiesV1"
        xmlns:MsChapV2="http://www.microsoft.com/provisioning/MsChapV2UserPropertiesV1">
        <baseEap:Eap>
            <baseEap:Type>25</baseEap:Type>
            <MsPeap:EapType>
                <MsPeap:RoutingIdentity>$Username</MsPeap:RoutingIdentity>
                <baseEap:Eap>
                    <baseEap:Type>26</baseEap:Type>
                    <MsChapV2:EapType>
                        <MsChapV2:Username>$Username</MsChapV2:Username>
                        <MsChapV2:Password>$Password</MsChapV2:Password>
                    </MsChapV2:EapType>
                </baseEap:Eap>
            </MsPeap:EapType>
        </baseEap:Eap>
    </Credentials>
</EapHostUserCredentials>
"@
    try {
        if (-not ([System.Management.Automation.PSTypeName]'LmWlan.Native').Type) {
            Add-Type -Namespace 'LmWlan' -Name 'Native' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("wlanapi.dll")]
public static extern uint WlanOpenHandle(uint dwClientVersion, System.IntPtr pReserved, out uint pdwNegotiatedVersion, out System.IntPtr phClientHandle);
[System.Runtime.InteropServices.DllImport("wlanapi.dll")]
public static extern uint WlanCloseHandle(System.IntPtr hClientHandle, System.IntPtr pReserved);
[System.Runtime.InteropServices.DllImport("wlanapi.dll")]
public static extern uint WlanEnumInterfaces(System.IntPtr hClientHandle, System.IntPtr pReserved, out System.IntPtr ppInterfaceList);
[System.Runtime.InteropServices.DllImport("wlanapi.dll")]
public static extern void WlanFreeMemory(System.IntPtr pMemory);
[System.Runtime.InteropServices.DllImport("wlanapi.dll", CharSet=System.Runtime.InteropServices.CharSet.Unicode)]
public static extern uint WlanSetProfileEapXmlUserData(System.IntPtr hClientHandle, ref System.Guid pInterfaceGuid, string strProfileName, uint dwFlags, string strEapXmlUserData, System.IntPtr pReserved);
'@ -ErrorAction Stop | Out-Null
        }
        [uint32]$neg = 0
        [System.IntPtr]$h = [System.IntPtr]::Zero
        if ([LmWlan.Native]::WlanOpenHandle(2, [System.IntPtr]::Zero, [ref]$neg, [ref]$h) -ne 0) {
            Write-SimLog '  [1x] WlanOpenHandle failed — skipping credential injection'
            return $false
        }
        try {
            [System.IntPtr]$list = [System.IntPtr]::Zero
            if ([LmWlan.Native]::WlanEnumInterfaces($h, [System.IntPtr]::Zero, [ref]$list) -ne 0) {
                Write-SimLog '  [1x] WlanEnumInterfaces failed — skipping credential injection'
                return $false
            }
            try {
                # WLAN_INTERFACE_INFO_LIST: dwNumberOfItems (4), dwIndex (4), then
                # WLAN_INTERFACE_INFO[0] where the first field is the GUID.
                $count = [System.Runtime.InteropServices.Marshal]::ReadInt32($list, 0)
                if ($count -lt 1) { Write-SimLog '  [1x] no WLAN interfaces'; return $false }
                $guidPtr = [System.IntPtr]::Add($list, 8)
                $guidBytes = New-Object byte[] 16
                [System.Runtime.InteropServices.Marshal]::Copy($guidPtr, $guidBytes, 0, 16)
                $guid = New-Object System.Guid (,$guidBytes)
                $rc = [LmWlan.Native]::WlanSetProfileEapXmlUserData($h, [ref]$guid, $ProfileName, 0, $userDataXml, [System.IntPtr]::Zero)
                if ($rc -ne 0) {
                    Write-SimLog "  [1x] WlanSetProfileEapXmlUserData rc=$rc — proceeding without cached creds"
                    return $false
                }
                Write-SimLog "  [1x] PEAP credentials injected for '$ProfileName' (identity=$Username)"
                return $true
            } finally {
                [LmWlan.Native]::WlanFreeMemory($list) | Out-Null
            }
        } finally {
            [LmWlan.Native]::WlanCloseHandle($h, [System.IntPtr]::Zero) | Out-Null
        }
    } catch {
        Write-SimLog "  [1x] credential injection error: $($_.Exception.Message) — proceeding without cached creds"
        return $false
    }
}

# Build-1xProfile — delete-then-add the 1X profile for $TargetSsid and inject the
# PEAP user credentials. Returns $true on a usable profile, $false to skip the
# connect. EAP-TLS is a TODO stub (see below). Mirrors bash
# _connect_1x_build_profile (which deletes + re-adds every run).
function Build-1xProfile {
    param([string]$TargetSsid)
    $eap = Get-Dot1xEap

    Remove-WifiProfileByName -ProfileName $TargetSsid

    if ($eap -eq 'tls') {
        # ---- EAP-TLS (cert-based, Cloud NAC) — TODO ------------------------
        # The Linux path (connect_1x.sh + cloud_nac_onboard.py) points nmcli at
        # PEM files: dot1x_client_cert / dot1x_private_key / dot1x_ca_cert.
        # Windows EAP-TLS instead requires the client cert+key imported into the
        # LocalMachine\My cert store and referenced by THUMBPRINT inside the
        # profile's <EapType> (SmartCardOrOtherCertificate) — netsh cannot consume
        # raw PEM. That onboarding (import PFX -> thumbprint -> profile) is the
        # out-of-reach piece and is NOT implemented here. Fail closed so we never
        # silently associate with the wrong method.
        Write-SimLog '  [1x] EAP-TLS requested but not implemented on Windows (TODO: import client PFX to cert store + reference thumbprint). Skipping.'
        return $false
    }

    # ---- PEAP-MSCHAPv2 (username/password) — the default ---------------------
    $identity = Get-SimUsername
    $password = Get-Dot1xPassword
    $adapter  = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) {
        Write-SimLog '  [1x] no wifi adapter found'
        return $false
    }
    $xml = New-Peap1xProfileXml -Ssid $TargetSsid -Identity $identity
    $profilePath = Add-WifiProfileXml -ProfileName $TargetSsid -Xml $xml
    Remove-Item -LiteralPath $profilePath -Force -ErrorAction SilentlyContinue
    Set-Peap1xUserCredentials -ProfileName $TargetSsid -Username $identity -Password $password | Out-Null
    return $true
}

# Connect-Wifi1x — the genuine 1X associate. Tracks the ReconnectFails ramp.
#   -WaitTime : association backstop in seconds (default 180).
#   -Reset    : force a radio cycle now.
# Mirrors bash connect_1x -> _connect_1x_normal.
function Connect-Wifi1x {
    param([int]$WaitTime = 180, [switch]$Reset)

    $targetSsid = Get-SimTargetSsid
    if ([string]::IsNullOrWhiteSpace($targetSsid)) {
        Write-SimLog '  [1x] target SSID is empty'
        return $false
    }
    $adapter = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) {
        Write-SimLog '  [1x] no wifi adapter found'
        return $false
    }

    # ---- Radio cycle (only as a LAST resort) --------------------------------
    Invoke-RadioCycleIfNeeded -Reset:$Reset -Track:$true

    # ---- Wait for the SSID, then (re)build the profile ----------------------
    Wait-SsidSeen -Ssid $targetSsid -CapSeconds (Get-ScanCap) | Out-Null
    if (-not (Build-1xProfile -TargetSsid $targetSsid)) { return $false }

    # ---- Connect ------------------------------------------------------------
    netsh wlan connect name="$targetSsid" interface="$adapter" 2>&1 | Out-Null
    if (Test-ConnectOutcome -Ssid $targetSsid -CapSeconds $WaitTime) {
        Register-ConnectSuccess
        Write-SimLog "  [1x] WiFi connected to $targetSsid"
        return $true
    }
    Register-ConnectFailure
    Write-SimLog "  [1x] WiFi failed to connect to $targetSsid"
    return $false
}

# Connect-Wifi1xFail — fast wrong-password 1X loop for ssidpw_fail.
#   -CapSeconds : association backstop in seconds (default 5).
# No radio cycle / scan-wait / ramp tracking — the profile delete+rebuild forces a
# fresh association each attempt (a distinct RADIUS event), and a short cap still
# registers the failed EAP within ~1-2s so the loop sustains >=10 auth-failures/
# min. The bad password (set by the caller via dot1x_password before this runs) is
# what makes it fail fast on the deauth. Mirrors bash connect_1x_fail ->
# _connect_1x_fast.
function Connect-Wifi1xFail {
    param([int]$CapSeconds = 5)

    $targetSsid = Get-SimTargetSsid
    if ([string]::IsNullOrWhiteSpace($targetSsid)) { return }
    $adapter = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapter)) { return }

    # Delete + rebuild so each attempt is a fresh association.
    if (-not (Build-1xProfile -TargetSsid $targetSsid)) { return }

    netsh wlan connect name="$targetSsid" interface="$adapter" 2>&1 | Out-Null
    Test-ConnectOutcome -Ssid $targetSsid -CapSeconds $CapSeconds | Out-Null
}
