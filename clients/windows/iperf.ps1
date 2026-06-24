$version = '.03'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-iperf.log'

"iPerf Script Version $version" | Tee-Object -FilePath $debugPath
"iPerf Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

if (-not (Get-Command iperf3 -ErrorAction SilentlyContinue)) {
    'iperf3 was not found in PATH. Skipping iperf simulation.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

if ([string]::IsNullOrWhiteSpace($iperf_server) -or [string]::IsNullOrWhiteSpace([string]$rn_iperf_port) -or [string]::IsNullOrWhiteSpace([string]$rn_iperf_time)) {
    'Missing iperf configuration values. Skipping iperf simulation.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

$ports = @($rn_iperf_port, 443, 3260, 2049, 1194, 3389, 445, 80, 1433)
"iPerf Server: $iperf_server" | Tee-Object -FilePath $debugPath -Append | Out-Null
"Ports: $($ports -join ', ')" | Tee-Object -FilePath $debugPath -Append | Out-Null
"Duration: $rn_iperf_time" | Tee-Object -FilePath $debugPath -Append | Out-Null

foreach ($port in $ports) {
    "Running iperf3 on port $port" | Tee-Object -FilePath $debugPath -Append | Out-Null
    try {
        & iperf3 -c $iperf_server -p $port -b 1k -t $rn_iperf_time 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
    } catch {
        "iperf3 failed on port $port: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    }
}
