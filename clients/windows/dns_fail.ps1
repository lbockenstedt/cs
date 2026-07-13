. 'C:\Scripts\ini-parser.ps1'

$version = '.04'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-dns-fail.log'

"DNS Failure Script Version $version" | Tee-Object -FilePath $debugPath
"DNS Failure Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'

try {
    $dnsfile = @((Get-Content -LiteralPath 'C:\Scripts\dns_fail.txt' -ErrorAction Stop) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    "Unable to read dns_fail.txt: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

$dns_latency_1 = get_value 'address' 'dns_latency_1'
$dns_latency_2 = get_value 'address' 'dns_latency_2'
$dns_latency_3 = get_value 'address' 'dns_latency_3'
$dns_bad_ip_1 = get_value 'address' 'dns_bad_ip_1'
$dns_bad_ip_2 = get_value 'address' 'dns_bad_ip_2'
$dns_bad_ip_3 = get_value 'address' 'dns_bad_ip_3'
$dns_bad_record_1 = get_value 'address' 'dns_bad_record_1'
$dns_bad_record_2 = get_value 'address' 'dns_bad_record_2'
$dns_bad_record_3 = get_value 'address' 'dns_bad_record_3'

$bad_records = @($dns_bad_record_1, $dns_bad_record_2, $dns_bad_record_3)
$bad_ips = @($dns_bad_ip_1, $dns_bad_ip_2, $dns_bad_ip_3)
$latencies = @($dns_latency_1, $dns_latency_2, $dns_latency_3)
$servers = @($bad_records + $bad_ips + $latencies | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })

# ------------------------------------------------------------
# Fire-and-forget DNS failures
#
# Central's DNS-failure alarm is rate-based: it needs roughly 200 failed
# lookups per minute before it trips. The servers above are unreachable or slow
# on purpose (the bad_ip ones are RFC1918 blackholes), so a normal
# Resolve-DnsName would sit and WAIT on its timeout and we'd only manage a
# handful per minute -- the alarm would never fire.
#
# Instead we launch every lookup with nslookup as a hidden background process
# and never wait for the answer. The failure is the point, not the reply. Each
# nslookup gets a 1-second timeout so the background lookups clear quickly
# instead of piling up, and we pause a fraction of a second between launches to
# set the rate.
# ------------------------------------------------------------

# How fast to fire (lookups per minute) and how long to keep firing (seconds).
# Both are read from simulation.conf [simulation]; if unset we use safe
# defaults. The rate is never allowed below the ~200/min the alarm needs.
$ratePerMinute = get_value 'simulation' 'dns_fail_rate'
$burstSeconds  = get_value 'simulation' 'dns_fail_duration'
if (-not $ratePerMinute) { $ratePerMinute = 600 }
if (-not $burstSeconds)  { $burstSeconds  = 60 }
$ratePerMinute = [int]$ratePerMinute
$burstSeconds  = [int]$burstSeconds
if ($ratePerMinute -lt 200) { $ratePerMinute = 200 }

# Milliseconds to wait between each launch to hit the target rate.
# Example: 600 per minute -> 60000/600 -> 100 ms between lookups.
$pauseMs = [int](60000 / $ratePerMinute)

"$(Get-Date) Firing DNS failures at $ratePerMinute/min for ${burstSeconds}s" |
    Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

# Keep firing until the burst window is up, cycling through every record
# against every bad/slow server.
$stopAt = (Get-Date).AddSeconds($burstSeconds)
$fired = 0
while ((Get-Date) -lt $stopAt) {
    foreach ($record in $dnsfile) {
        foreach ($server in $servers) {

            # Stop the moment the burst window closes.
            if ((Get-Date) -ge $stopAt) { break }

            # Launch the lookup as a hidden process and move straight on.
            Start-Process -FilePath 'nslookup.exe' `
                -ArgumentList '-timeout=1', $record, $server `
                -WindowStyle Hidden | Out-Null

            $fired++
            Start-Sleep -Milliseconds $pauseMs
        }

        # Also stop between records once the window has closed.
        if ((Get-Date) -ge $stopAt) { break }
    }
}

"$(Get-Date) DNS failures fired: $fired" |
    Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
