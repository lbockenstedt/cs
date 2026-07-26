$version = '0.01'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-download.log'
$tempPath = 'C:\Temp'
$outFile = Join-Path $tempPath 'file.tmp'

"Download Script Version $version" | Tee-Object -FilePath $debugPath
"Download Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

if (-not (Test-Path -LiteralPath $tempPath)) {
    New-Item -ItemType Directory -Path $tempPath -Force | Out-Null
}

try {
    $dlfile = @((Get-Content -LiteralPath 'C:\Scripts\downloads.txt' -ErrorAction Stop) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    "Unable to read downloads.txt: $($_.Exception.Message)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

$r_count = $dlfile.Count
if ($r_count -eq 0) {
    'No download URLs available.' | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    exit 0
}

$rn_dl = Get-Random -Minimum 0 -Maximum $r_count
$url = $dlfile[$rn_dl]

Start-Sleep -Seconds 1
"Selected URL: $url" | Tee-Object -FilePath $debugPath -Append | Out-Null

try {
    $response = Invoke-WebRequest -Uri $url -OutFile $outFile -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
    "Download completed with status code $($response.StatusCode)" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
} catch {
    "Download failed for $url" | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
    $_.Exception.Message | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}
