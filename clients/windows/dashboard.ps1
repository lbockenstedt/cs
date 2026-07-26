. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

$version = '0.01'
$logPath = 'C:\Scripts\sim.log'
$refreshRate = 5
$scriptRoot = 'C:\Scripts'
$excludeScripts = @('dashboard.ps1','startup.ps1','simulation.ps1','ini-parser.ps1','sys_mon.ps1','common.ps1','network_common.ps1')

function Get-SimulationId {
    # Bucket via crc32(hostname) % 10 (common.ps1 Get-SimBucket) — MUST match the
    # Linux client + the spoke (was SHA256%10, which diverged cross-platform).
    $bucketNum = Get-SimBucket
    $username = Get-SimUsername
    $userSimId = get_value $username 'simulation_id'
    if (-not [string]::IsNullOrWhiteSpace($userSimId)) { return $userSimId }
    return "s$bucketNum"
}

function Apply-Override {
    param([string]$Section, [string]$Name, [hashtable]$Config)

    $overrideValue = get_value $Section $Name
    if (-not [string]::IsNullOrWhiteSpace([string]$overrideValue)) {
        $Config[$Name] = $overrideValue
    }
}

function Get-ConnectedSsid {
    $output = netsh wlan show interfaces 2>$null
    foreach ($line in $output) {
        if ($line -match '^\s*SSID\s*:\s*(.+)$' -and $line -notmatch 'BSSID') {
            $ssid = $matches[1].Trim()
            if ($ssid) {
                return $ssid
            }
        }
    }
    return $null
}

function Get-WifiStatus {
    $ssid = Get-ConnectedSsid
    if ($ssid) {
        return "CONNECTED ($ssid)"
    }
    return 'DISCONNECTED'
}

function Get-GatewayStatus {
    $gateway = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
        Sort-Object RouteMetric |
        Select-Object -First 1 -ExpandProperty NextHop)

    if (-not $gateway) {
        return 'NOT FOUND'
    }

    if (Test-Connection -ComputerName $gateway -Count 1 -Quiet -ErrorAction SilentlyContinue) {
        return "ONLINE ($gateway)"
    }

    return "OFFLINE ($gateway)"
}

function Get-RunningScriptRows {
    $scriptFiles = @(Get-ChildItem -LiteralPath $scriptRoot -Filter '*.ps1' -ErrorAction SilentlyContinue |
        Where-Object { $excludeScripts -notcontains $_.Name } |
        Sort-Object Name)

    foreach ($file in $scriptFiles) {
        $processPattern = '*' + $file.Name + '*'
        $process = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like $processPattern } |
            Select-Object -First 1

        if ($process) {
            $creation = [System.Management.ManagementDateTimeConverter]::ToDateTime($process.CreationDate)
            $runtime = New-TimeSpan -Start $creation -End (Get-Date)
            [pscustomobject]@{
                STATUS  = 'RUNNING'
                SCRIPT  = $file.Name
                PID     = $process.ProcessId
                RUNTIME = ('{0:00}:{1:00}:{2:00}' -f $runtime.Hours, $runtime.Minutes, $runtime.Seconds)
            }
        } else {
            [pscustomobject]@{
                STATUS  = 'STOPPED'
                SCRIPT  = $file.Name
                PID     = '-'
                RUNTIME = '-'
            }
        }
    }
}

while ($true) {
    $global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
    $username = ($env:COMPUTERNAME -split '-')[0]
    $simulationId = Get-SimulationId

    $config = @{
        kill_switch      = get_value 'simulation' 'kill_switch'
        rapid_update     = get_value 'simulation' 'rapid_update'
        sim_load         = get_value 'simulation' 'sim_load'
        public_repo      = get_value 'simulation' 'public_repo'
        repo_location    = get_value 'simulation' 'repo_location'
        vh_server        = get_value 'simulation' 'vh_server'
        site_based_ssid  = get_value 'simulation' 'site_based_ssid'
        iperf_bw         = get_value 'simulation' 'iperf_bw'
        auth_fail        = get_value 'simulation' 'auth_fail'
        ssidpw_fail      = get_value 'simulation' 'ssidpw_fail'
        allow_offline    = get_value 'simulation' 'allow_offline'
        wsite            = get_value $simulationId 'wsite'
        sim_phy          = get_value $simulationId 'sim_phy'
        ssid             = get_value $simulationId 'ssid'
        ssidpw           = get_value $simulationId 'ssidpw'
        dhcp_fail        = get_value $simulationId 'dhcp_fail'
        dns_fail         = get_value $simulationId 'dns_fail'
        assoc_fail       = get_value $simulationId 'assoc_fail'
        port_flap        = get_value $simulationId 'port_flap'
        ping_test        = get_value $simulationId 'ping_test'
        download         = get_value $simulationId 'download'
        iperf            = get_value $simulationId 'iperf'
        www_traffic      = get_value $simulationId 'www_traffic'
    }

    foreach ($key in @('kill_switch','sim_load','public_repo','repo_location','vh_server','site_based_ssid','iperf_bw','wsite','sim_phy','ssid','ssidpw','dhcp_fail','dns_fail','assoc_fail','port_flap','ping_test','download','iperf','www_traffic','ssidpw_fail','auth_fail')) {
        Apply-Override -Section $username -Name $key -Config $config
    }

    $wifiAdapter = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'wireless|wlan|wi-fi' -or $_.InterfaceDescription -match 'wireless|wlan|wi-fi|802\.11' } |
        Select-Object -First 1 -ExpandProperty Name

    Clear-Host
    Write-Host '            SIMULATION DASHBOARD (LIVE)              '
    Write-Host "Time:        $(Get-Date)"
    Write-Host "Hostname:    $($env:COMPUTERNAME)"
    Write-Host "WiFi Status: $(Get-WifiStatus)"
    Write-Host "Gateway:     $(Get-GatewayStatus)"
    Write-Host '--------------------------------------------------'
    Write-Host 'Simulation Details:'
    Write-Host "Simulation Load: $($config.sim_load)"
    Write-Host "Site: $($config.wsite) || Site Based SSID: $($config.site_based_ssid)"
    Write-Host "Phy: $($config.sim_phy)"
    Write-Host "VH Server: $($config.vh_server)"
    Write-Host "Adapter: $(if ($wifiAdapter) { $wifiAdapter } else { 'N/A' })"

    foreach ($label in @(
        @{ Name = 'Kill Switch'; Value = $config.kill_switch },
        @{ Name = 'DHCP Fail'; Value = $config.dhcp_fail },
        @{ Name = 'DNS Fail'; Value = $config.dns_fail },
        @{ Name = 'WWW Traffic'; Value = $config.www_traffic },
        @{ Name = 'iPerf'; Value = $config.iperf },
        @{ Name = 'Download'; Value = $config.download },
        @{ Name = 'Port Flap'; Value = $config.port_flap },
        @{ Name = 'Incorrect SSID PW'; Value = $config.ssidpw_fail }
    )) {
        if ($label.Value -eq 'on') {
            Write-Host "$($label.Name): on"
        }
    }

    Write-Host '--------------------------------------------------'
    Get-RunningScriptRows | Format-Table -AutoSize | Out-String | Write-Host
    Write-Host '--------------------------------------------------'
    Write-Host 'Last Log Entries:'
    if (Test-Path -LiteralPath $logPath) {
        Get-Content -LiteralPath $logPath -Tail 10
    }
    Write-Host '--------------------------------------------------'
    Start-Sleep -Seconds $refreshRate
}
