. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
. 'C:\Scripts\network_common.ps1'
. 'C:\Scripts\connect_1x.ps1'
. 'C:\Scripts\connect_open.ps1'
. 'C:\Scripts\connect_psk.ps1'

$version = '0.01'
$scriptRoot = 'C:\Scripts'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-simulation.log'
$tempDir = 'C:\Temp'
$killSwitchPath = 'C:\Scripts\kill_switch.txt'
$vhCachePath = 'C:\Scripts\vhcached.txt'
$script:statusErrors = @()
$script:dot1xTlsUnsupported = $false

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

function Add-StatusError {
    param([string]$Message)
    if ([string]::IsNullOrWhiteSpace($Message)) { return }
    if (-not $script:statusErrors) { $script:statusErrors = @() }
    if ($script:statusErrors -notcontains $Message) {
        $script:statusErrors += $Message
    }
}

function Get-AdapterInventory {
    $defaultIfIndex = $null
    try {
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            Sort-Object RouteMetric, InterfaceMetric |
            Select-Object -First 1
        if ($route) { $defaultIfIndex = $route.InterfaceIndex }
    } catch {
    }

    $items = @()
    foreach ($adapter in (Get-NetAdapter -ErrorAction SilentlyContinue)) {
        $name = [string]$adapter.Name
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        $desc = [string]$adapter.InterfaceDescription
        $media = 'other'
        if ($name -match 'wi-fi|wireless|wlan' -or $desc -match 'wi-fi|wireless|wlan|802\.11') {
            $media = 'wireless'
        } elseif ($name -match 'ethernet|eth' -or $desc -match 'ethernet|gigabit|usb.*lan|usb.*ethernet|realtek|intel') {
            $media = 'wired'
        }
        $isDefault = $false
        if ($defaultIfIndex -ne $null -and $adapter.ifIndex -eq $defaultIfIndex) {
            $isDefault = $true
        }
        $items += @{
            name = $name
            mac = [string]$adapter.MacAddress
            media_type = $media
            is_default_route = $isDefault
        }
    }
    return $items
}

function Get-TargetSsid {
    if ([string]::IsNullOrWhiteSpace($script:ssid)) { return '' }
    if ($script:site_based_ssid -eq 'on' -and -not [string]::IsNullOrWhiteSpace($script:wsite)) {
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
        foreach ($name in @('dns_fail','dns_latency','collab','iperf','download','www_traffic','www_discover','rogue_dns','ping_test','ssidpw_fail','auth_fail','mac_auth_fail','dhcp_fail','client_offline')) {
            if ((Get-Variable -Name $name -Scope Script -ValueOnly -ErrorAction SilentlyContinue) -eq 'on') {
                $activeSimulations += $name
            }
        }
        $clientStatus = 'ok'
        if ($script:dot1xTlsUnsupported) { $clientStatus = 'unsupported' }

        $payload = @{
            hostname = $hostname
            simulation_id = $script:simulation_id
            platform = 'windows'
            iteration = $Iteration
            connected_ssid = $connectedSsid
            gateway_reachable = [bool]$script:gateway_reachable
            vh_connected = $false
            status = $clientStatus
            errors = @($script:statusErrors)
            adapters = @(Get-AdapterInventory)
            active_simulations = $activeSimulations
            config = @{
                sim_phy = [string]$script:sim_phy
                dot1x_eap = [string]$script:dot1x_eap
                kill_switch = [string]$script:kill_switch
                dns_fail = [string]$script:dns_fail
                dns_latency = [string]$script:dns_latency
                collab = [string]$script:collab
                client_offline = [string]$script:client_offline
                iperf = [string]$script:iperf
                www_traffic = [string]$script:www_traffic
                rogue_dns = [string]$script:rogue_dns
                www_discover = [string]$script:www_discover
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
    # (connect_1x.ps1) or open (connect_open.ps1) per Get-SimSsidAuth. Mirrors the
    # Linux simulation.sh sourcing network_common.sh + connect_psk.sh +
    # connect_1x.sh + connect_open.sh.
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

# The command that dot-sources a sim; Run-Simulation launches exactly this.
function Get-SimLaunchCommand {
    param([string]$ScriptName)

    return ". $(Convert-ToSingleQuotedLiteral (Join-Path $scriptRoot $ScriptName))"
}

# A sim process is the powershell.exe Run-Simulation starts, dot-sourcing the
# script by its full path. An editor, PowerShell ISE or another PowerShell
# command that merely mentions the file is never counted as running (or
# force-killed), like Linux's anchored rogue_dns match.
function Test-SimProcess {
    param($Process, [string]$ScriptName)

    return $Process.Name -eq 'powershell.exe' -and ([string]$Process.CommandLine).Contains("; $(Get-SimLaunchCommand $ScriptName)")
}

# 'running', 'stopped' or 'unknown'. A failed (or empty) process query is
# unknown, never "not running": callers that would launch or restore must wait.
function Get-ScriptRunState {
    param([string]$ScriptName)

    try { $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop) } catch { return 'unknown' }
    if (-not $procs.Count) { return 'unknown' }
    if ($procs | Where-Object { Test-SimProcess $_ $ScriptName } | Select-Object -First 1) { return 'running' }
    return 'stopped'
}

# Generic sims keep the old rule (and Linux run_simulation's): a failed query
# counts as not running. rogue_dns callers use Get-ScriptRunState instead, so an
# unknown state never launches, browses over or restores rogue_dns.
function Test-ScriptRunning {
    param([string]$ScriptName)

    return (Get-ScriptRunState -ScriptName $ScriptName) -eq 'running'
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
        (Get-SimLaunchCommand $ScriptName)
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

$rogueDnsScript = 'C:\Scripts\rogue_dns.ps1'
$rogueDnsStatePath = 'C:\Scripts\rogue_dns.state'
$rogueDnsStopPath = 'C:\Scripts\rogue_dns.stop'
$rogueDnsStopGrace = 10
$simConfPath = 'C:\Scripts\simulation.conf'
$script:simConfStamp = $null
$script:rogueDnsRetry = $false

# Rogue DNS lifecycle, run EVERY cycle outside the rogue_dns flag gate (Windows
# manual addressing also survives reboot). Off or kill switch: ask a running
# instance to stop via the stop file (its finally restores DHCP) and force-kill
# it after a short grace. Then, with no instance left, phase=static means a
# restore is still owed (reboot, crash, force-kill): run a restore-only pass.
# A stop that can't be confirmed (still running, or a failed process query) sets
# $script:rogueDnsRetry, and Update-RogueDnsGate retries it every iteration.
function Sync-RogueDns {
    # Like the main loop, anything but kill_switch=off counts as kill-switched.
    $stop = $script:rogue_dns -ne 'on' -or $script:kill_switch -ne 'off'
    # Write the marker before process discovery: a failed query must not keep a
    # live instance in its static phase. It is removed only on a definite 'stopped'.
    if ($stop -and -not (Test-Path -LiteralPath $rogueDnsStopPath)) {
        Set-Content -LiteralPath $rogueDnsStopPath -Value 'stop' -ErrorAction SilentlyContinue
    }
    $run = Get-ScriptRunState -ScriptName 'rogue_dns.ps1'
    if ($stop -and $run -eq 'running') {
        Write-SimLog 'rogue_dns: disabled or kill switch on; stopping running instance'
        for ($i = 0; $i -lt $rogueDnsStopGrace -and $run -ne 'stopped'; $i++) {
            Start-Sleep -Seconds 1
            $run = Get-ScriptRunState -ScriptName 'rogue_dns.ps1'
        }
        if ($run -eq 'running') {
            $stuck = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { Test-SimProcess $_ 'rogue_dns.ps1' })
            Write-SimLog "rogue_dns: did not exit in ${rogueDnsStopGrace}s; force-killing"
            $stuck | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            if ($stuck.Count) { Wait-Process -Id ($stuck | ForEach-Object { $_.ProcessId }) -Timeout 5 -ErrorAction SilentlyContinue }
            $run = Get-ScriptRunState -ScriptName 'rogue_dns.ps1'
        }
        # Only a definite 'stopped' ends the stop request. Unknown (failed query)
        # or still running: keep the marker so the instance still sees it.
        if ($run -ne 'stopped') { Write-SimLog "rogue_dns: instance state $run after stop request; keeping stop marker, retrying" }
    }
    $script:rogueDnsRetry = $stop -and $run -ne 'stopped'
    if ($run -eq 'unknown') { Write-SimLog 'rogue_dns: process check failed; retrying'; return }
    if ($run -ne 'stopped') { return }
    Remove-Item -LiteralPath $rogueDnsStopPath -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $rogueDnsScript) -or -not (Test-Path -LiteralPath $rogueDnsStatePath)) { return }
    # Same validity rule as rogue_dns.ps1. Unreadable/corrupt (including a static
    # state missing what restore needs) means unknown: restore-only handles it.
    $phase = $null
    try {
        $state = Get-Content -LiteralPath $rogueDnsStatePath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($state.phase -in @('static','dhcp') -and ($state.start -is [int] -or $state.start -is [long]) -and
            ($state.phase -eq 'dhcp' -or ($state.adapter -and $state.address -and $state.gateway))) { $phase = $state.phase }
    } catch { }
    if ($phase -eq 'dhcp') { return }
    if ($phase -eq 'static') {
        Write-SimLog 'rogue_dns: static state with no running instance; restoring DHCP'
    } else {
        Write-SimLog 'rogue_dns: rogue_dns state unreadable; restore-only pass'
    }
    & $rogueDnsScript -RestoreOnly
}

# Per-iteration twin of Linux rogue_dns_gate: the outer cycle (and its
# Sync-RogueDns) runs only every ~25 min. While rogue_dns is on, re-read the
# global kill switch file every iteration, and simulation.conf only when its
# write time moved; on kill switch or rogue_dns=off, stop it now. With
# rogue_dns off it costs nothing unless a stop is still owed ($script:rogueDnsRetry).
function Update-RogueDnsGate {
    if ($script:rogue_dns -ne 'on') {
        if ($script:rogueDnsRetry) { Sync-RogueDns }
        return
    }
    $kill = $null
    $flag = $null
    $stamp = (Get-Item -LiteralPath $simConfPath -ErrorAction SilentlyContinue).LastWriteTimeUtc
    if ($stamp -ne $script:simConfStamp) {
        $script:simConfStamp = $stamp
        $ini = Parse-IniFile $simConfPath
        $get = { param($section, $key) if ($ini.ContainsKey($section)) { [string]$ini[$section][$key] } }
        $kill = & $get $script:username 'kill_switch'
        if (-not $kill) { $kill = & $get 'simulation' 'kill_switch' }
        # Parsed, so never $null: a blank kill switch is kill-switched, as in the main loop.
        $kill = "$kill"
        # Compare with the cycle-start sources: an unrelated conf write must not
        # turn an ambient-rolled rogue_dns off.
        $user = [string](& $get $script:username 'rogue_dns')
        $bucket = [string](& $get $script:simulation_id 'rogue_dns')
        if ($user -ne [string]$script:rogueDnsUser) {
            # A removed override falls back to what the cycle would have run without it.
            if ($user) { $flag = $user }
            elseif ($script:random_pool -eq 'on') { $flag = [string]$script:rogueDnsBase }
            else { $flag = $bucket }
            if (-not $flag) { $flag = 'off' }
        } elseif (-not $user -and $script:random_pool -ne 'on' -and $bucket -ne [string]$script:rogueDnsBucket) {
            $flag = $bucket; if (-not $flag) { $flag = 'off' }
        }
    }
    if (Test-Path -LiteralPath $killSwitchPath) {
        $globalKill = ([string](Get-Content -LiteralPath $killSwitchPath -ErrorAction SilentlyContinue | Select-Object -First 1)).Trim()
        if ($globalKill) { $kill = $globalKill }
    }
    if (($null -ne $kill -and $kill -ne 'off') -or ($flag -and $flag -ne 'on')) {
        Write-SimLog 'rogue_dns: kill switch on or rogue_dns turned off mid-cycle; stopping'
        $script:rogue_dns = 'off'
        Sync-RogueDns
    }
}

# www_traffic/fallback browse dispatch. While rogue_dns is on and not definitely
# stopped (static phase, or a failed process query), never browse: the static
# phase does its own browsing. Clients without rogue_dns behave exactly as before.
function Test-WwwBrowseDue {
    if ($script:rogue_dns -eq 'on' -and (Get-ScriptRunState -ScriptName 'rogue_dns.ps1') -ne 'stopped') { return $false }
    return ($script:www_traffic -eq 'on' -or ($script:rogue_dns -eq 'on' -and $script:rogueDnsBrowseReady)) -and
        -not (Test-ScriptRunning -ScriptName 'www_traffic.ps1')
}

# Close headless browsers, sparing the process tree of a running rogue_dns.ps1:
# that browser is its static-phase traffic and it stops it itself.
function Stop-SimFirefox {
    # A failed or empty query is unknown: a rogue_dns browser can't be told apart
    # from ours, whatever the rogue_dns flag says, so skip this cleanup.
    try { $procs = @(Get-CimInstance Win32_Process -ErrorAction Stop) } catch { return }
    if (-not $procs.Count) { return }
    $keep = @{}
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $procs | Where-Object { Test-SimProcess $_ 'rogue_dns.ps1' } | ForEach-Object { $queue.Enqueue([int]$_.ProcessId) }
    while ($queue.Count) {
        $id = $queue.Dequeue()
        if ($keep.ContainsKey($id)) { continue }
        $keep[$id] = $true
        $procs | Where-Object { [int]$_.ParentProcessId -eq $id } | ForEach-Object { $queue.Enqueue([int]$_.ProcessId) }
    }
    Get-Process -Name firefox -ErrorAction SilentlyContinue |
        Where-Object { -not $keep.ContainsKey([int]$_.Id) } |
        Stop-Process -ErrorAction SilentlyContinue
}

while ($true) {
    # Snapshot before parsing: Update-RogueDnsGate re-parses only if it moves.
    $script:simConfStamp = (Get-Item -LiteralPath $simConfPath -ErrorAction SilentlyContinue).LastWriteTimeUtc
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
    $script:dot1x_eap = get_value 'simulation' 'dot1x_eap'
    if ([string]$script:dot1x_eap -ne 'tls') {
        $script:dot1xTlsUnsupported = $false
        $script:statusErrors = @($script:statusErrors | Where-Object { $_ -notmatch '^dot1x_eap=tls unsupported' })
    }
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
    # psk/mpsk/1x/open — empty falls back to the SSID-name rule (Get-SimSsidAuth).
    $script:ssid_auth = get_value $script:simulation_id 'ssid_auth'
    $script:dhcp_fail = get_value $script:simulation_id 'dhcp_fail'
    $script:dns_fail = get_value $script:simulation_id 'dns_fail'
    $script:assoc_fail = get_value $script:simulation_id 'assoc_fail'
    $script:port_flap = get_value $script:simulation_id 'port_flap'
    $script:ping_test = get_value $script:simulation_id 'ping_test'
    $script:download = get_value $script:simulation_id 'download'
    $script:iperf = get_value $script:simulation_id 'iperf'
    $script:www_traffic = get_value $script:simulation_id 'www_traffic'
    $script:rogue_dns = get_value $script:simulation_id 'rogue_dns'
    if (-not $script:rogue_dns) { $script:rogue_dns = 'off' }
    $script:rogueDnsBrowseReady = $true
    $script:www_discover = get_value $script:simulation_id 'www_discover'
    if (-not $script:www_discover) { $script:www_discover = 'off' }
    $script:dns_latency = get_value $script:simulation_id 'dns_latency'
    $script:collab = get_value $script:simulation_id 'collab'
    $script:client_offline = get_value $script:simulation_id 'client_offline'
    if (-not $script:client_offline) { $script:client_offline = 'off' }

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
        foreach ($sim in @('dhcp_fail','dns_fail','dns_latency','collab','assoc_fail','port_flap','ssidpw_fail','auth_fail','mac_auth_fail','ping_test','download','iperf','www_traffic','www_discover','rogue_dns')) {
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

    # rogue_dns from the bucket or ambient roll, before any [username] override.
    $script:rogueDnsBase = $script:rogue_dns
    Apply-UserOverrides -Section $script:username -Names @(
        'kill_switch','sim_load','public_repo','repo_location','site_based_ssid','iperf_bw',
        'wsite','sim_phy','ssid','ssidpw','ssid_auth','dhcp_fail','dns_fail','dns_latency','collab','assoc_fail','port_flap','ping_test',
        'download','iperf','www_traffic','www_discover','ssidpw_fail','auth_fail','mac_auth_fail','mac_auth_fail_mac','smb_address','ping_address',
        'rogue_dns','rogue_dns_server','rogue_dns_dhcp_interval','rogue_dns_duration','rogue_dns_browse_interval',
        'dns_bad_ip_1','dns_bad_ip_2','dns_bad_ip_3',
        'dns_bad_record_1','dns_bad_record_2','dns_bad_record_3','iperf_server',
        'collab_app','collab_bw','collab_time','collab_server','dot1x_password','dot1x_eap','web_server','client_offline'
    )
    $script:rogueDnsUser = [string](get_value $script:username 'rogue_dns')
    $script:rogueDnsBucket = [string](get_value $script:simulation_id 'rogue_dns')

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
    Sync-RogueDns

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
            Update-RogueDnsGate
            if ((($script:ssidpw_fail -eq 'on') -or ($script:auth_fail -eq 'on') -or ($script:mac_auth_fail -eq 'on')) -and $null -ne $wladapter) {
                $correctSsidpw = get_value $script:simulation_id 'ssidpw'
                if ($script:ssidpw_fail -eq 'on') {
                    # Fast wrong-password loop (<=~6s/attempt) so the "WPA Passphrase
                    # Incorrect" insight fires >=10x/min — the full-connect loop
                    # (radio cycle + scan-wait each) was far too slow to trip it.
                    # PSK vs 1X per Get-SimSsidAuth. Mirrors Linux connect_wifi_fail.
                    # An open SSID has no password to get wrong, so it is skipped.
                    if ((Get-SimSsidAuth) -eq 'open') {
                        Write-SimLog 'SSID Incorrect Password does not apply to an open SSID (no password) — skipping'
                    } else {
                        $script:ssidpw = "${correctSsidpw}_fail"
                        for ($i = 1; $i -le 100; $i++) {
                            Write-SimDebug "Running SSID Incorrect Password — iteration $i of 100"
                            if ((Get-SimSsidAuth) -eq '1x') { [void](Connect-Wifi1xFail) } else { [void](Connect-WifiPskFail) }
                        }
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

                if (Test-WwwBrowseDue) {
                    Run-Simulation -ScriptName 'www_traffic.ps1'
                    Write-SimDebug 'Running WWW Traffic Simulation'
                    $script:www_traffic = 'off'
                    $script:rogueDnsBrowseReady = $false
                }
                # Launch only on a definite 'stopped'; a failed check waits.
                if ($script:rogue_dns -eq 'on') {
                    $rogueDnsRun = Get-ScriptRunState -ScriptName 'rogue_dns.ps1'
                    if ($rogueDnsRun -eq 'stopped') {
                        # Definitely stopped and still wanted: any marker is stale.
                        Remove-Item -LiteralPath $rogueDnsStopPath -Force -ErrorAction SilentlyContinue
                        Run-Simulation -ScriptName 'rogue_dns.ps1'
                    }
                    elseif ($rogueDnsRun -eq 'unknown') { Write-SimLog 'rogue_dns: process check failed; not launching this iteration' }
                }
                if ($script:www_discover -eq 'on' -and -not (Test-ScriptRunning -ScriptName 'www_discover.ps1')) {
                    Run-Simulation -ScriptName 'www_discover.ps1'
                    Write-SimDebug 'Running WWW Discover Simulation'
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
                    Stop-SimFirefox
                    $script:www_traffic = 'on'
                    # Unless definitely running: Test-WwwBrowseDue re-checks at use time.
                    if ($script:rogue_dns -eq 'on' -and (Get-ScriptRunState -ScriptName 'rogue_dns.ps1') -ne 'running') {
                        $script:rogueDnsBrowseReady = $true
                    }
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
        # Sliced so a rogue_dns stop still owed is retried while kill-switched.
        for ($ks = 0; $ks -lt 30; $ks++) {
            if ($script:rogueDnsRetry) { Sync-RogueDns }
            Start-Sleep -Seconds 10
        }
    }

    if ($restartCycle) {
        continue
    }

    Write-SimDebug 'Closing Firefox'
    Stop-SimFirefox
    Write-SimDebug 'Running Updates'
    Start-Process powershell -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', 'C:\Scripts\apt_update.ps1') -WindowStyle Hidden | Out-Null

    # Foreground lifecycle window, deliberately not a Run-Simulation traffic job.
    if ($script:client_offline -eq 'on' -and $script:kill_switch -ne 'on' -and -not (Test-ScriptRunning -ScriptName 'client_offline.ps1')) {
        & 'C:\Scripts\client_offline.ps1' $script:sim_phy
    }

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
