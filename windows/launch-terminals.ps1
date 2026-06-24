# launch-terminals.ps1 v0.02
# Windows equivalent of launch-terminals.sh
# Launches and positions PowerShell console windows for client-sim.
# Called by the ClientSim-Startup scheduled task at logon.
#
# Layout designed for 1920x1080 — positions scale proportionally to actual resolution.
#
#  x=0%           x=26%                          x=73%
#  ┌─────────────┬──────────────────────────────┬──────────────────┐  y=0
#  │  Dashboard  │  Event Log (live)            │                  │
#  │  (full ht)  ├──────────────────────────────┤  Simulation      │  y=49%
#  │             │                              │  (startup.ps1)   │
#  └─────────────┴──────────────────────────────┴──────────────────┘  y=100%

$SCRIPTS = 'C:\Scripts'
$LOG     = "$SCRIPTS\sim.log"

function Write-LaunchLog {
    param([string]$Msg)
    $line = "$(Get-Date -Format 'HH:mm:ss') launch-terminals: $Msg"
    Add-Content -Path $LOG -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

# ── Detect actual screen resolution ─────────────────────────────────────────
# Works on all Windows versions — no external tools needed.
Add-Type -AssemblyName System.Windows.Forms
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$SW = $screen.Width
$SH = $screen.Height
Write-LaunchLog "Screen resolution: ${SW}x${SH}"

# ── Win32 API — window positioning ──────────────────────────────────────────
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class SimWin32 {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    public const int SW_RESTORE = 9;
}
'@

# ── Calculate proportional pixel positions ───────────────────────────────────
# Baseline 1920x1080 offsets (matching Linux .desktop layout):
#   Journal X  : 500  px → 26.04%  of 1920
#   Startup X  : 1400 px → 72.92%  of 1920
#   Startup Y  : 525  px → 48.61%  of 1080
$JOUR_X  = [int]($SW * 500  / 1920)
$START_X = [int]($SW * 1400 / 1920)
$START_Y = [int]($SH * 525  / 1080)

# Window pixel dimensions
# Dashboard: left column — 26% wide, full height
$DASH_W  = $JOUR_X
$DASH_H  = $SH

# Event log: center — remaining width to startup, top 49% height
$JOUR_W  = $START_X - $JOUR_X
$JOUR_H  = $START_Y

# Simulation: right column — remaining width, lower 51%
$START_W = $SW - $START_X
$START_H = $SH - $START_Y

Write-LaunchLog ("Dashboard  : {0}x{1} at +0+0"             -f $DASH_W,  $DASH_H)
Write-LaunchLog ("Event Log  : {0}x{1} at +{2}+0"           -f $JOUR_W,  $JOUR_H,  $JOUR_X)
Write-LaunchLog ("Simulation : {0}x{1} at +{2}+{3}"         -f $START_W, $START_H, $START_X, $START_Y)

# ── Helper: launch a PowerShell window, wait for its handle, then position it ─
function Start-SimWindow {
    param(
        [string]$Title,
        [string]$ScriptPath,
        [string]$Command,     # alternative to ScriptPath for inline commands
        [int]$X,
        [int]$Y,
        [int]$Width,
        [int]$Height
    )

    if ($ScriptPath) {
        $args = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$ScriptPath`""
    } else {
        $args = "-NoProfile -ExecutionPolicy Bypass -NoExit -Command `"$Command`""
    }

    try {
        $proc = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList $args `
            -PassThru -WindowStyle Normal -ErrorAction Stop

        # Poll for the main window handle — it takes a moment to appear
        $handle = [IntPtr]::Zero
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 400
            $proc.Refresh()
            if ($proc.MainWindowHandle -ne [IntPtr]::Zero) {
                $handle = $proc.MainWindowHandle
                break
            }
        }

        if ($handle -ne [IntPtr]::Zero) {
            [SimWin32]::ShowWindow($handle, [SimWin32]::SW_RESTORE) | Out-Null
            [SimWin32]::MoveWindow($handle, $X, $Y, $Width, $Height, $true) | Out-Null
            Write-LaunchLog "Opened: $Title (PID $($proc.Id))"
        } else {
            Write-LaunchLog "WARNING: could not get window handle for $Title — window opened but not positioned"
        }

        return $proc
    } catch {
        Write-LaunchLog "WARNING: failed to open $Title — $($_.Exception.Message)"
        return $null
    }
}

# ── Small delay to let the desktop settle after logon ───────────────────────
Start-Sleep -Seconds 3

# ── Dashboard — left column, full height ─────────────────────────────────────
Start-SimWindow -Title 'Dashboard' `
    -ScriptPath "$SCRIPTS\dashboard.ps1" `
    -X 0 -Y 0 -Width $DASH_W -Height $DASH_H | Out-Null

# ── Event log viewer — center top (equivalent of journalctl -f on Linux) ─────
$eventLogCmd = @'
while ($true) {
    Clear-Host
    Write-Host "=== ClientSim Event Log (live) ===" -ForegroundColor Cyan
    Get-EventLog -LogName Application -Source ClientSim -Newest 40 -ErrorAction SilentlyContinue |
        Select-Object TimeGenerated, EntryType, Message |
        Format-Table -AutoSize -Wrap
    Start-Sleep -Seconds 5
}
'@
Start-SimWindow -Title 'Event Log' `
    -Command $eventLogCmd `
    -X $JOUR_X -Y 0 -Width $JOUR_W -Height $JOUR_H | Out-Null

# ── Simulation / Startup — right column, lower half ──────────────────────────
# Reboots on completion (matches Linux startup.desktop behaviour)
$startupCmd = "& '$SCRIPTS\startup.ps1'; shutdown /r /t 30"
Start-SimWindow -Title 'Simulation' `
    -Command $startupCmd `
    -X $START_X -Y $START_Y -Width $START_W -Height $START_H | Out-Null

Write-LaunchLog 'All terminal windows launched'
