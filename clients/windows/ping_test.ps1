$version = '0.01'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-ping-test.log'

"Ping_Test Script Version $version" | Tee-Object -FilePath $debugPath
"Ping_Test Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null

if ([string]::IsNullOrWhiteSpace($ping_address) -or [string]::IsNullOrWhiteSpace([string]$rn) -or [string]::IsNullOrWhiteSpace([string]$rn_ping_size)) {
    'Missing ping simulation values. Skipping ping test.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

try {
    Test-Connection -ComputerName $ping_address -Count $rn -BufferSize $rn_ping_size -ErrorAction Stop 2>&1 |
        Tee-Object -FilePath $debugPath -Append | Out-Null
} catch {
    "Ping test failed: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}
