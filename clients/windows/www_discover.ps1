$version = '0.01'
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'www_discover.ps1' $version
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-www-discover.log'

"WWW_Discover Script Version $version" | Tee-Object -FilePath $debugPath | Out-Null
try {
    $wwwfile = @((Get-Content -LiteralPath 'C:\Scripts\www_discover_websites.txt' -ErrorAction Stop) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    "Unable to read www_discover_websites.txt: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}
if ($wwwfile.Count -eq 0) {
    'No websites configured for WWW Discover.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}
$url = Get-Random -InputObject $wwwfile
$firefoxCommand = Get-Command firefox.exe -ErrorAction SilentlyContinue
if (-not $firefoxCommand) {
    $firefoxCommand = Get-Command firefox -ErrorAction SilentlyContinue
}
if (-not $firefoxCommand) {
    'Firefox was not found in PATH. Skipping WWW Discover.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}
"Launching Firefox headless for $url" | Tee-Object -FilePath $debugPath -Append | Out-Null
$firefox = $null
try {
    $firefox = Start-Process -FilePath $firefoxCommand.Source -ArgumentList '--headless', $url -WindowStyle Hidden -PassThru -ErrorAction Stop
    if (-not $firefox.WaitForExit(60000)) {
        'Firefox reached the 60s limit.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    }
} catch {
    "Firefox run failed: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
} finally {
    # Only the process returned by our launch is eligible for cleanup.
    if ($firefox -and -not $firefox.HasExited) {
        $firefox | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    if ($firefox) { $firefox.Dispose() }
}
exit 0
