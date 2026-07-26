$version = '0.01'
$logPath = 'C:\Scripts\sim_reboot.log'

"Sys_Mon Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null

try {
    $latestEvent = Get-WinEvent -LogName System -MaxEvents 1 -ErrorAction Stop
    $lastRecordId = if ($latestEvent) { [long]$latestEvent.RecordId } else { 0 }
} catch {
    $lastRecordId = 0
    "Unable to initialize System log monitor: $($_.Exception.Message)" | Tee-Object -FilePath $logPath -Append | Out-Null
}

while ($true) {
    try {
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = 'System' } -MaxEvents 50 -ErrorAction Stop |
            Where-Object { $_.RecordId -gt $lastRecordId } |
            Sort-Object RecordId)

        foreach ($event in $events) {
            $message = [string]$event.Message
            if ($message -match 'Call Trace' -or $message -match 'failure') {
                'Failure message Found' | Tee-Object -FilePath $logPath -Append | Out-Null
                'Rebooting system' | Tee-Object -FilePath $logPath -Append | Out-Null
                Get-Date | Tee-Object -FilePath $logPath -Append | Out-Null
                '--------------------------' | Tee-Object -FilePath $logPath -Append | Out-Null
                Restart-Computer -Force
            }
            $lastRecordId = [long]$event.RecordId
        }
    } catch {
        "System log monitor error: $($_.Exception.Message)" | Tee-Object -FilePath $logPath -Append | Out-Null
    }

    Start-Sleep -Seconds 10
}
