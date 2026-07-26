$version = '0.01'
$logPath = 'C:\Scripts\sim.log'
$debugPath = 'C:\Scripts\debug-apt-update.log'

function Write-AptUpdateLog {
    param([string]$Message)
    $Message | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null
}

"apt update Script Version $version" | Tee-Object -FilePath $debugPath
"apt update Script Version $version" | Tee-Object -FilePath $logPath -Append | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Tee-Object -FilePath $logPath -Append | Out-Null

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-AptUpdateLog 'winget was not found. Skipping package updates.'
    exit 0
}

function Invoke-WingetCommand {
    param([string[]]$Arguments)

    & winget @Arguments 2>&1 | Tee-Object -FilePath $debugPath -Append | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-AptUpdateLog "winget command failed: winget $($Arguments -join ' ')"
    }
}

Write-AptUpdateLog 'Running system package upgrades'
Invoke-WingetCommand -Arguments @('upgrade','--all','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing iperf3'
Invoke-WingetCommand -Arguments @('install','--id','EsoftInteractive.iperf3','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing Git'
Invoke-WingetCommand -Arguments @('install','--id','Git.Git','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing Firefox'
Invoke-WingetCommand -Arguments @('install','--id','Mozilla.Firefox','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing Python 3.12'
Invoke-WingetCommand -Arguments @('install','--id','Python.Python.3.12','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing GNU Wget'
Invoke-WingetCommand -Arguments @('install','--id','JernejSimoncic.Wget','--silent','--accept-package-agreements','--accept-source-agreements')

Write-AptUpdateLog 'Installing Windows Terminal'
Invoke-WingetCommand -Arguments @('install','--id','Microsoft.WindowsTerminal','--silent','--accept-package-agreements','--accept-source-agreements')

# Not applicable on Windows:
# - network-manager, wpasupplicant: Windows handles WiFi natively via WlanAPI/netsh.
# - rfkill: Windows manages radio state through device management and netsh.
# - rsyslog: Windows uses Event Log instead.
# - smbclient: Windows provides native SMB access with net use / New-PSDrive.
# - dkms, kernel headers: Linux-only kernel module tooling.
# - qemu-guest-agent: install separately with a QEMU guest agent MSI when needed.
