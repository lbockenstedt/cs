# PowerShell script to monitor simulation logs in a new window
# Equivalent to tail -f /usr/local/scripts/sim.log in Linux

# Open a new Windows Terminal tab for monitoring with specific size and position
Start-Process "wt.exe" -ArgumentList "new-tab", "--title", "Simulation Log Monitor", "--size", "35,15", "--pos", "0,525", "powershell.exe", "-NoExit", "-Command", "Get-Content -Path 'C:\Scripts\sim.log' -Wait"
