$version = '.03'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-www-traffic.log'

"WWW_Traffic Script Version $version" | Tee-Object -FilePath $debugPath
"WWW_Traffic Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null

try {
    $wwwfile = @((Get-Content -LiteralPath 'C:\Scripts\websites.txt' -ErrorAction Stop) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    "Unable to read websites.txt: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

if ($wwwfile.Count -eq 0) {
    'No websites configured for web traffic simulation.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

$url = Get-Random -InputObject $wwwfile
"Launching Firefox headless for $url" | Tee-Object -FilePath $debugPath -Append | Out-Null

$firefoxCommand = Get-Command firefox.exe -ErrorAction SilentlyContinue
if (-not $firefoxCommand) {
    $firefoxCommand = Get-Command firefox -ErrorAction SilentlyContinue
}

if (-not $firefoxCommand) {
    'Firefox was not found in PATH. Skipping web traffic simulation.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

try {
    Start-Process -FilePath $firefoxCommand.Source -ArgumentList '--headless', $url -WindowStyle Hidden | Out-Null
} catch {
    "Failed to launch Firefox: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}
