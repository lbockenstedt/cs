. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
. 'C:\Scripts\network_common.ps1'
. 'C:\Scripts\connect_1x.ps1'
. 'C:\Scripts\connect_psk.ps1'

$version = '0.01'
$scriptRoot = 'C:\Scripts'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-simulation.log'
$tempDir = 'C:\Temp'
$killSwitchPath = 'C:\Scripts\kill_switch.txt'
$vhCachePath = 'C:\Scripts\vhcached.txt'

"Simulation Script Version $version" | Tee-Object -FilePath $debugPath
"Simulation Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

if (-not (Test-Path -LiteralPath $tempDir)) {
    New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
}

function Write-SimLog {
    param([string]$Message)
    $Message | Tee-Object -FilePath $logPath -Append | Out-Null
}

function Write-SimDebug {
    param([string]$Message)
    $Message | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}

function Get-WifiAdapter {
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match 'wi-fi|wireless|wlan' -or
            $_.InterfaceDescription -match 'wi-fi|wireless|wlan|802\.11'
        } |
        Sort-Object @{ Expression = { if ($_.Status -eq 'Up') { 0 } elseif ($_.Status -eq 'Disconnected') { 1 } else { 2 } } }, Name |
        Select-Object -First 1
}

function Get-EthernetAdapter {
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match 'ethernet|eth' -or $_.InterfaceDescription -match 'ethernet|gigabit'
        } |
        Sort-Object @{ Expression = { if ($_.Status -eq 'Up') { 0 } elseif ($_.Status -eq 'Disconnected') { 1 } else { 2 } } }, Name |
        Select-Object -First 1
}

function Get-ConnectedSsid {
    $output = netsh wlan show interfaces 2>$null
    foreach ($line in $output) {
        if ($line -match '^\s*SSID\s*:\s*(.+)$' -and $line -notmatch 'BSSID') {
            $ssidValue = $matches[1].Trim()
            if (-not [string]::IsNullOrWhiteSpace($ssidValue)) {
                return $ssidValue
            }
        }
    }
    return $null
}

function Get-TargetSsid {
    if ($script:site_based_ssid -eq 'on') {
        return "$($script:wsite)-$($script:ssid)"
    }
    return $script:ssid
}

function Send-Status {
    param([int]$Iteration = 0)

    if ($script:web_server -ne 'on' -or [string]::IsNullOrWhiteSpace($script:server_url)) {
        return
    }

    try {
        $connectedSsid = $null
        foreach ($line in ((netsh wlan show interfaces 2>$null) -match '^\s*SSID\s*:')) {
            if ($line -match '^\s*SSID\s*:\s*(.+)$' -and $line -notmatch 'BSSID') {
                $connectedSsid = $matches[1].Trim()
                break
            }
        }

        $activeSimulations = @()
        foreach ($name in @('dns_fail','dns_latency','collab','iperf','download','www_traffic','ping_test','ssidpw_fail','auth_fail','mac_auth_fail','dhcp_fail')) {
            if ((Get-Variable -Name $name -Scope Script -ValueOnly -ErrorAction SilentlyContinue) -eq 'on') {
                $activeSimulations += $name
            }
        }

        $payload = @{
            hostname = $hostname
            simulation_id = $script:simulation_id
            platform = 'windows'
            iteration = $Iteration
            connected_ssid = $connectedSsid
            gateway_reachable = [bool]$script:gateway_reachable
            vh_connected = $false
            active_simulations = $activeSimulations
            config = @{
                sim_phy = [string]$script:sim_phy
                kill_switch = [string]$script:kill_switch
                dns_fail = [string]$script:dns_fail
                dns_latency = [string]$script:dns_latency
                collab = [string]$script:collab
                iperf = [string]$script:iperf
                www_traffic = [string]$script:www_traffic
                download = [string]$script:download
                ping_test = [string]$script:ping_test
                ssidpw_fail = [string]$script:ssidpw_fail
                auth_fail = [string]$script:auth_fail
                mac_auth_fail = [string]$script:mac_auth_fail
                dhcp_fail = [string]$script:dhcp_fail
            }
        } | ConvertTo-Json -Depth 4 -Compress

        Invoke-WebRequest -Uri ($script:server_url.TrimEnd('/') + '/api/status') -Method Post -ContentType 'application/json' -Body $payload -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop | Out-Null
    } catch {
    }
}

function New-WifiProfileXml {
    param([string]$ssid, [string]$password)
    return @"
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>$ssid</name>
    <SSIDConfig><SSID><name>$ssid</name></SSID></SSIDConfig>
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
            <keyMaterial>$password</keyMaterial>
        </sharedKey>
    </security></MSM>
</WLANProfile>
"@
}

function Convert-ToSingleQuotedLiteral {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) {
        $Value = ''
    }
    return "'" + ($Value -replace "'", "''") + "'"
}

function Test-SsidVisible {
    param([string]$TargetSsid)

    if ([string]::IsNullOrWhiteSpace($TargetSsid)) {
        return $false
    }

    $pattern = '^\s*SSID\s+\d+\s*:\s*' + [regex]::Escape($TargetSsid) + '$'
    $networks = netsh wlan show networks mode=bssid 2>$null
    return ($networks | Select-String -Pattern $pattern -Quiet)
}

function Wait-ForSsid {
    param([string]$TargetSsid)

    $timeout = 60
    $interval = 3
    $elapsed = 0
    Write-SimDebug "Scanning for SSID: $TargetSsid"

    while ($elapsed -lt $timeout) {
        if (Test-SsidVisible -TargetSsid $TargetSsid) {
            Write-SimDebug "SSID found: $TargetSsid"
            return $true
        }
        Start-Sleep -Seconds $interval
        $elapsed += $interval
    }

    Write-SimDebug "SSID not found after $timeout seconds, attempting rescan"
    netsh wlan show networks mode=bssid 2>$null | Out-Null
    Start-Sleep -Seconds 2
    if (Test-SsidVisible -TargetSsid $TargetSsid) {
        Write-SimDebug "SSID found after rescan: $TargetSsid"
        return $true
    }

    Write-SimDebug 'SSID still not found, resetting WiFi adapter'
    $adapter = Get-WifiAdapter
    if ($adapter) {
        Disable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Start-Sleep -Seconds 3
        Enable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        Start-Sleep -Seconds 2
    }

    $elapsed = 0
    while ($elapsed -lt $timeout) {
        netsh wlan show networks mode=bssid 2>$null | Out-Null
        if (Test-SsidVisible -TargetSsid $TargetSsid) {
            Write-SimDebug "SSID found after WiFi reset: $TargetSsid"
            return $true
        }
        Start-Sleep -Seconds $interval
        $elapsed += $interval
    }

    Write-SimDebug "ERROR: SSID '$TargetSsid' not found after rescan and WiFi reset"
    return $false
}

function Remove-WifiProfile {
    param([string]$ProfileName)

    if ([string]::IsNullOrWhiteSpace($ProfileName)) {
        return
    }

    netsh wlan delete profile name="$ProfileName" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
}

function Connect-Wifi {
    # Delegate to the shared connect library. Connect-WifiPsk (connect_psk.ps1)
    # does the WPA2-PSK associate + reconnect-fail ramp AND dispatches to 802.1X
    # (connect_1x.ps1) when the bucket SSID is "1X". Mirrors the Linux
    # simulation.sh sourcing network_common.sh + connect_psk.sh + connect_1x.sh.
    return [bool](Connect-WifiPsk)
}

function Manage-Connection {
    param(
        [string]$Action,
        [int]$WaitTime
    )

    $adapter = Get-WifiAdapter
    if (-not $adapter) {
        return $false
    }

    Enable-NetAdapter -Name $adapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Write-SimDebug 'Ensuring WiFi Adapter is ON'
    Start-Sleep -Seconds 2

    $targetSsid = Get-TargetSsid
    if ($Action -eq 'up') {
        $result = Connect-Wifi
        Start-Sleep -Seconds $WaitTime
        return $result
    }

    if ($Action -eq 'down') {
        Write-SimDebug "Attempting to down connection: $targetSsid"
        netsh wlan disconnect interface="$($adapter.Name)" 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        Start-Sleep -Seconds $WaitTime
        return $true
    }

    return $false
}

function Set-WifiMacSpoof {
    # Windows has no NetworkManager-style per-CONNECTION MAC override (unlike
    # Linux's nmcli 802-11-wireless.cloned-mac-address, which is pinned on the
    # connection PROFILE and survives up/down cycles) — the spoof here is a
    # NIC-level property instead: the driver's "NetworkAddress" advanced
    # setting (the same value a manual driver-key edit under
    # SOFTWARE\...\Class\{4d36e972-e325-11ce-bfc1-08002be10318}\<NNNN> would
    # set), applied via Set-NetAdapterAdvancedProperty, then a disable/enable
    # cycle to force the NIC to re-init with it.
    #
    # UNVERIFIED ON REAL HARDWARE — there is no Windows box in this dev loop to
    # confirm against (same "hardware-untested" honesty flag the T3 IoT port
    # carries elsewhere in this codebase). Most modern WLAN miniport drivers
    # (Intel/Realtek/Qualcomm) expose NetworkAddress and honor it for
    # association, but some WLAN driver/firmware stacks silently IGNORE it
    # (Windows' own per-network "random hardware addresses" privacy feature is
    # a DIFFERENT mechanism entirely and does not go through this property) —
    # always log the ACTUAL adapter MAC read back after the cycle so a spoof
    # that silently didn't land is diagnosable from the log alone, not a guess.
    param([string]$TargetMac)

    $adapterName = Get-WlanAdapter
    if ([string]::IsNullOrWhiteSpace($adapterName)) { return $null }

    $macNoSep = ($TargetMac -replace '[:\-]', '').ToUpperInvariant()
    $setOk = $true
    $setErr = ''
    try {
        Set-NetAdapterAdvancedProperty -Name $adapterName -RegistryKeyword 'NetworkAddress' -RegistryValue $macNoSep -NoRestart -ErrorAction Stop
    } catch {
        $setOk = $false
        $setErr = $_.Exception.Message
    }
    Disable-NetAdapter -Name $adapterName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 2
    Enable-NetAdapter -Name $adapterName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 3
    $actualMac = (Get-NetAdapter -Name $adapterName -ErrorAction SilentlyContinue).MacAddress
    Write-SimDebug "  [mac_auth_fail] NetworkAddress set_ok=$setOk set_err='$setErr' target_mac=$TargetMac actual_iface_mac=$actualMac"
    return $adapterName
}

function Clear-WifiMacSpoof {
    # Remove the NetworkAddress override and cycle the adapter back to its
    # permanent MAC, so the maintenance Connect-Wifi call below (and any
    # subsequent normal reconnect) is NOT left running on the spoofed identity.
    param([string]$AdapterName)
    if ([string]::IsNullOrWhiteSpace($AdapterName)) { return }
    try {
        Remove-NetAdapterAdvancedProperty -Name $AdapterName -RegistryKeyword 'NetworkAddress' -ErrorAction SilentlyContinue
    } catch {}
    Disable-NetAdapter -Name $AdapterName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 2
    Enable-NetAdapter -Name $AdapterName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    Start-Sleep -Seconds 3
    $actualMac = (Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue).MacAddress
    Write-SimDebug "  [mac_auth_fail] cleared NetworkAddress override on '$AdapterName' before maintenance reconnect (actual_iface_mac=$actualMac)"
}

function Test-ScriptRunning {
    param([string]$ScriptName)

    return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*${ScriptName}*" } |
        Select-Object -First 1)
}

function Run-Simulation {
    param([string]$ScriptName)

    $scriptPath = Join-Path $scriptRoot $ScriptName
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        return
    }

    $command = @(
        "`$global:rn = $($global:rn)",
        "`$global:rn_iperf_port = $($global:rn_iperf_port)",
        "`$global:rn_iperf_time = $($global:rn_iperf_time)",
        "`$global:rn_ping_size = $($global:rn_ping_size)",
        "`$global:ping_address = $(Convert-ToSingleQuotedLiteral $script:ping_address)",
        "`$global:iperf_server = $(Convert-ToSingleQuotedLiteral $script:iperf_server)",
        ". $(Convert-ToSingleQuotedLiteral $scriptPath)"
    ) -join '; '

    Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $command) -WindowStyle Hidden | Out-Null
}

function Get-DefaultGateway {
    return (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
        Sort-Object RouteMetric |
        Select-Object -First 1 -ExpandProperty NextHop)
}

function Test-GatewayReachable {
    param([string]$Gateway)

    if ([string]::IsNullOrWhiteSpace($Gateway)) {
        return $false
    }

    return (Test-Connection -ComputerName $Gateway -Count 2 -Quiet -ErrorAction SilentlyContinue)
}

function Reset-VHState {
    if (Get-Command 'vhclientx86_64.exe' -ErrorAction SilentlyContinue) {
        & 'vhclientx86_64.exe' -t 'STOP USING ALL LOCAL' 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
        & 'vhclientx86_64.exe' -t 'AUTO USE CLEAR ALL' 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
    }

    Remove-Item -LiteralPath $vhCachePath -Force -ErrorAction SilentlyContinue
}

function Apply-UserOverrides {
    param(
        [string]$Section,
        [string[]]$Names
    )

    foreach ($name in $Names) {
        $overrideValue = get_value $Section $name
        if (-not [string]::IsNullOrWhiteSpace([string]$overrideValue)) {
            Set-Variable -Name $name -Value $overrideValue -Scope Script
        }
    }
}

while ($true) {
    $global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'

    $script:username = Get-SimUsername
    $hostname = $env:COMPUTERNAME
    # Bucket via crc32(hostname) % 10 (common.ps1 Get-SimBucket) — MUST match the
    # Linux client (common.sh derive_bucket) AND the spoke's sim_config.bucket_for().
    # The old SHA256(host)%10 put the SAME host in a DIFFERENT bucket than
    # Linux/the spoke, so a host's Windows and Linux sims diverged. crc32 aligns them.
    $bucketNum = Get-SimBucket
    $script:simulation_id = "s$bucketNum"
    # Allow user-overrides.conf to pin a specific bucket.
    $userSimId = get_value $script:username 'simulation_id'
    if (-not [string]::IsNullOrWhiteSpace($userSimId)) { $script:simulation_id = $userSimId }

    $script:kill_switch = get_value 'simulation' 'kill_switch'
    $script:rapid_update = get_value 'simulation' 'rapid_update'
    $script:sim_load = get_value 'simulation' 'sim_load'
    $script:public_repo = get_value 'simulation' 'public_repo'
    $script:repo_location = get_value 'simulation' 'repo_location'
    $script:site_based_ssid = get_value 'simulation' 'site_based_ssid'
    $script:iperf_bw = get_value 'simulation' 'iperf_bw'
    $script:auth_fail = get_value 'simulation' 'auth_fail'
    $script:ssidpw_fail = get_value 'simulation' 'ssidpw_fail'
    # mac_auth_fail: same "connectivity-failure, inline in the connect loop" kind
    # as ssidpw_fail/auth_fail (see the loop below) — associates with a fixed,
    # PREDICTABLE spoofed MAC (mac_auth_fail_mac, [address]) so the operator can
    # pre-configure that exact MAC as a RADIUS/ClearPass MAC-Auth deny entry.
    $script:mac_auth_fail = get_value 'simulation' 'mac_auth_fail'
    $script:allow_offline = get_value 'simulation' 'allow_offline'
    $script:web_server = get_value 'simulation' 'web_server'
    $script:server_url = get_value 'server' 'server_url'

    $script:wsite = get_value $script:simulation_id 'wsite'
    $script:sim_phy = get_value $script:simulation_id 'sim_phy'
    $script:ssid = get_value $script:simulation_id 'ssid'
    $script:ssidpw = get_value $script:simulation_id 'ssidpw'
    $script:dhcp_fail = get_value $script:simulation_id 'dhcp_fail'
    $script:dns_fail = get_value $script:simulation_id 'dns_fail'
    $script:assoc_fail = get_value $script:simulation_id 'assoc_fail'
    $script:port_flap = get_value $script:simulation_id 'port_flap'
    $script:ping_test = get_value $script:simulation_id 'ping_test'
    $script:download = get_value $script:simulation_id 'download'
    $script:iperf = get_value $script:simulation_id 'iperf'
    $script:www_traffic = get_value $script:simulation_id 'www_traffic'
    $script:dns_latency = get_value $script:simulation_id 'dns_latency'
    $script:collab = get_value $script:simulation_id 'collab'

    # ------------------------------------------------------------
    # Ambient random pool (see clients/linux/simulation.sh for the full story).
    #
    # When the spoke tells us this client's site is in the random pool, its
    # behaviour is randomised: pick a random bucket (s0-s9) and take only the
    # randomizable sim flags from it. Every other sim - the failure/alert sims -
    # is forced OFF, because those only run when the engine harvests this client
    # (a harvest arrives as a [username] override applied below and WINS, so a
    # harvested client still runs its assigned sim). Connectivity
    # (wsite/ssid/ssidpw/sim_phy) is NOT touched.
    #
    # random_pool (on/off) and randomizable_sims (space separated list) are
    # delivered by the spoke in the [simulation] section of simulation.conf.
    # ------------------------------------------------------------
    $script:random_pool = get_value 'simulation' 'random_pool'
    $randomizableSims = get_value 'simulation' 'randomizable_sims'
    if ($script:random_pool -eq 'on' -and $randomizableSims) {
        $randomBucket = 's' + (Get-Random -Minimum 0 -Maximum 10)
        Write-SimLog "Ambient random pool: rolling behaviour from bucket $randomBucket"
        $randomizableList = $randomizableSims -split '\s+' | Where-Object { $_ }
        foreach ($sim in @('dhcp_fail','dns_fail','dns_latency','collab','assoc_fail','port_flap','ssidpw_fail','auth_fail','mac_auth_fail','ping_test','download','iperf','www_traffic')) {
            if ($randomizableList -contains $sim) {
                Set-Variable -Name $sim -Scope Script -Value (get_value $randomBucket $sim)
            } else {
                Set-Variable -Name $sim -Scope Script -Value 'off'
            }
        }
    }

    $script:smb_address = get_value 'address' 'smb_address'
    $script:ping_address = get_value 'address' 'ping_address'
    $script:dns_bad_ip_1 = get_value 'address' 'dns_bad_ip_1'
    $script:dns_bad_ip_2 = get_value 'address' 'dns_bad_ip_2'
    $script:dns_bad_ip_3 = get_value 'address' 'dns_bad_ip_3'
    $script:dns_bad_record_1 = get_value 'address' 'dns_bad_record_1'
    $script:dns_bad_record_2 = get_value 'address' 'dns_bad_record_2'
    $script:dns_bad_record_3 = get_value 'address' 'dns_bad_record_3'
    $script:iperf_server = get_value 'address' 'iperf_server'
    # mac_auth_fail_mac: the SHARED, predictable spoofed MAC every mac_auth_fail
    # client associates with (same value fleet-wide — a single known RADIUS/
    # ClearPass deny-list entry, matching how ssidpw_fail corrupts the SAME real
    # password by the same rule rather than deriving a per-client value).
    $script:mac_auth_fail_mac = get_value 'address' 'mac_auth_fail_mac'

    Apply-UserOverrides -Section $script:username -Names @(
        'kill_switch','sim_load','public_repo','repo_location','site_based_ssid','iperf_bw',
        'wsite','sim_phy','ssid','ssidpw','dhcp_fail','dns_fail','dns_latency','collab','assoc_fail','port_flap','ping_test',
        'download','iperf','www_traffic','ssidpw_fail','auth_fail','mac_auth_fail','mac_auth_fail_mac','smb_address','ping_address',
        'dns_bad_ip_1','dns_bad_ip_2','dns_bad_ip_3',
        'dns_bad_record_1','dns_bad_record_2','dns_bad_record_3','iperf_server',
        'collab_app','collab_bw','collab_time','collab_server','dot1x_password','web_server'
    )

    if (Test-Path -LiteralPath $killSwitchPath) {
        $globalKillRaw = Get-Content -LiteralPath $killSwitchPath -ErrorAction SilentlyContinue | Select-Object -First 1
        $globalKill = [string]$globalKillRaw
        if ($globalKill) {
            $globalKill = $globalKill.Trim()
        }
        if (-not [string]::IsNullOrWhiteSpace($globalKill)) {
            $script:kill_switch = $globalKill
        }
    }

    $global:rn = Get-Random -Minimum 1 -Maximum 61
    $global:rn_iperf_port = 5201 + (Get-Random -Minimum 0 -Maximum 10)
    $global:rn_iperf_time = Get-Random -Minimum 1 -Maximum 301
    $global:rn_ping_size = Get-Random -Minimum 1 -Maximum 65001
    $script:rn_offline_time = Get-Random -Minimum 1 -Maximum 14401
    $script:rn_sim_load = Get-Random -Minimum 1 -Maximum 100

    $wladapter = Get-WifiAdapter
    $eadapter = Get-EthernetAdapter
    if ($wladapter) {
        Write-SimDebug "WLAN Adapter name $($wladapter.Name)"
    }
    if ($eadapter) {
        Write-SimDebug "Wired Adapter name $($eadapter.Name)"
    }

    [void](Connect-Wifi)
    $gateway = Get-DefaultGateway
    $script:gateway_reachable = Test-GatewayReachable -Gateway $gateway
    Send-Status -Iteration 0

    Write-SimDebug 'Disabling unused interface'
    if ($script:sim_phy -eq 'ethernet' -and $wladapter) {
        Disable-NetAdapter -Name $wladapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }
    if ($script:sim_phy -eq 'wireless' -and $eadapter) {
        Disable-NetAdapter -Name $eadapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
    }

    $gateway = Get-DefaultGateway
    if ((Test-GatewayReachable -Gateway $gateway) -and $script:sim_phy -eq 'wireless' -and $wladapter) {
        Write-SimDebug 'Successful network connection - Pre-Simulation'
        Write-SimDebug 'In Pre-Simulation'
    } else {
        Write-SimDebug 'Network connection failed'
        Write-SimDebug 'In Pre-Simulation'
        Start-Sleep -Seconds 15
        $wladapter = Get-WifiAdapter
        [void](Connect-Wifi)
        Start-Sleep -Seconds 15
        $gateway = Get-DefaultGateway
    }

    if ([int]$script:sim_load -lt $script:rn_sim_load) {
        Write-SimDebug 'Simulation load under threshold'
        Write-SimDebug 'Skipping Simulations but staying associated'
        if ($script:ssidpw_fail -ne 'on' -and $wladapter) {
            [void](Manage-Connection -Action 'up' -WaitTime 180)
        }
        Start-Sleep -Seconds 5
    }

    Write-SimDebug "Kill Switch is $($script:kill_switch)"
    $restartCycle = $false

    if ($script:kill_switch -eq 'off') {
        for ($z = 1; $z -le 100; $z++) {
            # Rapid update — MUST fire every iteration REGARDLESS of which
            # simulation is running. At the top of the loop, before the
            # ssidpw_fail/auth_fail branch and the connectivity gate, so it runs
            # for every client. It used to sit at the bottom of the non-auth-fail
            # branch, where an auth_fail/ssidpw_fail client (other branch) or a
            # connectivity break skipped it entirely.
            if ($script:rapid_update -eq 'on') {
                . 'C:\Scripts\update.ps1'
            }
            $wladapter = Get-WifiAdapter
            $gateway = Get-DefaultGateway
            $script:gateway_reachable = Test-GatewayReachable -Gateway $gateway
            Send-Status -Iteration $z
            if ((($script:ssidpw_fail -eq 'on') -or ($script:auth_fail -eq 'on') -or ($script:mac_auth_fail -eq 'on')) -and $null -ne $wladapter) {
                $correctSsidpw = get_value $script:simulation_id 'ssidpw'
                if ($script:ssidpw_fail -eq 'on') {
                    # Fast wrong-password loop (<=~6s/attempt) so the "WPA Passphrase
                    # Incorrect" insight fires >=10x/min — the full-connect loop
                    # (radio cycle + scan-wait each) was far too slow to trip it.
                    # PSK vs 1X per the bucket SSID. Mirrors Linux connect_wifi_fail.
                    $script:ssidpw = "${correctSsidpw}_fail"
                    for ($i = 1; $i -le 100; $i++) {
                        Write-SimDebug "Running SSID Incorrect Password — iteration $i of 100"
                        if ($script:ssid -eq '1X') { [void](Connect-Wifi1xFail) } else { [void](Connect-WifiPskFail) }
                    }
                }
                if ($script:auth_fail -eq 'on') {
                    Write-SimDebug 'Running Auth Failure'
                    for ($i = 1; $i -le 100; $i++) {
                        Write-SimDebug 'Enable/Disable WLAN interface'
                        Write-SimDebug "Iteration $i of 100"
                        Remove-WifiProfile -ProfileName (Get-TargetSsid)
                        [void](Manage-Connection -Action 'up' -WaitTime 5)
                        Start-Sleep -Seconds 5
                        [void](Manage-Connection -Action 'down' -WaitTime 5)
                    }
                }
                if ($script:mac_auth_fail -eq 'on') {
                    Write-SimDebug "Running MAC Auth Failure (spoofed MAC deny-list test, target=$($script:mac_auth_fail_mac))"
                    $macAdapterName = Set-WifiMacSpoof -TargetMac $script:mac_auth_fail_mac
                    for ($i = 1; $i -le 100; $i++) {
                        Write-SimDebug "Enable/Disable WLAN interface (spoofed MAC deny-list test) — iteration $i of 100"
                        [void](Manage-Connection -Action 'up' -WaitTime 5)
                        Start-Sleep -Seconds 5
                        [void](Manage-Connection -Action 'down' -WaitTime 5)
                        $actualMac = if ($macAdapterName) { (Get-NetAdapter -Name $macAdapterName -ErrorAction SilentlyContinue).MacAddress } else { $null }
                        Write-SimDebug "  [mac_auth_fail] iteration $i up target_mac=$($script:mac_auth_fail_mac) actual_iface_mac=$actualMac"
                    }
                    # Clear the spoof BEFORE the maintenance Connect-Wifi below so
                    # it reconnects on the adapter's normal identity, not the
                    # deny-listed one.
                    Clear-WifiMacSpoof -AdapterName $macAdapterName
                }
                $script:ssidpw = $correctSsidpw
                [void](Connect-Wifi)
            } else {
                $gateway = Get-DefaultGateway
                if (Test-GatewayReachable -Gateway $gateway) {
                    Write-SimDebug 'Successful network connection'
                    Write-SimDebug 'In Simulation Loop'
                } else {
                    Write-SimDebug 'Network connection failed'
                    Write-SimDebug 'In Simulation Loop'
                    Write-SimDebug 'Attempting to reset adapter'
                    Start-Sleep -Seconds 15
                    $wladapter = Get-WifiAdapter
                    Remove-WifiProfile -ProfileName (Get-TargetSsid)
                    [void](Connect-Wifi)
                    if ($wladapter) {
                        Write-SimDebug "WLAN Adapter name $($wladapter.Name)"
                    }
                    Start-Sleep -Seconds 15
                    $gateway = Get-DefaultGateway
                    if (Test-GatewayReachable -Gateway $gateway) {
                        Write-SimDebug 'Successful network connection'
                        Write-SimDebug 'After Adapter Reset'
                    } else {
                        Write-SimDebug 'Connection failed multiple times'
                        Write-SimDebug 'Resetting configuration'
                        Write-SimDebug 'Purging VHConfig'
                        Reset-VHState
                        Remove-WifiProfile -ProfileName (Get-TargetSsid)
                        $restartCycle = $true
                        break
                    }
                }

                if ($script:www_traffic -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'www_traffic.ps1')) {
                    Run-Simulation -ScriptName 'www_traffic.ps1'
                    Write-SimDebug 'Running WWW Traffic Simulation'
                    $script:www_traffic = 'off'
                }
                if ($script:ping_test -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'ping_test.ps1')) {
                    Run-Simulation -ScriptName 'ping_test.ps1'
                    Write-SimDebug 'Running Ping Test Simulation'
                }
                if ($script:iperf -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'iperf.ps1')) {
                    Run-Simulation -ScriptName 'iperf.ps1'
                    Write-SimDebug 'Running iPerf Simulation'
                }
                if ($script:download -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'download.ps1')) {
                    Run-Simulation -ScriptName 'download.ps1'
                    Write-SimDebug 'Running Download Simulation'
                }
                if ($script:dns_fail -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'dns_fail.ps1')) {
                    Run-Simulation -ScriptName 'dns_fail.ps1'
                    Write-SimDebug 'Running DNS Simulation'
                }
                if ($script:dns_latency -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'dns_latency.ps1')) {
                    Run-Simulation -ScriptName 'dns_latency.ps1'
                    Write-SimDebug 'Running DNS Latency Simulation'
                }
                if ($script:dhcp_fail -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'dhcp_fail.ps1')) {
                    Run-Simulation -ScriptName 'dhcp_fail.ps1'
                    Write-SimDebug 'Running DHCP Fail Simulation'
                }
                if ($script:collab -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'collab.ps1')) {
                    Run-Simulation -ScriptName 'collab.ps1'
                    Write-SimDebug 'Running Collab Simulation'
                }

                Start-Sleep -Seconds 10
                if (($z % 10) -eq 0) {
                    Write-SimDebug 'Closing Firefox'
                    Stop-Process -Name firefox -ErrorAction SilentlyContinue
                    $script:www_traffic = 'on'
                }
                Write-SimDebug 'End of simulation'
                # (rapid_update now runs at the TOP of the loop so it fires for
                # every client regardless of simulation — see above.)
                Write-SimDebug 'Sleeping for 5 seconds'
                Write-SimDebug "Loop iteration $z of 100"
                Start-Sleep -Seconds 5
            }
        }
    } else {
        Write-SimDebug 'Kill switch enabled - sleeping for 5 minutes'
        # Poll for updates while kill-switched so the client can pick up a config
        # change that turns the kill switch back off.
        if ($script:rapid_update -eq 'on') {
            . 'C:\Scripts\update.ps1'
        }
        Start-Sleep -Seconds 300
    }

    if ($restartCycle) {
        continue
    }

    Write-SimDebug 'Closing Firefox'
    Stop-Process -Name firefox -ErrorAction SilentlyContinue
    Write-SimDebug 'Running Updates'
    Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'C:\Scripts\apt_update.ps1') -WindowStyle Hidden | Out-Null

    if ($script:allow_offline -in @('on', 'yes')) {
        Write-SimDebug 'Bringing all interfaces down'
        if ($wladapter) {
            Disable-NetAdapter -Name $wladapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        }
        if ($eadapter) {
            Disable-NetAdapter -Name $eadapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        }
        Write-SimDebug "Sleeping for $($script:rn_offline_time) seconds"
        Write-SimDebug '------------------------------'
        Start-Sleep -Seconds $script:rn_offline_time
        Write-SimDebug 'Bringing all interfaces online'
        if ($eadapter) {
            Enable-NetAdapter -Name $eadapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        }
        if ($wladapter) {
            Enable-NetAdapter -Name $wladapter.Name -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
        }
        Write-SimDebug '------------------------------'
    }
}
