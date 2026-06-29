# PowerShell script to monitor system logs in a new window
# Equivalent to journalctl -f in Linux

# Open a new Windows Terminal tab for monitoring with specific size and position
Start-Process "wt.exe" -ArgumentList "new-tab", "--title", "System Log Monitor", "--size", "140,20", "--pos", "0,0", "powershell.exe", "-NoExit", "-Command", @"
while (`$true) {
    Get-WinEvent -LogName System -MaxEvents 10 | Select-Object TimeCreated, LevelDisplayName, Message | Format-Table -AutoSize
    Start-Sleep 5
}
"@
