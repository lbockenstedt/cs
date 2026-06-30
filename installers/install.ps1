#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-IsAdministrator {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

if (-not (Test-IsAdministrator)) {
    $scriptPath = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }
    $hostExe = if ($PSVersionTable.PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' }
    $args = @(
        '-NoProfile'
        '-ExecutionPolicy', 'Bypass'
        '-File', ('"{0}"' -f $scriptPath)
    )

    try {
        Start-Process -FilePath $hostExe -ArgumentList ($args -join ' ') -Verb RunAs | Out-Null
        exit 0
    } catch {
        Write-Error "ERROR: This script must be run as Administrator (self-elevation failed: $($_.Exception.Message))"
        exit 1
    }
}

$VERSION = '0.01'
$INSTALL_START = Get-Date
$WARN_COUNT = 0
$ERR_COUNT = 0
$PHASE_START = Get-Date
$CURRENT_PHASE = 0
$CURRENT_PROGRESS = 0
$SIM_USER = 'sim-user'

$STATE_DIR = 'C:\ProgramData\client-sim'
$STATE_FILE = Join-Path $STATE_DIR 'install.state'
$PASSWORD_FILE = Join-Path $STATE_DIR 'sim-user-password.secure.txt'
$SMB_CREDS_FILE = Join-Path $STATE_DIR 'smb-credentials.txt'
$LOG_DIR = 'C:\Logs'
$LOG_FILE = Join-Path $LOG_DIR 'client-sim_install.log'
$SCRIPTS_DIR = 'C:\Scripts'
$SIM_LOG = Join-Path $SCRIPTS_DIR 'sim.log'
$TEMP_DIR = 'C:\Temp'
$EVENT_REG_PATH = 'HKLM:\SOFTWARE\ClientSim'
$VH_DIR = Join-Path ${env:ProgramFiles} 'VirtualHere'
$QEMU_URL = 'https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win-guest-tools.exe'

$PHASE_NAMES = @(
    'User Provisioning',
    'Package Install',
    'Display & Power',
    'Scripts Directory',
    'Client-Sim Repo',
    'SMB Config Sync',
    'Event Log Setup',
    'VirtualHere Install',
    'Driver Check',
    'Health Check'
)
$PHASE_WEIGHTS = @(4, 20, 5, 2, 8, 5, 2, 10, 42, 2)
$SPINNER_FRAMES = @('⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏')
$script:SPINNER_JOB = $null
$script:SPINNER_LABEL = ''

New-Item -ItemType Directory -Path $STATE_DIR, $LOG_DIR -Force | Out-Null
Set-Content -Path $LOG_FILE -Value '' -Encoding UTF8
Set-Content -Path $STATE_FILE -Value '' -Encoding UTF8

$ESC = [char]27
$SupportsAnsi = $false
try {
    $SupportsAnsi = [bool]$Host.UI.SupportsVirtualTerminal
} catch {
    $SupportsAnsi = $false
}
if ($SupportsAnsi) {
    $COL_RESET  = "$ESC[0m"
    $COL_GREEN  = "$ESC[0;32m"
    $COL_CYAN   = "$ESC[0;36m"
    $COL_YELLOW = "$ESC[1;33m"
    $COL_RED    = "$ESC[0;31m"
    $COL_BOLD   = "$ESC[1m"
    $COL_DIM    = "$ESC[2m"
    $CURSOR_HIDE = "$ESC[?25l"
    $CURSOR_SHOW = "$ESC[?25h"
    $CLEAR_LINE = "`r$ESC[2K"
} else {
    $COL_RESET = ''
    $COL_GREEN = ''
    $COL_CYAN = ''
    $COL_YELLOW = ''
    $COL_RED = ''
    $COL_BOLD = ''
    $COL_DIM = ''
    $CURSOR_HIDE = ''
    $CURSOR_SHOW = ''
    $CLEAR_LINE = "`r"
}

$TERM_WIDTH = 80
try {
    if ($Host.UI.RawUI.WindowSize.Width -gt 0) {
        $TERM_WIDTH = $Host.UI.RawUI.WindowSize.Width
    }
} catch {
    $TERM_WIDTH = 80
}
$BAR_WIDTH = $TERM_WIDTH - 30
if ($BAR_WIDTH -lt 20) { $BAR_WIDTH = 20 }

function ts {
    (Get-Date).ToString('HH:mm:ss')
}

function Write-LogLine {
    param(
        [string]$Line,
        [ConsoleColor]$Color = [ConsoleColor]::Gray,
        [switch]$ErrorToStdErr
    )

    Add-Content -Path $LOG_FILE -Value $Line -Encoding UTF8
    if ($ErrorToStdErr) {
        [Console]::Error.WriteLine($Line)
    } else {
        Write-Host $Line -ForegroundColor $Color
    }
}

function info {
    param([string]$Message)
    Write-LogLine -Line "[$(ts)] INFO: $Message" -Color Cyan
}

function ok {
    param([string]$Message)
    Write-LogLine -Line "[$(ts)] OK:   $Message" -Color Green
}

function warn {
    param([string]$Message)
    $script:WARN_COUNT++
    Write-LogLine -Line "[$(ts)] WARN: $Message" -Color Yellow
}

function err {
    param([string]$Message)
    $script:ERR_COUNT++
    Write-LogLine -Line "[$(ts)] ERR:  $Message" -Color Red -ErrorToStdErr
}

function Set-StateEntry {
    param(
        [string]$Key,
        [string]$Value
    )

    $lines = @()
    if (Test-Path $STATE_FILE) {
        $lines = @(Get-Content -Path $STATE_FILE -ErrorAction SilentlyContinue)
    }
    $pattern = '^{0}:' -f [regex]::Escape($Key)
    $updated = @($lines | Where-Object { $_ -notmatch $pattern })
    $updated += ('{0}:{1}' -f $Key, $Value)
    Set-Content -Path $STATE_FILE -Value $updated -Encoding UTF8
}

function Draw-Bar {
    param(
        [int]$Percent,
        [string]$Label
    )

    if ($Percent -lt 0) { $Percent = 0 }
    if ($Percent -gt 100) { $Percent = 100 }

    $filled = [int]([math]::Floor(($Percent * $BAR_WIDTH) / 100))
    $empty = $BAR_WIDTH - $filled
    $bar = ('█' * $filled) + ('░' * $empty)
    $color = $COL_GREEN
    if ($Percent -lt 50) { $color = $COL_CYAN }
    if ($Percent -lt 20) { $color = $COL_YELLOW }

    $shortLabel = if ($Label.Length -gt 20) { $Label.Substring(0, 20) } else { $Label.PadRight(20) }
    Write-Host -NoNewline ("`r{0}{1}{2} {3}{4}{5} {6}{7,3}%{2}" -f $COL_BOLD, $shortLabel, $COL_RESET, $color, $bar, $COL_RESET, $COL_BOLD, $Percent)
}

function Phase-Step {
    param(
        [int]$Step,
        [int]$Total
    )

    $weight = $PHASE_WEIGHTS[$CURRENT_PHASE]
    $previous = 0
    for ($i = 0; $i -lt $CURRENT_PHASE; $i++) {
        $previous += $PHASE_WEIGHTS[$i]
    }
    $fractional = $previous
    if ($Total -gt 0) {
        $fractional += [int]([math]::Floor(($Step * $weight) / $Total))
    }
    Draw-Bar -Percent $fractional -Label $PHASE_NAMES[$CURRENT_PHASE]
}

function Start-Spinner {
    param([string]$Label = 'Working...')

    Stop-Spinner
    $script:SPINNER_LABEL = $Label
    $frames = $SPINNER_FRAMES
    $script:SPINNER_JOB = Start-Job -ScriptBlock {
        param($SpinnerFrames)
        $index = 0
        while ($true) {
            Start-Sleep -Milliseconds 100
            Write-Output $SpinnerFrames[$index]
            $index = ($index + 1) % $SpinnerFrames.Count
        }
    } -ArgumentList (, $frames)

    Start-Sleep -Milliseconds 120
    $frame = Receive-Job -Job $script:SPINNER_JOB -Keep -ErrorAction SilentlyContinue | Select-Object -Last 1
    if (-not $frame) { $frame = $frames[0] }
    Write-Host -NoNewline ("`r  {0}{1}{2}  {3} " -f $COL_CYAN, $frame, $COL_RESET, $Label)
}

function Stop-Spinner {
    if ($script:SPINNER_JOB) {
        try {
            Stop-Job -Job $script:SPINNER_JOB -Force -ErrorAction SilentlyContinue | Out-Null
            Receive-Job -Job $script:SPINNER_JOB -ErrorAction SilentlyContinue | Out-Null
            Remove-Job -Job $script:SPINNER_JOB -Force -ErrorAction SilentlyContinue | Out-Null
        } catch {
        }
        $script:SPINNER_JOB = $null
    }
    Write-Host -NoNewline $CLEAR_LINE
}

function Begin-Phase {
    Stop-Spinner
    $script:PHASE_START = Get-Date
    $name = $PHASE_NAMES[$CURRENT_PHASE]
    Draw-Bar -Percent $CURRENT_PROGRESS -Label $name
    Write-Host ''
    info ("Phase {0}/{1}: {2}" -f ($CURRENT_PHASE + 1), $PHASE_NAMES.Count, $name)
}

function End-Phase {
    Stop-Spinner
    $elapsed = [int]((Get-Date) - $PHASE_START).TotalSeconds
    $weight = $PHASE_WEIGHTS[$CURRENT_PHASE]
    $script:CURRENT_PROGRESS += $weight
    if ($script:CURRENT_PROGRESS -gt 100) { $script:CURRENT_PROGRESS = 100 }
    $name = $PHASE_NAMES[$CURRENT_PHASE]
    Draw-Bar -Percent $CURRENT_PROGRESS -Label $name
    Write-Host ("  {0}✓{1}  {2}({3}s){1}" -f $COL_GREEN, $COL_RESET, $COL_DIM, $elapsed)
    $script:CURRENT_PHASE++
}

function Invoke-WithRetry {
    param([scriptblock]$ScriptBlock, [int]$Attempts = 3, [int]$Delay = 5)
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            & $ScriptBlock
            return
        } catch {
            warn "Attempt $i/$Attempts failed: $($_.Exception.Message)"
            if ($i -lt $Attempts) { Start-Sleep -Seconds $Delay }
        }
    }
    throw "All $Attempts attempts failed"
}

function Test-LocalUserExists {
    param([string]$UserName)
    try {
        if (Get-Command Get-LocalUser -ErrorAction SilentlyContinue) {
            return [bool](Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue)
        }
    } catch {
    }

    $computer = [ADSI]("WinNT://{0},computer" -f $env:COMPUTERNAME)
    foreach ($child in $computer.psbase.Children) {
        if ($child.SchemaClassName -eq 'user' -and $child.Name -eq $UserName) {
            return $true
        }
    }
    return $false
}

function New-SimUser {
    param([string]$UserName)

    $plainPassword = [guid]::NewGuid().ToString('N') + 'aA!9'
    $secure = ConvertTo-SecureString $plainPassword -AsPlainText -Force

    if (Get-Command New-LocalUser -ErrorAction SilentlyContinue) {
        New-LocalUser -Name $UserName -Password $secure -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword -Description 'Client Simulator local user' | Out-Null
    } else {
        $computer = [ADSI]("WinNT://{0},computer" -f $env:COMPUTERNAME)
        $user = $computer.Create('user', $UserName)
        $user.SetPassword($plainPassword)
        $user.SetInfo()
        $flags = 0x10000
        $user.userFlags = $flags
        $user.SetInfo()
        $user.Description = 'Client Simulator local user'
        $user.SetInfo()
    }

    $protected = $secure | ConvertFrom-SecureString
    Set-Content -Path $PASSWORD_FILE -Value @(
        'username=sim-user'
        ('created={0}' -f (Get-Date).ToString('s'))
        ('password={0}' -f $protected)
    ) -Encoding UTF8
    & icacls.exe $PASSWORD_FILE '/inheritance:r' '/grant:r' 'Administrators:F' 'SYSTEM:F' >> $LOG_FILE 2>&1
}

function Test-LocalGroupMembership {
    param(
        [string]$GroupName,
        [string]$UserName
    )

    try {
        if (Get-Command Get-LocalGroupMember -ErrorAction SilentlyContinue) {
            $members = Get-LocalGroupMember -Group $GroupName -ErrorAction SilentlyContinue
            return [bool]($members | Where-Object { $_.Name -match ('(^|\\){0}$' -f [regex]::Escape($UserName)) })
        }
    } catch {
    }

    $group = [ADSI]("WinNT://{0}/{1},group" -f $env:COMPUTERNAME, $GroupName)
    $members = @($group.psbase.Invoke('Members')) | ForEach-Object {
        $_.GetType().InvokeMember('Name', 'GetProperty', $null, $_, $null)
    }
    return [bool]($members | Where-Object { $_ -eq $UserName })
}

function Add-UserToLocalGroup {
    param(
        [string]$GroupName,
        [string]$UserName
    )

    if (Test-LocalGroupMembership -GroupName $GroupName -UserName $UserName) {
        return
    }

    if (Get-Command Add-LocalGroupMember -ErrorAction SilentlyContinue) {
        Add-LocalGroupMember -Group $GroupName -Member $UserName -ErrorAction Stop
    } else {
        $group = [ADSI]("WinNT://{0}/{1},group" -f $env:COMPUTERNAME, $GroupName)
        $group.Add(("WinNT://{0}/{1},user" -f $env:COMPUTERNAME, $UserName))
    }
}

function Ensure-SystemPathEntry {
    param([string]$PathEntry)

    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts = @($machinePath -split ';' | Where-Object { $_ })
    if ($parts -notcontains $PathEntry) {
        $newPath = if ($machinePath) { "$machinePath;$PathEntry" } else { $PathEntry }
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'Machine')
    }

    $processParts = @($env:Path -split ';' | Where-Object { $_ })
    if ($processParts -notcontains $PathEntry) {
        $env:Path = if ($env:Path) { "$env:Path;$PathEntry" } else { $PathEntry }
    }
}

function Install-WingetPackage {
    param(
        [string]$DisplayName,
        [string[]]$Ids,
        [string[]]$Queries = @()
    )

    $commonArgs = @('--silent', '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
    foreach ($id in $Ids) {
        try {
            & winget install --exact --id $id @commonArgs >> $LOG_FILE 2>&1
            if ($LASTEXITCODE -eq 0) { return }
        } catch {
        }
    }

    foreach ($query in $Queries) {
        try {
            & winget install --query $query @commonArgs >> $LOG_FILE 2>&1
            if ($LASTEXITCODE -eq 0) { return }
        } catch {
        }
    }

    throw "Failed to install $DisplayName with winget"
}

function Download-File {
    param(
        [string]$Url,
        [string]$Destination
    )

    $securityProtocol = [Net.SecurityProtocolType]::Tls12
    if ([enum]::GetNames([Net.SecurityProtocolType]) -contains 'Tls13') {
        $securityProtocol = $securityProtocol -bor [Net.SecurityProtocolType]::Tls13
    }
    [Net.ServicePointManager]::SecurityProtocol = $securityProtocol
    $client = New-Object System.Net.WebClient
    try {
        $client.DownloadFile($Url, $Destination)
    } finally {
        $client.Dispose()
    }
}

function Test-IsVirtualMachine {
    try {
        $cs = Get-CimInstance -ClassName Win32_ComputerSystem
        $text = ('{0} {1}' -f $cs.Manufacturer, $cs.Model)
        return $text -match 'Virtual|VMware|KVM|QEMU|VirtualBox|Hyper-V|Xen|HVM|Parallels'
    } catch {
        return $false
    }
}

function Get-PnpDevicesSafe {
    try {
        if (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue) {
            return @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)
        }
    } catch {
    }
    return @(Get-CimInstance -ClassName Win32_PnPEntity -ErrorAction SilentlyContinue)
}

function Get-WirelessAdaptersSafe {
    try {
        if (Get-Command Get-NetAdapter -ErrorAction SilentlyContinue) {
            return @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
                $_.Name -like '*Wi-Fi*' -or $_.InterfaceDescription -like '*Wireless*' -or $_.InterfaceDescription -like '*Wi-Fi*'
            })
        }
    } catch {
    }

    return @(Get-CimInstance -ClassName Win32_NetworkAdapter -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like '*Wi-Fi*' -or $_.Name -like '*Wireless*'
    })
}

function Test-PowerSettingDisabled {
    param([string[]]$Args)
    try {
        $output = (& powercfg.exe @Args | Out-String)
        return $output -match 'Current AC Power Setting Index:\s+0x00000000'
    } catch {
        return $false
    }
}

$cleanupEvent = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    try {
        Write-Host -NoNewline "$using:CURSOR_SHOW"
    } catch {
    }
} -SupportEvent

[Console]::TreatControlCAsInput = $false
Write-Host -NoNewline $CURSOR_HIDE

try {
    @(
        ''
        '============================================================'
        (" Client Simulator Installer v{0}" -f $VERSION)
        (" Started at: {0}" -f (Get-Date))
        '============================================================'
        ''
    ) | Tee-Object -FilePath $LOG_FILE -Append | ForEach-Object { Write-Host $_ }

    Write-Host -NoNewline ("{0}  Phases : {1}{2}" -f $COL_DIM, (($PHASE_NAMES -join "${COL_DIM} → ${COL_RESET}${COL_DIM}")), $COL_RESET)
    Write-Host ''
    Write-Host ("{0}  Log    : {1}{2}" -f $COL_DIM, $LOG_FILE, $COL_RESET)
    Write-Host ("{0}  Press Ctrl+C at any time to abort.{1}" -f $COL_DIM, $COL_RESET)
    Write-Host ''

    ###############################################################################
    # PHASE 1 — USER PROVISIONING
    ###############################################################################
    Begin-Phase

    Start-Spinner "Checking user '$SIM_USER'"
    if (-not (Test-LocalUserExists -UserName $SIM_USER)) {
        New-SimUser -UserName $SIM_USER
        Stop-Spinner
        ok "Created user '$SIM_USER' with a random stored password"
        Set-StateEntry -Key 'sim-user' -Value 'CREATED'
    } else {
        Stop-Spinner
        ok "User '$SIM_USER' already exists"
        Set-StateEntry -Key 'sim-user' -Value 'EXISTS'
    }

    Start-Spinner "Adding '$SIM_USER' to Remote Desktop Users"
    Add-UserToLocalGroup -GroupName 'Remote Desktop Users' -UserName $SIM_USER
    Stop-Spinner
    ok "Remote Desktop Users membership configured for '$SIM_USER'"
    info "Windows uses local group membership instead of sudoers for scoped access"
    Set-StateEntry -Key 'sim-user-group' -Value 'REMOTE_DESKTOP_USERS'

    End-Phase

    ###############################################################################
    # PHASE 2 — PACKAGE INSTALL
    ###############################################################################
    Begin-Phase

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Stop-Spinner
        err 'winget not found — skipping package installation phase'
        Set-StateEntry -Key 'packages' -Value 'WINGET_MISSING'
    } else {
        $packages = @(
            @{ Name = 'Git'; Ids = @('Git.Git') },
            @{ Name = 'iperf3'; Ids = @('EsoftInteractive.iperf3'); Queries = @('iperf3') },
            @{ Name = 'Firefox'; Ids = @('Mozilla.Firefox') },
            @{ Name = 'Python 3.12'; Ids = @('Python.Python.3.12') },
            @{ Name = 'GNU wget'; Ids = @('JernejSimoncic.Wget'); Queries = @('wget') },
            @{ Name = 'Windows Terminal'; Ids = @('Microsoft.WindowsTerminal') }
        )

        $totalPackages = $packages.Count
        $batchSize = 3
        $processed = 0

        for ($i = 0; $i -lt $totalPackages; $i += $batchSize) {
            $upper = [Math]::Min($i + $batchSize - 1, $totalPackages - 1)
            $batch = $packages[$i..$upper]
            $processed += $batch.Count
            Phase-Step -Step $processed -Total $totalPackages
            Start-Spinner ("Installing: {0}" -f (($batch | ForEach-Object { $_.Name }) -join ', '))
            foreach ($pkg in $batch) {
                Invoke-WithRetry -ScriptBlock {
                    Install-WingetPackage -DisplayName $pkg.Name -Ids $pkg.Ids -Queries $pkg.Queries
                }
            }
            Stop-Spinner
            ok ("Installed batch: {0}" -f (($batch | ForEach-Object { $_.Name }) -join ', '))
        }

        if (Test-IsVirtualMachine) {
            $qemuInstaller = Join-Path $TEMP_DIR 'virtio-win-guest-tools.exe'
            Start-Spinner 'Downloading qemu-guest-agent guest tools'
            New-Item -ItemType Directory -Path $TEMP_DIR -Force | Out-Null
            Invoke-WithRetry -ScriptBlock {
                Download-File -Url $QEMU_URL -Destination $qemuInstaller
            }
            Stop-Spinner
            ok 'qemu-guest-agent installer downloaded'

            Start-Spinner 'Installing qemu-guest-agent guest tools'
            try {
                $proc = Start-Process -FilePath $qemuInstaller -ArgumentList '/quiet','/norestart' -Wait -PassThru -WindowStyle Hidden
                if ($proc.ExitCode -ne 0) {
                    throw "Installer exit code $($proc.ExitCode)"
                }
                Stop-Spinner
                ok 'qemu-guest-agent installed'
                Set-StateEntry -Key 'qemu-guest-agent' -Value 'INSTALLED'
            } catch {
                Stop-Spinner
                warn "qemu-guest-agent install failed: $($_.Exception.Message)"
                Set-StateEntry -Key 'qemu-guest-agent' -Value 'FAILED'
            }
        } else {
            info 'Not running in a detected VM — skipping qemu-guest-agent install'
            Set-StateEntry -Key 'qemu-guest-agent' -Value 'SKIPPED'
        }

        Start-Spinner 'Running winget upgrade --all'
        try {
            & winget upgrade --all --silent --accept-package-agreements --accept-source-agreements --disable-interactivity >> $LOG_FILE 2>&1
            if ($LASTEXITCODE -ne 0) { throw "winget upgrade exit code $LASTEXITCODE" }
            Stop-Spinner
            ok 'winget upgrade --all completed'
            Set-StateEntry -Key 'packages' -Value 'INSTALLED'
        } catch {
            Stop-Spinner
            warn "winget upgrade --all failed: $($_.Exception.Message)"
            Set-StateEntry -Key 'packages' -Value 'PARTIAL'
        }
    }

    End-Phase

    ###############################################################################
    # PHASE 3 — DISPLAY & POWER CONFIGURATION
    ###############################################################################
    Begin-Phase

    Start-Spinner 'Setting High Performance power plan'
    try {
        & powercfg.exe /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c >> $LOG_FILE 2>&1
        if ($LASTEXITCODE -ne 0) { throw "powercfg exit code $LASTEXITCODE" }
        Stop-Spinner
        ok 'High Performance power plan enabled'
    } catch {
        Stop-Spinner
        warn "Could not set High Performance power plan: $($_.Exception.Message)"
    }

    Start-Spinner 'Disabling screen saver and lock-screen idle behavior'
    try {
        New-Item -Path 'HKCU:\Control Panel\Desktop' -Force | Out-Null
        Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -Force
        Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaveTimeOut -Value '0' -Force
        Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name ScreenSaverIsSecure -Value '0' -Force
        New-Item -Path 'Registry::HKEY_USERS\.DEFAULT\Control Panel\Desktop' -Force | Out-Null
        Set-ItemProperty -Path 'Registry::HKEY_USERS\.DEFAULT\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -Force
        New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
        New-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Name NoLockScreen -Value 1 -PropertyType DWord -Force | Out-Null
        Stop-Spinner
        ok 'Screen saver and lock-screen timeout settings disabled'
    } catch {
        Stop-Spinner
        warn "Failed to apply screen saver or lock-screen settings: $($_.Exception.Message)"
    }

    Start-Spinner 'Disabling sleep and hibernate on AC power'
    try {
        & powercfg.exe /change standby-timeout-ac 0 >> $LOG_FILE 2>&1
        & powercfg.exe /change hibernate-timeout-ac 0 >> $LOG_FILE 2>&1
        & powercfg.exe /h off >> $LOG_FILE 2>&1
        Stop-Spinner
        ok 'Sleep and hibernate disabled'
    } catch {
        Stop-Spinner
        warn "Failed to disable sleep or hibernate: $($_.Exception.Message)"
    }

    Start-Spinner 'Setting display resolution to 1920x1080'
    try {
        if (Get-Command Set-DisplayResolution -ErrorAction SilentlyContinue) {
            Set-DisplayResolution -Width 1920 -Height 1080 -Force
            Stop-Spinner
            ok 'Display resolution set to 1920x1080'
        } else {
            Stop-Spinner
            warn 'Set-DisplayResolution unavailable (Windows 10/11) — launch-terminals.ps1 will use native resolution'
        }
    } catch {
        Stop-Spinner
        warn "Display resolution change skipped: $($_.Exception.Message)"
    }

    # ── Windows AutoLogon (Registry) ─────────────────────────────────────────
    # Equivalent of LightDM autologin-user on Linux.
    # Sets AutoAdminLogon so the SIM_USER logs in automatically on boot.
    # NOTE: DefaultPassword is stored in plaintext — acceptable for lab/sim devices.
    Start-Spinner "Configuring AutoLogon for $SIM_USER"
    try {
        $winlogonPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        Set-ItemProperty -Path $winlogonPath -Name AutoAdminLogon    -Value '1'         -Force
        Set-ItemProperty -Path $winlogonPath -Name DefaultUserName    -Value $SIM_USER   -Force
        Set-ItemProperty -Path $winlogonPath -Name DefaultDomainName  -Value $env:COMPUTERNAME -Force
        Set-ItemProperty -Path $winlogonPath -Name DefaultPassword    -Value $SIM_PASS   -Force
        Stop-Spinner
        ok "AutoLogon configured for $SIM_USER"
        Set-StateEntry -Key 'autologon' -Value 'CONFIGURED'
    } catch {
        Stop-Spinner
        warn "AutoLogon configuration failed: $($_.Exception.Message)"
        Set-StateEntry -Key 'autologon' -Value 'FAILED'
    }

    # ── Scheduled Task — launch-terminals.ps1 at logon ───────────────────────
    # Equivalent of openbox autostart running launch-terminals.sh on Linux.
    # Runs as the SIM_USER at logon, launches and positions all terminal windows.
    Start-Spinner 'Registering ClientSim-Startup scheduled task'
    try {
        $launchScript = Join-Path $SCRIPTS_DIR 'launch-terminals.ps1'
        $action = New-ScheduledTaskAction `
            -Execute 'powershell.exe' `
            -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $launchScript)
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $SIM_USER
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable
        $principal = New-ScheduledTaskPrincipal `
            -UserId $SIM_USER `
            -LogonType Interactive `
            -RunLevel Highest

        Register-ScheduledTask `
            -TaskName 'ClientSim-Startup' `
            -TaskPath '\ClientSim\' `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Force | Out-Null

        Stop-Spinner
        ok 'ClientSim-Startup scheduled task registered (runs launch-terminals.ps1 at logon)'
        Set-StateEntry -Key 'startup-task' -Value 'REGISTERED'
    } catch {
        Stop-Spinner
        warn "Scheduled task registration failed: $($_.Exception.Message)"
        Set-StateEntry -Key 'startup-task' -Value 'FAILED'
    }

    Set-StateEntry -Key 'display-power' -Value 'CONFIGURED'
    End-Phase

    ###############################################################################
    # PHASE 4 — SCRIPTS DIRECTORY SETUP
    ###############################################################################
    Begin-Phase

    Start-Spinner 'Preparing C:\Scripts, C:\Logs, and C:\Temp'
    New-Item -ItemType Directory -Path $SCRIPTS_DIR, $LOG_DIR, $TEMP_DIR -Force | Out-Null
    & icacls.exe $SCRIPTS_DIR '/grant' ("${env:COMPUTERNAME}\${SIM_USER}:(OI)(CI)F") '/T' '/C' >> $LOG_FILE 2>&1
    if ($LASTEXITCODE -ne 0) {
        warn 'Failed to apply full-control permissions for sim-user on C:\Scripts'
    }
    Set-Content -Path $SIM_LOG -Value ("Installer Version v{0}" -f $VERSION) -Encoding UTF8
    Stop-Spinner
    ok ("C:\Scripts prepared — version v{0} written to sim.log" -f $VERSION)
    Set-StateEntry -Key 'scripts-dir' -Value 'READY'

    End-Phase

    ###############################################################################
    # PHASE 5 — CLIENT-SIM GITHUB REPO CLONE + FILE DEPLOYMENT
    ###############################################################################
    Begin-Phase

    $clientSimRepo = 'https://github.com/lbockenstedt/cs.git'
    $clientSimDir = Join-Path $HOME 'client-sim'
    $windowsDir = Join-Path $clientSimDir 'windows'
    $configsDir = Join-Path $clientSimDir 'configs'

    Start-Spinner 'Cloning lbockenstedt/cs'
    try {
        if (Test-Path $clientSimDir) {
            Remove-Item -Path $clientSimDir -Recurse -Force
        }
        Invoke-WithRetry -ScriptBlock {
            & git clone --depth=1 $clientSimRepo $clientSimDir >> $LOG_FILE 2>&1
            if ($LASTEXITCODE -ne 0) { throw "git clone exit code $LASTEXITCODE" }
        }
        Stop-Spinner
        ok 'client-sim repo cloned'
        Set-StateEntry -Key 'repo-clone' -Value 'OK'

        if (Test-Path $windowsDir) {
            Start-Spinner 'Copying PowerShell scripts to C:\Scripts'
            $psFiles = @(Get-ChildItem -Path $windowsDir -Filter '*.ps1' -File -ErrorAction SilentlyContinue)
            if ($psFiles.Count -gt 0) {
                Copy-Item -Path $psFiles.FullName -Destination $SCRIPTS_DIR -Force
                Stop-Spinner
                ok 'PowerShell scripts copied'
            } else {
                Stop-Spinner
                warn "No .ps1 files found in $windowsDir"
            }

            Start-Spinner 'Copying text files to C:\Scripts'
            $txtFiles = @(Get-ChildItem -Path $windowsDir -Filter '*.txt' -File -ErrorAction SilentlyContinue)
            if ($txtFiles.Count -gt 0) {
                Copy-Item -Path $txtFiles.FullName -Destination $SCRIPTS_DIR -Force
                Stop-Spinner
                ok 'Text files copied'
            } else {
                Stop-Spinner
                warn "No .txt files found in $windowsDir"
            }
        } else {
            Stop-Spinner
            warn "windows directory not found in $clientSimDir — skipping file deployment"
        }

        Start-Spinner 'Checking simulation.conf'
        $targetSimConf = Join-Path $SCRIPTS_DIR 'simulation.conf'
        $sourceSimConf = Join-Path $configsDir 'simulation.conf'
        if (Test-Path $targetSimConf) {
            Stop-Spinner
            ok 'simulation.conf already exists — not overwriting'
        } elseif (Test-Path $sourceSimConf) {
            Copy-Item -Path $sourceSimConf -Destination $targetSimConf -Force
            Stop-Spinner
            ok 'simulation.conf copied from configs directory'
        } else {
            Stop-Spinner
            warn 'simulation.conf not found in repo — skipping'
        }

        Start-Spinner 'Setting execution policy and PATH'
        try {
            Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope LocalMachine -Force
            Ensure-SystemPathEntry -PathEntry $SCRIPTS_DIR
            Stop-Spinner
            ok 'Execution policy updated and C:\Scripts added to PATH'
            if (Test-Path $targetSimConf) {
                Set-StateEntry -Key 'simulation-conf' -Value 'PRESENT'
            } else {
                Set-StateEntry -Key 'simulation-conf' -Value 'MISSING'
            }
        } catch {
            Stop-Spinner
            warn "Execution policy or PATH update failed: $($_.Exception.Message)"
        }
    } catch {
        Stop-Spinner
        warn "Failed to clone client-sim repo or deploy files: $($_.Exception.Message)"
        Set-StateEntry -Key 'repo-clone' -Value 'FAILED'
    }

    End-Phase

    ###############################################################################
    # PHASE 6 — SMB CONFIG SYNC
    ###############################################################################
    Begin-Phase

    if (-not (Test-Path $SMB_CREDS_FILE)) {
        warn "SMB credentials file not found at $SMB_CREDS_FILE — skipping SMB sync"
        warn 'Create the file with: username=..., password=..., domain=...'
        Set-StateEntry -Key 'smb-sync' -Value 'SKIPPED'
    } else {
        Start-Spinner 'Syncing config files from SMB share'
        try {
            $credMap = @{}
            foreach ($line in Get-Content -Path $SMB_CREDS_FILE) {
                if ($line -match '^\s*([^=]+?)\s*=\s*(.*)$') {
                    $credMap[$matches[1].Trim()] = $matches[2].Trim()
                }
            }

            $userName = $credMap['username']
            $password = $credMap['password']
            $domain = $credMap['domain']
            if (-not $userName -or -not $password) {
                throw 'username and password are required in smb-credentials.txt'
            }

            $qualifiedUser = if ($domain) { "$domain\$userName" } else { $userName }
            $securePassword = ConvertTo-SecureString $password -AsPlainText -Force
            $credential = New-Object pscredential($qualifiedUser, $securePassword)

            if (Get-PSDrive -Name SimSMB -ErrorAction SilentlyContinue) {
                Remove-PSDrive -Name SimSMB -Force -ErrorAction SilentlyContinue
            }
            New-PSDrive -Name SimSMB -PSProvider FileSystem -Root '\\nas\scripts' -Credential $credential -Scope Script | Out-Null
            $confFiles = @(Get-ChildItem -Path 'SimSMB:\' -Filter '*.conf' -File -ErrorAction SilentlyContinue)
            if ($confFiles.Count -gt 0) {
                Copy-Item -Path $confFiles.FullName -Destination $SCRIPTS_DIR -Force
                Stop-Spinner
                ok 'SMB config sync complete'
                info 'Checksum verification intentionally skipped to match install.sh behavior'
                Set-StateEntry -Key 'smb-sync' -Value 'OK'
            } else {
                Stop-Spinner
                warn 'No .conf files found on SMB share'
                Set-StateEntry -Key 'smb-sync' -Value 'EMPTY'
            }
        } catch {
            Stop-Spinner
            warn "SMB config sync failed — continuing without remote config: $($_.Exception.Message)"
            Set-StateEntry -Key 'smb-sync' -Value 'FAILED'
        } finally {
            if (Get-PSDrive -Name SimSMB -ErrorAction SilentlyContinue) {
                Remove-PSDrive -Name SimSMB -Force -ErrorAction SilentlyContinue
            }
        }
    }

    End-Phase

    ###############################################################################
    # PHASE 7 — EVENT LOG / LOGGING SETUP
    ###############################################################################
    Begin-Phase

    Start-Spinner 'Creating ClientSim Windows Event Log source'
    try {
        if (-not [System.Diagnostics.EventLog]::SourceExists('ClientSim')) {
            New-EventLog -LogName Application -Source 'ClientSim'
        }
        New-Item -Path $EVENT_REG_PATH -Force | Out-Null
        New-ItemProperty -Path $EVENT_REG_PATH -Name SimLogPath -Value $SIM_LOG -PropertyType String -Force | Out-Null
        New-ItemProperty -Path $EVENT_REG_PATH -Name LoggingBackend -Value 'Windows Event Log' -PropertyType String -Force | Out-Null
        Add-Content -Path $SIM_LOG -Value 'Windows Event Log is used instead of rsyslog.' -Encoding UTF8
        Stop-Spinner
        ok 'ClientSim Event Log source configured'
        Set-StateEntry -Key 'event-log' -Value 'READY'
    } catch {
        Stop-Spinner
        warn "Failed to configure Windows Event Log source: $($_.Exception.Message)"
        Set-StateEntry -Key 'event-log' -Value 'FAILED'
    }

    End-Phase

    ###############################################################################
    # PHASE 8 — VIRTUALHERE INSTALL
    ###############################################################################
    Begin-Phase
    info 'Installing VirtualHere'

    $arch = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE')
    $vhBinary = $null
    switch -Regex ($arch) {
        'AMD64|x86_64' { $vhBinary = 'vhclientx86_64.exe'; break }
        'ARM64' { $vhBinary = 'vhclientarm64.exe'; break }
        default { warn "Unsupported architecture '$arch' for VirtualHere — skipping" }
    }

    if ($vhBinary) {
        $vhUrl = "https://www.virtualhere.com/sites/default/files/usbclient/$vhBinary"
        $vhTarget = Join-Path $VH_DIR $vhBinary
        $vhLink = Join-Path $VH_DIR 'vhclient.exe'

        Start-Spinner "Downloading VirtualHere ($vhBinary)"
        try {
            New-Item -ItemType Directory -Path $VH_DIR, $TEMP_DIR -Force | Out-Null
            Invoke-WithRetry -ScriptBlock {
                Download-File -Url $vhUrl -Destination $vhTarget
            }
            Stop-Spinner
            ok "VirtualHere binary installed as $vhTarget"

            Start-Spinner 'Creating VirtualHere vhclient.exe shortcut'
            try {
                if (Test-Path $vhLink) { Remove-Item -Path $vhLink -Force }
                New-Item -ItemType SymbolicLink -Path $vhLink -Target $vhTarget -Force | Out-Null
                Stop-Spinner
                ok 'VirtualHere symlink created'
            } catch {
                Copy-Item -Path $vhTarget -Destination $vhLink -Force
                Stop-Spinner
                warn 'Symlink creation failed — copied binary to vhclient.exe instead'
            }

            Start-Spinner 'Adding VirtualHere to system PATH'
            Ensure-SystemPathEntry -PathEntry $VH_DIR
            Stop-Spinner
            ok 'VirtualHere path registered'

            Start-Spinner 'Installing or refreshing VirtualHere service'
            & $vhTarget -b >> $LOG_FILE 2>&1
            Stop-Spinner

            $service = Get-Service -Name 'VirtualHereClient' -ErrorAction SilentlyContinue
            if (-not $service) {
                $service = Get-Service | Where-Object { $_.Name -match 'VirtualHere' -or $_.DisplayName -match 'VirtualHere' } | Select-Object -First 1
            }
            if ($service) {
                Start-Spinner 'Starting VirtualHere service'
                Start-Service -Name $service.Name -ErrorAction SilentlyContinue
                Stop-Spinner
                ok ("VirtualHere service active: {0}" -f $service.Name)
            } else {
                warn 'VirtualHere service was not detected after running -b'
            }

            Start-Spinner 'Initializing VirtualHere client state'
            & $vhTarget -t 'AUTO USE CLEAR ALL' >> $LOG_FILE 2>&1
            & $vhTarget -t 'STOP USING ALL LOCAL' >> $LOG_FILE 2>&1
            Stop-Spinner
            ok 'VirtualHere installed and initialized'
            Set-StateEntry -Key 'virtualhere' -Value 'INSTALLED'
        } catch {
            Stop-Spinner
            warn "Failed to install VirtualHere: $($_.Exception.Message)"
            Set-StateEntry -Key 'virtualhere' -Value 'FAILED'
        }
    }

    End-Phase

    ###############################################################################
    # PHASE 9 — DRIVER CHECK
    ###############################################################################
    Begin-Phase

    $driverChecks = @(
        @{ Key = '8821au'; Pattern = '8821AU|RTL8811AU|RTL8821AU' },
        @{ Key = '8821cu'; Pattern = '8821CU|RTL8821CU' },
        @{ Key = '8814au'; Pattern = '8814AU|RTL8814AU' },
        @{ Key = '8812au'; Pattern = '8812AU|RTL8812AU' },
        @{ Key = 'rtl8852bu'; Pattern = '8852BU|RTL8852BU' },
        @{ Key = 'rtl8852cu'; Pattern = '8852CU|RTL8852CU' },
        @{ Key = '88x2bu'; Pattern = '88X2BU|RTL88X2BU' },
        @{ Key = 'rtw89'; Pattern = 'RTW89|8852AE|8852BE|8851BE' },
        @{ Key = 'rtl8812au'; Pattern = 'RTL8812AU' },
        @{ Key = 'rtl8188eu'; Pattern = '8188EU|RTL8188EU' },
        @{ Key = 'rtl8723au'; Pattern = '8723AU|RTL8723AU' },
        @{ Key = 'rtl8852au'; Pattern = '8852AU|RTL8852AU' }
    )

    Start-Spinner 'Triggering Windows driver scan'
    try {
        & pnputil.exe /scan-devices >> $LOG_FILE 2>&1
        Stop-Spinner
        ok 'Windows driver scan completed'
    } catch {
        Stop-Spinner
        warn "Driver scan failed: $($_.Exception.Message)"
    }

    $driverInfRoot = Join-Path $SCRIPTS_DIR 'Drivers'
    if (Test-Path $driverInfRoot) {
        Start-Spinner 'Installing any bundled driver INF files'
        try {
            & pnputil.exe /add-driver (Join-Path $driverInfRoot '*.inf') /install /subdirs >> $LOG_FILE 2>&1
            Stop-Spinner
            ok 'Bundled driver INF scan completed'
        } catch {
            Stop-Spinner
            warn "Bundled INF install attempt failed: $($_.Exception.Message)"
        }
    }

    $pnpDevices = Get-PnpDevicesSafe
    $totalDrivers = $driverChecks.Count
    $driverIndex = 0

    foreach ($driver in $driverChecks) {
        $driverIndex++
        Phase-Step -Step $driverIndex -Total $totalDrivers
        $matches = @($pnpDevices | Where-Object {
            ($_.FriendlyName -as [string]) -match $driver.Pattern -or
            ($_.Name -as [string]) -match $driver.Pattern -or
            ($_.InstanceId -as [string]) -match $driver.Pattern -or
            ($_.DeviceID -as [string]) -match $driver.Pattern
        })

        if ($matches.Count -eq 0) {
            warn ("{0}: adapter not present" -f $driver.Key)
            Set-StateEntry -Key $driver.Key -Value 'NOT_PRESENT'
            continue
        }

        $healthy = $false
        foreach ($match in $matches) {
            $statusText = ($match.Status -as [string])
            $cmError = $match.ConfigManagerErrorCode
            if ($statusText -eq 'OK' -or $cmError -eq 0 -or $null -eq $cmError) {
                $healthy = $true
                break
            }
        }

        if ($healthy) {
            ok ("{0}: driver check OK" -f $driver.Key)
            Set-StateEntry -Key $driver.Key -Value 'CHECK_OK'
        } else {
            warn ("{0}: driver missing or unhealthy" -f $driver.Key)
            Set-StateEntry -Key $driver.Key -Value 'DRIVER_MISSING'
        }
    }

    End-Phase

    ###############################################################################
    # PHASE 10 — FINAL HEALTH SUMMARY
    ###############################################################################
    Begin-Phase
    Write-Host ''

    function _hc_ok {
        param([string]$Label)
        $line = "  ${COL_GREEN}✓${COL_RESET}  {0,-24} ${COL_GREEN}OK${COL_RESET}" -f $Label
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }
    function _hc_warn {
        param([string]$Label, [string]$State)
        $line = "  ${COL_YELLOW}✗${COL_RESET}  {0,-24} ${COL_YELLOW}{1}${COL_RESET}" -f $Label, $State
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }
    function _hc_fail {
        param([string]$Label, [string]$State)
        $line = "  ${COL_RED}✗${COL_RESET}  {0,-24} ${COL_RED}{1}${COL_RESET}" -f $Label, $State
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }
    function _hc_drv_ok {
        param([string]$Label)
        $line = "  ${COL_GREEN}✓${COL_RESET}  {0,-35} ${COL_GREEN}CHECK_OK${COL_RESET}" -f $Label
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }
    function _hc_drv_warn {
        param([string]$Label, [string]$State)
        $line = "  ${COL_YELLOW}✗${COL_RESET}  {0,-35} ${COL_YELLOW}{1}${COL_RESET}" -f $Label, $State
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }
    function _hc_drv_fail {
        param([string]$Label, [string]$State)
        $line = "  ${COL_RED}✗${COL_RESET}  {0,-35} ${COL_RED}{1}${COL_RESET}" -f $Label, $State
        Write-Host $line
        Add-Content -Path $LOG_FILE -Value ($line -replace [regex]::Escape($ESC) + '\[[0-9;?]*[A-Za-z]', '') -Encoding UTF8
    }

    $summaryHeader = '================ HEALTH CHECK ================'
    Write-Host $summaryHeader
    Add-Content -Path $LOG_FILE -Value $summaryHeader -Encoding UTF8

    if (Test-LocalUserExists -UserName $SIM_USER) { _hc_ok "User ($SIM_USER)" } else { _hc_fail "User ($SIM_USER)" 'MISSING' }
    if (Test-LocalGroupMembership -GroupName 'Remote Desktop Users' -UserName $SIM_USER) { _hc_ok 'Remote Desktop Users' } else { _hc_warn 'Remote Desktop Users' 'NOT IN GROUP' }
    if ((Test-PowerSettingDisabled -Args @('/query','SCHEME_CURRENT','SUB_SLEEP','STANDBYIDLE')) -and (Test-PowerSettingDisabled -Args @('/query','SCHEME_CURRENT','SUB_SLEEP','HIBERNATEIDLE'))) { _hc_ok 'Sleep/Hibernate' } else { _hc_warn 'Sleep/Hibernate' 'NOT DISABLED' }

    $vhService = Get-Service -Name 'VirtualHereClient' -ErrorAction SilentlyContinue
    if (-not $vhService) {
        $vhService = Get-Service | Where-Object { $_.Name -match 'VirtualHere' -or $_.DisplayName -match 'VirtualHere' } | Select-Object -First 1
    }
    if ($vhService -and $vhService.Status -eq 'Running') { _hc_ok 'VirtualHere' } else { _hc_warn 'VirtualHere' 'NOT ACTIVE' }

    $wifiAdapters = Get-WirelessAdaptersSafe
    if ($wifiAdapters.Count -gt 0) { _hc_ok 'Wi-Fi adapters' } else { _hc_warn 'Wi-Fi adapters' 'NOT FOUND' }
    if (Test-Path (Join-Path $SCRIPTS_DIR 'simulation.conf')) { _hc_ok 'simulation.conf' } else { _hc_fail 'simulation.conf' 'MISSING' }
    if ([System.Diagnostics.EventLog]::SourceExists('ClientSim')) { _hc_ok 'Event Log source' } else { _hc_fail 'Event Log source' 'MISSING' }
    if (Test-Path (Join-Path $SCRIPTS_DIR 'launch-terminals.ps1')) { _hc_ok 'launch-terminals.ps1' } else { _hc_fail 'launch-terminals.ps1' 'MISSING' }

    # AutoLogon health check
    try {
        $winlogonPath = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
        $autoAdminLogon = (Get-ItemProperty -Path $winlogonPath -Name AutoAdminLogon -ErrorAction SilentlyContinue).AutoAdminLogon
        if ($autoAdminLogon -eq '1') { _hc_ok 'AutoLogon' } else { _hc_warn 'AutoLogon' 'NOT SET' }
    } catch { _hc_warn 'AutoLogon' 'NOT SET' }

    # Startup scheduled task health check
    $startupTask = Get-ScheduledTask -TaskName 'ClientSim-Startup' -TaskPath '\ClientSim\' -ErrorAction SilentlyContinue
    if ($startupTask) { _hc_ok 'Startup Task' } else { _hc_warn 'Startup Task' 'NOT REGISTERED' }

    Write-Host ''
    Add-Content -Path $LOG_FILE -Value '' -Encoding UTF8
    Write-Host '  ---- Driver State ----'
    Add-Content -Path $LOG_FILE -Value '  ---- Driver State ----' -Encoding UTF8
    foreach ($line in Get-Content -Path $STATE_FILE -ErrorAction SilentlyContinue) {
        if ($line -match '^(?<drv>[^:]+):(?<status>.+)$') {
            $drv = $matches['drv']
            $status = $matches['status']
            switch ($status) {
                'CHECK_OK' { _hc_drv_ok $drv }
                'DRIVER_MISSING' { _hc_drv_fail $drv 'DRIVER_MISSING' }
                'NOT_PRESENT' { _hc_drv_warn $drv 'NOT_PRESENT' }
                default { }
            }
        }
    }

    $summaryFooter = '============================================='
    Write-Host $summaryFooter
    Add-Content -Path $LOG_FILE -Value $summaryFooter -Encoding UTF8

    End-Phase

    Draw-Bar -Percent 100 -Label 'Complete'
    Write-Host ("  {0}✓{1}" -f $COL_GREEN, $COL_RESET)
    Write-Host ''

    $totalElapsed = [int]((Get-Date) - $INSTALL_START).TotalSeconds
    $elapsedMin = [int]($totalElapsed / 60)
    $elapsedSec = $totalElapsed % 60

    $finalLines = @(
        '============================================================',
        ' Installation complete — reboot recommended',
        (' Total time : {0}m {1:d2}s' -f $elapsedMin, $elapsedSec),
        (' Warnings   : {0}{1}' -f $WARN_COUNT, $(if ($WARN_COUNT -gt 0) { "  (see $LOG_FILE)" } else { '' })),
        (' Errors     : {0}{1}' -f $ERR_COUNT, $(if ($ERR_COUNT -gt 0) { "  (see $LOG_FILE)" } else { '' })),
        (" Full log   : {0}" -f $LOG_FILE),
        (" Driver state: {0}" -f $STATE_FILE),
        (" Sim log    : {0}" -f $SIM_LOG),
        '============================================================'
    )

    foreach ($line in $finalLines) {
        Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
        if ($line -match '^ Warnings' -and $WARN_COUNT -gt 0) {
            Write-Host $line -ForegroundColor Yellow
        } elseif ($line -match '^ Errors' -and $ERR_COUNT -gt 0) {
            Write-Host $line -ForegroundColor Red
        } elseif ($line -match 'complete') {
            Write-Host $line -ForegroundColor Green
        } else {
            Write-Host $line
        }
    }

    if (Test-Path (Join-Path $TEMP_DIR 'virtio-win-guest-tools.exe')) {
        Remove-Item -Path (Join-Path $TEMP_DIR 'virtio-win-guest-tools.exe') -Force -ErrorAction SilentlyContinue
    }
} catch {
    Stop-Spinner
    err $_.Exception.Message
    exit 1
} finally {
    Stop-Spinner
    Write-Host -NoNewline $CURSOR_SHOW
    if ($cleanupEvent) {
        Unregister-Event -SourceIdentifier PowerShell.Exiting -ErrorAction SilentlyContinue
    }
}
