# ============================================================================ #
# collab.ps1 — collaboration-app UDP media generator (Teams / Zoom / WebEx).    #
#                                                                               #
# PowerShell port of clients/linux/collab.sh + clients/linux/collab.py.         #
# The hub runs a matching UDP sink (lm-collab-sink) bound to its LAN IP on the   #
# same ports; this sender blasts raw UDP at it over the network path — the       #
# media NEVER rides the WebSocket/API control plane (only on/off + config does). #
#                                                                               #
# Per-app flow profiles (port set + payload size + default bw). The ports are    #
# the real media/control ports each platform uses, so a monitor classifying by   #
# 5-tuple sees the right signature — identical to collab.py:                     #
#                                                                               #
#     teams : 3478, 3481, 3479  (STUN + media)   default 1.2M                    #
#     zoom  : 8801, 8802, 8803  (media)          default 1.5M                    #
#     webex : 9000, 5004, 5006  (SRTP media)     default 1.0M                    #
#                                                                               #
# Bandwidth is shared across the port set (round-robin per datagram). One UDP    #
# socket per port so each 5-tuple is distinct on the wire.                       #
#                                                                               #
# Target: Windows PowerShell 5.1 (Desktop).                                     #
# ============================================================================ #
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

$version = '0.01'
$debugPath = 'C:\Scripts\debug-collab.log'

"Collab Script Version $version" | Tee-Object -FilePath $debugPath | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Out-Null

$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'collab' $version

# App -> (ports, payload bytes, default bandwidth bps) — mirror collab.py.
$appProfiles = @{
    'teams' = @{ ports = @(3478, 3481, 3479); payload = 1280; bw = 1200000 }
    'zoom'  = @{ ports = @(8801, 8802, 8803); payload = 1280; bw = 1500000 }
    'webex' = @{ ports = @(9000, 5004, 5006); payload = 1280; bw = 1000000 }
}

# ── config + per-user overrides ──────────────────────────────────────────────
# Bash collab.sh sources ini-parser then applies user-overrides for collab_*.
$collabServer = get_value 'address' 'collab_server'
$collabApp    = get_value 'simulation' 'collab_app'
$collabBw     = get_value 'simulation' 'collab_bw'
$collabTime   = get_value 'simulation' 'collab_time'

$overrides = Get-SimOverrides
if ($overrides.ContainsKey('collab_server')) { $collabServer = $overrides['collab_server'] }
if ($overrides.ContainsKey('collab_app'))    { $collabApp    = $overrides['collab_app'] }
if ($overrides.ContainsKey('collab_bw'))     { $collabBw     = $overrides['collab_bw'] }
if ($overrides.ContainsKey('collab_time'))   { $collabTime   = $overrides['collab_time'] }

# No sink configured -> no-op cleanly (mirror collab.sh).
if ([string]::IsNullOrWhiteSpace($collabServer)) {
    Write-SimLog "$(Get-Date) collab: no collab_server configured — skipping"
    exit 0
}

# App profile (default teams; unknown app falls back to teams).
if ([string]::IsNullOrWhiteSpace($collabApp)) { $collabApp = 'teams' }
$collabApp = $collabApp.ToLower()
if (-not $appProfiles.ContainsKey($collabApp)) { $collabApp = 'teams' }
$appProfile = $appProfiles[$collabApp]
$ports       = $appProfile.ports
$payloadSize = $appProfile.payload

# ── bandwidth: '1.5M' / '500k' / '2000000' -> bits per second (mirror parse_bw)
function ConvertFrom-CollabBw {
    param([string]$Text)
    $s = ("$Text").Trim().ToLower()
    if ([string]::IsNullOrEmpty($s)) { return $null }
    $mult = 1
    $last = $s.Substring($s.Length - 1, 1)
    if ($last -eq 'k') { $mult = 1000; $s = $s.Substring(0, $s.Length - 1) }
    elseif ($last -eq 'm') { $mult = 1000000; $s = $s.Substring(0, $s.Length - 1) }
    elseif ($last -eq 'g') { $mult = 1000000000; $s = $s.Substring(0, $s.Length - 1) }
    $num = 0.0
    if (-not [double]::TryParse($s, [ref]$num)) { return $null }
    return [int]($num * $mult)
}
$bw = ConvertFrom-CollabBw $collabBw
if (-not $bw -or $bw -le 0) { $bw = $appProfile.bw }

# ── run window: collab_time seconds; empty -> random 1..300; 0 -> until killed ─
if ([string]::IsNullOrWhiteSpace($collabTime)) {
    $runTime = Get-Random -Minimum 1 -Maximum 301
} else {
    $runTime = [int]$collabTime
}

# datagrams/sec needed to hit bw with this payload size (mirror collab.py).
$dgramHz  = [math]::Max(1.0, $bw / ($payloadSize * 8))
$interval = 1.0 / $dgramHz
$intervalMs = [int][math]::Round($interval * 1000)

Write-SimLog ("$(Get-Date) collab: app=$collabApp server=$collabServer ports=$($ports -join ',') " +
              "bw=${bw}bps payload=${payloadSize}B interval=$([math]::Round($interval * 1000, 1))ms " +
              "time=$(if ($runTime -eq 0) { 'until-killed' } else { $runTime })")

# One UDP socket per port so each 5-tuple is distinct on the wire.
$socks = @{}
foreach ($p in $ports) {
    $u = New-Object System.Net.Sockets.UdpClient
    try { $u.Connect($collabServer, $p) } catch {}
    $socks[$p] = $u
}

# Random media-shaped payload (opaque bytes — DPI keys on the 5-tuple, not content).
$payload = New-Object 'System.Byte[]' $payloadSize
(New-Object System.Random).NextBytes($payload)

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$portIdx = 0
$sent = 0
try {
    while ($runTime -eq 0 -or $sw.Elapsed.TotalSeconds -lt $runTime) {
        $p = $ports[$portIdx % $ports.Count]
        try { [void]$socks[$p].Send($payload, $payload.Length); $sent++ } catch {}
        $portIdx++
        if ($intervalMs -gt 0) { Start-Sleep -Milliseconds $intervalMs }
    }
} finally {
    foreach ($u in $socks.Values) { try { $u.Close() } catch {} }
    $dur = [math]::Max($sw.Elapsed.TotalSeconds, 0.000001)
    $mbps = [math]::Round($sent * $payloadSize * 8 / $dur / 1000000, 2)
    Write-SimLog "$(Get-Date) collab: sent $sent datagrams in $([math]::Round($dur, 1))s (~$mbps Mbps across $($ports.Count) ports)"
}
