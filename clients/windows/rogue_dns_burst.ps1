param([switch]$RestoreOnly)
$version = '0.01'
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'rogue_dns_burst.ps1' $version
$statePath = 'C:\Scripts\rogue_dns_burst.state'

function Save-RogueDnsBurstState {
    param($State)
    $State | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath "$statePath.new" -Encoding UTF8 -ErrorAction Stop
    Move-Item -LiteralPath "$statePath.new" -Destination $statePath -Force -ErrorAction Stop
}
function Restore-RogueDnsBurstDhcp {
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
    Save-RogueDnsBurstState @{ phase = 'dhcp'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
}

try {
    if (Test-Path -LiteralPath $statePath) {
        $state = Get-Content -LiteralPath $statePath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($state.phase -notin @('static','dhcp') -or $null -eq $state.start) { throw 'Invalid rogue_dns_burst state' }
        if ($state.phase -eq 'static') {
            Restore-RogueDnsBurstDhcp $state
            return
        }
    } else {
        if (-not $RestoreOnly) { Save-RogueDnsBurstState @{ phase = 'dhcp'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() } }
        return
    }
    if ($RestoreOnly) { return }
    $settings = @{}
    $overrides = Get-SimOverrides
    foreach ($key in @('rogue_dns_burst_server','rogue_dns_burst_dhcp_interval','rogue_dns_burst_duration','rogue_dns_burst_browse_interval','rogue_dns_burst_rate','rogue_dns_burst_max_pending','rogue_dns_burst_domains')) {
        $section = 'simulation'
        if ($key -eq 'rogue_dns_burst_server' -or $key -eq 'rogue_dns_burst_domains') { $section = 'address' }
        $settings[$key] = get_value $section $key
        if ($overrides.ContainsKey($key) -and -not [string]::IsNullOrWhiteSpace($overrides[$key])) { $settings[$key] = $overrides[$key] }
    }
    foreach ($entry in @{ rogue_dns_burst_dhcp_interval = 1800; rogue_dns_burst_duration = 3600; rogue_dns_burst_browse_interval = 60; rogue_dns_burst_rate = 20; rogue_dns_burst_max_pending = 40 }.GetEnumerator()) {
        if ([string]$settings[$entry.Key] -notmatch '^[1-9][0-9]{0,7}$') { $settings[$entry.Key] = $entry.Value }
    }
    $domains = @(([string]$settings.rogue_dns_burst_domains) -split '[,\s]+' | Where-Object { $_ })
    if ($domains.Count -eq 0) { $domains = @('www.google.com','www.youtube.com','www.linkedin.com') }
    if (([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$state.start) -lt [int]$settings.rogue_dns_burst_dhcp_interval) { return }
    $server = [string]$settings.rogue_dns_burst_server
    $ip = $null
    if ($server -notmatch '^\d{1,3}(\.\d{1,3}){3}$' -or -not [System.Net.IPAddress]::TryParse($server, [ref]$ip) -or
        $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $ip.GetAddressBytes()[0] -eq 0 -or $ip.GetAddressBytes()[0] -eq 127 -or $ip.GetAddressBytes()[0] -ge 224) {
        Write-SimLog 'rogue_dns_burst: blank or invalid rogue_dns_burst_server; skipping'
        return
    }
    $name = Get-WlanAdapter
    if (-not $name) { Write-SimLog 'rogue_dns_burst: no wireless lease'; return }
    $adapter = Get-NetAdapter -Name $name -ErrorAction Stop
    $idx = $adapter.ifIndex
    $lease = Get-NetIPAddress -ErrorAction Stop |
        Where-Object { $_.InterfaceIndex -eq $idx -and $_.AddressFamily -eq 'IPv4' -and $_.PrefixOrigin -eq 'Dhcp' -and $_.IPAddress -notmatch '^169\.254\.' } | Select-Object -First 1
    $route = Get-NetRoute -ErrorAction Stop | Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' } | Sort-Object RouteMetric | Select-Object -First 1
    if (-not $lease -or -not $route) { Write-SimLog 'rogue_dns_burst: no DHCP lease; skipping'; return }
    $state = @{ phase = 'static'; start = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); adapter = [string]$adapter.InterfaceGuid
        address = $lease.IPAddress; prefix = $lease.PrefixLength; gateway = $route.NextHop
        dns = @((Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4).ServerAddresses) }
    # Persist before the first mutation so reboot/Stop-Process can recover.
    Save-RogueDnsBurstState $state
    try {
        Set-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
        Get-NetIPAddress -ErrorAction Stop |
            Where-Object { $_.InterfaceIndex -eq $idx -and $_.IPAddress -eq $state.address } | Remove-NetIPAddress -Confirm:$false -ErrorAction Stop
        Get-NetRoute -ErrorAction Stop |
            Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.NextHop -eq $state.gateway } | Remove-NetRoute -Confirm:$false -ErrorAction Stop
        New-NetIPAddress -InterfaceIndex $idx -IPAddress $state.address -PrefixLength $state.prefix -DefaultGateway $state.gateway -ErrorAction Stop | Out-Null
        Set-DnsClientServerAddress -InterfaceIndex $idx -ServerAddresses $server -ErrorAction Stop
        # Background query load for the static window. It reuses the shared DNS
        # ceiling + gateway breaker from common.ps1 and is stopped in finally,
        # before DHCP is restored. Only this job is ever stopped.
        $burstRate = [int]$settings.rogue_dns_burst_rate
        $burstMax = [int]$settings.rogue_dns_burst_max_pending
        $burstJob = Start-Job -ArgumentList @($server, $domains, $burstRate, $burstMax) -ScriptBlock {
            param($Server, $Domains, $Rate, $MaxPending)
            . 'C:\Scripts\common.ps1'
            $gw = Get-DnsDefaultGateway
            $nextGw = [DateTime]::UtcNow
            $pending = New-Object System.Collections.ArrayList
            $idx = 0
            try {
                while ($true) {
                    $rate = Get-DnsCeilingRate $Rate
                    if ($gw -and [DateTime]::UtcNow -ge $nextGw) {
                        $nextGw = [DateTime]::UtcNow.AddSeconds(10)
                        if (-not (Test-DnsGatewayAlive $gw) -and (Test-DnsGatewayConfirmedDown $gw)) {
                            $rate = Invoke-DnsCeilingPenalize $rate
                            while (-not (Test-DnsGatewayStable $gw)) { Start-Sleep -Seconds 2 }
                        }
                    }
                    for ($n = 0; $n -lt $rate; $n++) {
                        # Drop finished (or 2s-expired) queries; wait on the oldest when full.
                        foreach ($p in @($pending)) {
                            if ($p.Handle.IsCompleted -or ([DateTime]::UtcNow - $p.Started).TotalSeconds -ge 2) {
                                try { $p.Ps.Stop() } catch {}
                                $p.Ps.Dispose()
                                $pending.Remove($p)
                            }
                        }
                        while ($pending.Count -ge $MaxPending) { Start-Sleep -Milliseconds 50
                            $oldest = $pending[0]
                            if ($oldest.Handle.IsCompleted -or ([DateTime]::UtcNow - $oldest.Started).TotalSeconds -ge 2) {
                                try { $oldest.Ps.Stop() } catch {}
                                $oldest.Ps.Dispose()
                                $pending.RemoveAt(0)
                            }
                        }
                        $ps = [powershell]::Create()
                        [void]$ps.AddScript('param($d,$s) Resolve-DnsName -Name $d -Server $s -Type A -DnsOnly -ErrorAction SilentlyContinue | Out-Null').AddArgument($Domains[$idx % $Domains.Count]).AddArgument($Server)
                        [void]$pending.Add([pscustomobject]@{ Ps = $ps; Handle = $ps.BeginInvoke(); Started = [DateTime]::UtcNow })
                        $idx++
                    }
                    Start-Sleep -Seconds 1
                }
            } finally {
                foreach ($p in @($pending)) { try { $p.Ps.Stop() } catch {}; $p.Ps.Dispose() }
            }
        }
        $wwwfile = @(Get-Content -LiteralPath 'C:\Scripts\websites.txt' -ErrorAction SilentlyContinue | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $firefoxCommand = Get-Command firefox.exe -ErrorAction SilentlyContinue
        if (-not $firefoxCommand) { $firefoxCommand = Get-Command firefox -ErrorAction SilentlyContinue }
        $firefox = $null
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt [int]$settings.rogue_dns_burst_duration) {
            $next = [Math]::Min([int]$settings.rogue_dns_burst_duration, $clock.Elapsed.TotalSeconds + [int]$settings.rogue_dns_burst_browse_interval)
            # Start-Process is asynchronous; retain only our browser's handle.
            if ($firefoxCommand -and $wwwfile.Count -gt 0) {
                try {
                    $url = Get-Random -InputObject $wwwfile
                    $firefox = Start-Process -FilePath $firefoxCommand.Source -ArgumentList '--headless', $url -WindowStyle Hidden -PassThru -ErrorAction Stop
                } catch { Write-SimLog "rogue_dns_burst: browser launch failed: $($_.Exception.Message)" }
            }
            while ($clock.Elapsed.TotalSeconds -lt $next) {
                # Chunk long intervals: Start-Sleep -Milliseconds takes Int32.
                Start-Sleep -Milliseconds ([int](1000 * [Math]::Max(0, [Math]::Min(60, $next - $clock.Elapsed.TotalSeconds))))
            }
            if ($firefox -and -not $firefox.HasExited) { $firefox | Stop-Process -Force -ErrorAction SilentlyContinue }
            $firefox = $null
        }
    } finally {
        try {
            if ($burstJob) { Stop-Job $burstJob -ErrorAction SilentlyContinue; Remove-Job $burstJob -Force -ErrorAction SilentlyContinue }
            if ($firefox -and -not $firefox.HasExited) { $firefox | Stop-Process -Force -ErrorAction SilentlyContinue }
        } finally {
            Restore-RogueDnsBurstDhcp $state
        }
    }
} catch {
    # Failed restore deliberately retains phase=static for the next attempt.
    Write-SimLog "rogue_dns_burst: $($_.Exception.Message)"
    exit 1
}
