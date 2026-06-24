. 'C:\Scripts\ini-parser.ps1'

$version = '.03'
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

for ($i = 1; $i -le 10; $i++) {
    "Iteration $i of 10" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    foreach ($r in $dnsfile) {
        Get-Date | Tee-Object -FilePath $debugPath -Append | Out-Null
        'Running DNS Failure:' | Tee-Object -FilePath $debugPath -Append | Out-Null
        $r | Tee-Object -FilePath $debugPath -Append | Out-Null

        foreach ($server in $servers) {
            "Querying $r against $server" | Tee-Object -FilePath $debugPath -Append | Out-Null
            try {
                Resolve-DnsName -Name $r -Server $server -ErrorAction SilentlyContinue 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
            } catch {
                "DNS query error for $r via $server: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Out-Null
            }
        }

        Start-Sleep -Seconds 5
    }
}
