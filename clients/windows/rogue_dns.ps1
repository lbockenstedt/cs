param([switch]$RestoreOnly)
$version = '0.01'
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'rogue_dns.ps1' $version
$statePath = 'C:\Scripts\rogue_dns.state'
# The orchestrator creates this to cancel a running static phase gracefully
# (Windows has no SIGTERM): the loop exits and the finally block restores DHCP.
$stopPath = 'C:\Scripts\rogue_dns.stop'

function Save-RogueDnsState {
    param($State)
    $State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath "$statePath.new" -Encoding UTF8 -ErrorAction Stop
    Move-Item -LiteralPath "$statePath.new" -Destination $statePath -Force -ErrorAction Stop
}
function Restore-RogueDnsDhcp {
    param($State)
    # Resolve the captured adapter identity, not a potentially recycled ifIndex.
    $adapter = Get-NetAdapter -ErrorAction Stop | Where-Object { [string]$_.InterfaceGuid -eq $State.adapter } | Select-Object -First 1
    if (-not $adapter) { throw 'Captured wireless adapter is unavailable' }
    $idx = $adapter.ifIndex
    # Remove only our manual address/routes. A second recovery is safe after a
    # partial restore; never remove a newly acquired DHCP lease.
    # Enumerate then filter: CIM's keyed query throws ObjectNotFound when a
    # previous recovery already removed the address. Real query errors still fail.
    Get-NetIPAddress -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -eq $idx -and $_.IPAddress -eq $State.address -and $_.PrefixOrigin -eq 'Manual' } |
        Remove-NetIPAddress -Confirm:$false -ErrorAction Stop
    Get-NetRoute -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -eq $State.gateway -and $_.Protocol -eq 'NetMgmt' } |
        Remove-NetRoute -Confirm:$false -ErrorAction Stop
    Set-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4 -Dhcp Enabled -ErrorAction Stop
    Set-DnsClientServerAddress -InterfaceIndex $idx -ResetServerAddresses -ErrorAction Stop
    Save-RogueDnsState @{ phase = 'dhcp'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
}

$settings = @{}
$overrides = Get-SimOverrides
foreach ($key in @('rogue_dns_server','rogue_dns_dhcp_interval','rogue_dns_duration','rogue_dns_browse_interval')) {
    $section = 'simulation'
    if ($key -eq 'rogue_dns_server') { $section = 'address' }
    $settings[$key] = get_value $section $key
    if ($overrides.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($overrides[$key])) { $settings[$key] = $overrides[$key] }
}
foreach ($entry in @{ rogue_dns_dhcp_interval = 1800; rogue_dns_duration = 3600; rogue_dns_browse_interval = 60 }.GetEnumerator()) {
    if ([string]$settings[$entry.Key] -notmatch '^[1-9][0-9]{0,7}$') { $settings[$entry.Key] = $entry.Value }
}
try {
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($state.phase -notin @('static','dhcp') -or -not ($state.start -is [int] -or $state.start -is [long])) { throw 'Invalid rogue_dns state' }
            # A static state must carry everything the restore needs, else it is corrupt.
            if ($state.phase -eq 'static' -and -not ($state.adapter -and $state.address -and $state.gateway)) { throw 'Incomplete rogue_dns state' }
        } catch {
            if (-not $RestoreOnly) { throw }
            # Corrupt state: we don't know what was applied. Act only on rogue_dns's
            # own fingerprint (Wi-Fi adapter with DHCP off, DNS = rogue_dns_server
            # only, one manual IPv4 address); anything else may be operator static
            # config, so leave it and just rewrite a valid dhcp state.
            $name = Get-WlanAdapter
            if (-not $name) { throw 'Unreadable rogue_dns state and no wireless adapter' }
            $adapter = Get-NetAdapter -Name $name -ErrorAction Stop
            $idx = $adapter.ifIndex
            $dhcp = [string](Get-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction Stop).Dhcp
            $dns = @((Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction Stop).ServerAddresses)
            $manual = @(Get-NetIPAddress -ErrorAction Stop |
                Where-Object { $_.InterfaceIndex -eq $idx -and $_.AddressFamily -eq 'IPv4' -and $_.PrefixOrigin -eq 'Manual' })
            if ($dhcp -eq 'Disabled' -and $dns.Count -eq 1 -and $dns[0] -eq [string]$settings.rogue_dns_server -and $manual.Count -eq 1) {
                Write-SimLog "rogue_dns: unreadable state; $name uses rogue_dns_server, restoring DHCP"
                $route = Get-NetRoute -ErrorAction Stop |
                    Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.Protocol -eq 'NetMgmt' } | Select-Object -First 1
                Restore-RogueDnsDhcp @{ adapter = [string]$adapter.InterfaceGuid; address = $manual[0].IPAddress; gateway = $route.NextHop }
            } else {
                Write-SimLog "rogue_dns: unreadable state; no rogue_dns settings on $name, leaving it"
                Save-RogueDnsState @{ phase = 'dhcp'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
            }
            return
        }
        if ($state.phase -eq 'static') {
            Restore-RogueDnsDhcp $state
            return
        }
    } else {
        if (-not $RestoreOnly) { Save-RogueDnsState @{ phase = 'dhcp'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } }
        return
    }
    if ($RestoreOnly) { return }
    if (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$state.start) -lt [int]$settings.rogue_dns_dhcp_interval) { return }
    $server = [string]$settings.rogue_dns_server
    $ip = $null
    if ($server -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -or -not [System.Net.IPAddress]::TryParse($server, [ref]$ip) -or
        $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $ip.GetAddressBytes()[0] -eq 0 -or $ip.GetAddressBytes()[0] -eq 127 -or $ip.GetAddressBytes()[0] -ge 224) {
        Write-SimLog 'rogue_dns: blank or invalid rogue_dns_server; skipping'
        return
    }
    $name = Get-WlanAdapter
    if (-not $name) { Write-SimLog 'rogue_dns: no wireless lease'; return }
    $adapter = Get-NetAdapter -Name $name -ErrorAction Stop
    $idx = $adapter.ifIndex
    $lease = Get-NetIPAddress -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -eq $idx -and $_.AddressFamily -eq 'IPv4' -and $_.PrefixOrigin -eq 'Dhcp' -and $_.IPAddress -notmatch '^169\.254\.' } | Select-Object -First 1
    $route = Get-NetRoute -ErrorAction Stop | Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' } | Sort-Object RouteMetric | Select-Object -First 1
    if (-not $lease -or -not $route) { Write-SimLog 'rogue_dns: no DHCP lease; skipping'; return }
    $state = @{ phase = 'static'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); adapter = [string]$adapter.InterfaceGuid
        address = $lease.IPAddress; prefix = $lease.PrefixLength; gateway = $route.NextHop
        dns = @((Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4).ServerAddresses) }
    # Persist before the first mutation so reboot/Stop-Process can recover.
    Save-RogueDnsState $state
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    try {
        Set-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
        Get-NetIPAddress -ErrorAction Stop |
            Where-Object { $_.InterfaceIndex -eq $idx -and $_.IPAddress -eq $state.address } | Remove-NetIPAddress -Confirm:$false -ErrorAction Stop
        Get-NetRoute -ErrorAction Stop |
            Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -eq $state.gateway } | Remove-NetRoute -Confirm:$false -ErrorAction Stop
        New-NetIPAddress -InterfaceIndex $idx -IPAddress $state.address -PrefixLength $state.prefix -DefaultGateway $state.gateway -ErrorAction Stop | Out-Null
        Set-DnsClientServerAddress -InterfaceIndex $idx -ServerAddresses $server -ErrorAction Stop
        $wwwfile = @(Get-Content -LiteralPath 'C:\Scripts\websites.txt' -ErrorAction SilentlyContinue | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $firefoxCommand = Get-Command firefox.exe -ErrorAction SilentlyContinue
        if (-not $firefoxCommand) { $firefoxCommand = Get-Command firefox -ErrorAction SilentlyContinue }
        $firefox = $null
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt [int]$settings.rogue_dns_duration -and -not (Test-Path -LiteralPath $stopPath)) {
            $next = [Math]::Min([int]$settings.rogue_dns_duration, $clock.Elapsed.TotalSeconds + [int]$settings.rogue_dns_browse_interval)
            # Start-Process is asynchronous; retain only our browser's handle.
            if ($firefoxCommand -and $wwwfile.Count -gt 0) {
                try {
                    $url = Get-Random -InputObject $wwwfile
                    $firefox = Start-Process -FilePath $firefoxCommand.Source -ArgumentList '--headless', $url -WindowStyle Hidden -PassThru -ErrorAction Stop
                } catch { Write-SimLog "rogue_dns: browser launch failed: $($_.Exception.Message)" }
            }
            while ($clock.Elapsed.TotalSeconds -lt $next -and -not (Test-Path -LiteralPath $stopPath)) {
                # One-second chunks so a stop request is seen promptly.
                Start-Sleep -Milliseconds ([int](1000 * [Math]::Max(0, [Math]::Min(1, $next - $clock.Elapsed.TotalSeconds))))
            }
            if ($firefox -and -not $firefox.HasExited) { $firefox | Stop-Process -Force -ErrorAction SilentlyContinue }
            $firefox = $null
        }
        if (Test-Path -LiteralPath $stopPath) { Write-SimLog 'rogue_dns: stop requested; restoring DHCP' }
    } finally {
        try {
            if ($firefox -and -not $firefox.HasExited) { $firefox | Stop-Process -Force -ErrorAction SilentlyContinue }
        } finally {
            Restore-RogueDnsDhcp $state
        }
    }
} catch {
    # Failed restore deliberately retains phase=static for the next attempt.
    Write-SimLog "rogue_dns: $($_.Exception.Message)"
    exit 1
}
