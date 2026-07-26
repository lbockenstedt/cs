# ============================================================================ #
# common.ps1 — shared helpers for the Windows sim client scripts.               #
#                                                                               #
# PowerShell port of clients/linux/common.sh (canonical: clients/lib/common.sh).#
# Keep the two in functional parity — every helper here mirrors a bash helper   #
# of (almost) the same name so behavior is identical across platforms.          #
#                                                                               #
# Deployed flat to C:\Scripts\common.ps1 (the /api/scripts/<platform> list, the #
# GitHub windows\*.ps1 sync and install all ship every top-level .ps1, so it    #
# always travels with the scripts that dot-source it). Dot-source AFTER         #
# ini-parser.ps1 (Apply-SimOverride reads the parsed config via get_value) and  #
# AFTER $global:iniConfig has been populated by Parse-IniFile.                   #
#                                                                               #
# Target: Windows PowerShell 5.1 (Desktop) — avoid 7-only syntax/params.        #
# ============================================================================ #
$version = '0.01'

$script:ScriptsRoot = 'C:\Scripts'
$script:SimLog      = Join-Path $script:ScriptsRoot 'sim.log'

# ── logging ─────────────────────────────────────────────────────────────────
function Write-SimLog {
    param([string]$Message)
    try { Add-Content -LiteralPath $script:SimLog -Value $Message -ErrorAction SilentlyContinue } catch {}
    Write-Host $Message
}

# ── script version reporting ─────────────────────────────────────────────────
# Mirrors common.sh _sim_deploy_version / sim_version_banner / sim_versions_report.
# The deployed VERSION file (C:\Scripts\VERSION) is CI/spoke-maintained and can't
# drift, so it anchors each script's hand-bumped $version.
function Get-SimDeployVersion {
    $v = ''
    try { $v = (Get-Content -LiteralPath (Join-Path $script:ScriptsRoot 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1) } catch {}
    if ([string]::IsNullOrWhiteSpace($v)) { '?' } else { $v.Trim() }
}
function Write-SimVersionBanner {
    param([string]$Name = 'script', [string]$Version = '?')
    Write-SimLog "[$Name v$Version . deploy $(Get-SimDeployVersion)] running"
}
function Write-SimVersionsReport {
    Write-SimLog "=== client script versions (deploy $(Get-SimDeployVersion)) ==="
    Get-ChildItem -LiteralPath $script:ScriptsRoot -Filter '*.ps1' -ErrorAction SilentlyContinue | Sort-Object Name | ForEach-Object {
        $v = '-'
        $m = Select-String -LiteralPath $_.FullName -Pattern '^\$version\s*=\s*[''"]?([^''"\s]+)' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $v = $m.Matches[0].Groups[1].Value }
        Write-SimLog ("  {0,-24} v{1}" -f $_.Name, $v)
    }
}

# ── username ─────────────────────────────────────────────────────────────────
# $username = hostname prefix before the first '-' (whole name when no dash).
# Mirrors common.sh derive_username (${HOSTNAME%%-*}).
function Get-SimUsername {
    $h = $env:COMPUTERNAME
    if ([string]::IsNullOrEmpty($h)) { $h = [System.Net.Dns]::GetHostName() }
    ($h -split '-', 2)[0]
}

# ── s0-s9 bucket ─────────────────────────────────────────────────────────────
# $bucket (0-9) via zlib.crc32(hostname) % 10 — MUST stay identical to
# sim_config.bucket_for() on the spoke AND to the Linux client (common.sh:72).
# The old Windows code used SHA256(host)%10, which put the SAME host in a
# DIFFERENT bucket than Linux/the spoke — fixed here to crc32.
$script:BucketCache = Join-Path $script:ScriptsRoot 'client-sim-bucket.cache'
function Get-Crc32 {
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    # Standard IEEE/zlib CRC32 (reflected, poly 0xEDB88320, init/xorout 0xFFFFFFFF).
    $table = New-Object 'System.UInt32[]' 256
    for ($i = 0; $i -lt 256; $i++) {
        [uint32]$c = [uint32]$i
        for ($k = 0; $k -lt 8; $k++) {
            if ($c -band 1) { $c = (0xEDB88320 -bxor ($c -shr 1)) } else { $c = ($c -shr 1) }
        }
        $table[$i] = $c
    }
    [uint32]$crc = 0xFFFFFFFF
    foreach ($b in $bytes) {
        $crc = ($table[(($crc -bxor $b) -band 0xFF)]) -bxor ($crc -shr 8)
    }
    ($crc -bxor 0xFFFFFFFF)
}
function Get-SimBucket {
    $h = $env:COMPUTERNAME
    if ([string]::IsNullOrEmpty($h)) { $h = [System.Net.Dns]::GetHostName() }
    # cache keyed by hostname (crc32 is a constant per boot)
    if (Test-Path -LiteralPath $script:BucketCache) {
        try {
            $line = (Get-Content -LiteralPath $script:BucketCache -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($line) {
                $parts = $line -split '\s+', 2
                if ($parts.Count -eq 2 -and $parts[0] -eq $h -and $parts[1] -match '^[0-9]$') { return [int]$parts[1] }
            }
        } catch {}
    }
    $b = [int]((Get-Crc32 $h) % 10)
    try { Set-Content -LiteralPath $script:BucketCache -Value "$h $b" -ErrorAction SilentlyContinue } catch {}
    $b
}

# ── adapter / PHY detection ──────────────────────────────────────────────────
# Get-WlanAdapter / Get-EthAdapter return the interface NAME of the wifi / wired
# NIC. Get-PhyType classifies the interface that owns the DEFAULT ROUTE (the real
# uplink) — mirrors common.sh detect_phy_type: wireless→negotiated 802.11 std,
# ethernet→"ethernet", never guess "ethernet" for a gateway-less/APIPA NIC.
function Get-WlanAdapter {
    $a = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
         Where-Object { $_.Status -ne 'Disabled' -and ($_.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11|WLAN' -or $_.Name -match 'Wi-?Fi|Wireless|WLAN') } |
         Select-Object -First 1
    if ($a) { $a.Name } else { '' }
}
function Get-EthAdapter {
    $a = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
         Where-Object { $_.Status -ne 'Disabled' -and ($_.InterfaceDescription -match 'Ethernet|GBE|Realtek|Intel.*(I2|I3|82|Ethernet)' -and $_.InterfaceDescription -notmatch 'Wi-?Fi|Wireless|802\.11') } |
         Select-Object -First 1
    if ($a) { $a.Name } else { '' }
}
function Get-DefaultRouteIfIndex {
    $r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
         Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
         Sort-Object RouteMetric | Select-Object -First 1
    if ($r) { $r.InterfaceIndex } else { $null }
}
function Get-PhyType {
    $idx = Get-DefaultRouteIfIndex
    if ($null -eq $idx) { return 'unknown' }
    # never classify off an APIPA-only (169.254) interface
    $hasReal = Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue |
               Where-Object { $_.IPAddress -notmatch '^169\.254\.' }
    if (-not $hasReal) { return 'unknown' }
    $ad = Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue
    if (-not $ad) { return 'unknown' }
    if ($ad.InterfaceDescription -match 'Wi-?Fi|Wireless|802\.11|WLAN' -or $ad.Name -match 'Wi-?Fi|Wireless|WLAN') {
        return (Get-WifiStandard)
    }
    return 'ethernet'
}
# Negotiated 802.11 standard from `netsh wlan show interfaces` (Radio type row).
function Get-WifiStandard {
    try {
        $out = netsh wlan show interfaces 2>$null
        $radio = ($out | Select-String -Pattern 'Radio type\s*:\s*(.+)$')
        if ($radio) {
            $rt = $radio.Matches[0].Groups[1].Value.Trim()
            switch -Regex ($rt) {
                '802\.11ax' { return '802.11ax' }
                '802\.11ac' { return '802.11ac' }
                '802\.11n'  { return '802.11n'  }
                '802\.11g'  { return '802.11g'  }
                '802\.11a'  { return '802.11a'  }
                '802\.11b'  { return '802.11b'  }
                default     { return 'wireless' }
            }
        }
    } catch {}
    'wireless'
}

# ── per-user override ────────────────────────────────────────────────────────
# CS_OVERRIDE_KEYS mirrors common.sh exactly (the SUPERSET). Apply-SimOverride
# reads the [$username] section (get_value) and, when set, overrides the
# bucket/global value in the caller's scope. Requires ini-parser.ps1 +
# $global:iniConfig populated and $username set (Get-SimUsername).
$script:CS_OVERRIDE_KEYS = @(
    'kill_switch','sim_load','github_repo','repo_location','site_based_ssid','iperf_bw',
    'wsite','sim_phy','ssid','ssidpw','dhcp_fail','dns_fail','dns_latency','assoc_fail','port_flap','ping_test','download','iperf',
    'www_traffic','ssidpw_fail','auth_fail','smb_address','ping_address','dns_latency_1','dns_latency_2',
    'dns_latency_3','dns_bad_ip_1','dns_bad_ip_2','dns_bad_ip_3','dns_bad_record_1','dns_bad_record_2',
    'dns_bad_record_3','iperf_server','dot1x_password',
    'collab','collab_app','collab_bw','collab_time','collab_server','web_server'
)
# Returns the override value for a key ($null when unset). Callers assign it to
# their own variable — PowerShell scoping makes an in-place declare -g awkward,
# so this is the idiomatic form: $x = Apply-SimOverride 'x' $x
function Apply-SimOverride {
    param([string]$Key, $Current)
    $u = Get-SimUsername
    $val = get_value $u $Key
    if (-not [string]::IsNullOrEmpty($val)) { return $val }
    return $Current
}
# Bulk-apply: returns a hashtable of key→effective value for every override key
# that is SET in the [$username] section (unset keys omitted). Sims merge this
# over their config.
function Get-SimOverrides {
    $u = Get-SimUsername
    $out = @{}
    foreach ($k in $script:CS_OVERRIDE_KEYS) {
        $v = get_value $u $k
        if (-not [string]::IsNullOrEmpty($v)) { $out[$k] = $v }
    }
    $out
}

# ── JSON string escaping ─────────────────────────────────────────────────────
function ConvertTo-SimJsonString {
    param([string]$Value = '')
    $v = $Value -replace '\\','\\\\'
    $v = $v -replace '"','\"'
    $v = $v -replace "`n",'\n'
    $v = $v -replace "`r",'\r'
    $v = $v -replace "`t",'\t'
    $v
}

# ── DNS flood self-throttle (gateway circuit-breaker) ────────────────────────
# Faithful port of common.sh dns_ceiling_* + gateway helpers. ONE shared ceiling
# for both DNS sims. Down-only in Phase 1 (up-probe is Phase-2 learning-mode).
# See common.sh:184-296 for the full rationale (AIMD, "I've DOSed myself" bail).
$script:DNS_CEILING_FILE = Join-Path $script:ScriptsRoot 'dns_ceiling.state'
$script:DNS_RATE_FLOOR   = 0
$script:DNS_UPPROBE_EVERY = 5

function Get-DnsDefaultGateway {
    $r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
         Where-Object { $_.NextHop -and $_.NextHop -ne '0.0.0.0' } |
         Sort-Object RouteMetric | Select-Object -First 1
    if ($r) { $r.NextHop } else { '' }
}
# 0/true iff the gateway answers a single ping.
function Test-DnsGatewayAlive {
    param([string]$Gw)
    if ([string]::IsNullOrEmpty($Gw)) { return $false }
    try { return [bool](Test-Connection -ComputerName $Gw -Count 1 -Quiet -ErrorAction SilentlyContinue) } catch { return $false }
}
# True (offline) ONLY when all 5 pings fail — a real outage, not a blip.
function Test-DnsGatewayConfirmedDown {
    param([string]$Gw)
    if ([string]::IsNullOrEmpty($Gw)) { return $false }
    try { return -not [bool](Test-Connection -ComputerName $Gw -Count 5 -Quiet -ErrorAction SilentlyContinue) } catch { return $true }
}
# RECOVERY HOLD: gateway STABLY up = N consecutive single pings all reply, ~gap apart.
function Test-DnsGatewayStable {
    param([string]$Gw, [int]$N = 4, [int]$Gap = 2)
    if ([string]::IsNullOrEmpty($Gw)) { return $false }
    for ($i = 0; $i -lt $N; $i++) {
        if (-not (Test-DnsGatewayAlive $Gw)) { return $false }
        if ($i -lt ($N - 1)) { Start-Sleep -Seconds $Gap }
    }
    return $true
}
function Get-DnsCeilingSaved {
    if (Test-Path -LiteralPath $script:DNS_CEILING_FILE) {
        $s = (Get-Content -LiteralPath $script:DNS_CEILING_FILE -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($s -match '^[0-9]+$') { return [int]$s }
    }
    return $null
}
# Effective per-burst rate = min(configured target, persisted self-throttle).
# >= 0 so a persisted ceiling of 0 (sidelined client) is HONORED.
function Get-DnsCeilingRate {
    param([int]$Configured)
    $saved = Get-DnsCeilingSaved
    if ($null -ne $saved -and $saved -ge 0 -and $saved -lt $Configured) { return $saved }
    return $Configured
}
function Set-DnsCeiling {
    param([int]$Rate)
    try { Set-Content -LiteralPath $script:DNS_CEILING_FILE -Value ([string]$Rate) -ErrorAction SilentlyContinue } catch {}
}
# AIMD multiplicative DECREASE: persist (rate * 0.8), floored; returns new rate.
function Invoke-DnsCeilingPenalize {
    param([int]$Achieved)
    $next = [int][math]::Floor($Achieved * 0.8)
    if ($next -lt $script:DNS_RATE_FLOOR) { $next = $script:DNS_RATE_FLOOR }
    Set-DnsCeiling $next
    return $next
}
# Phase-2 additive up-probe: nudge ~+20% while throttled below target; clears the
# throttle when it reaches the target. Returns next rate.
function Invoke-DnsCeilingUpprobe {
    param([int]$Cur, [int]$Configured)
    $next = [int][math]::Floor($Cur * 1.2 + 1)
    if ($next -ge $Configured) { Reset-DnsCeiling; return $Configured }
    Set-DnsCeiling $next
    return $next
}
function Reset-DnsCeiling {
    try { Remove-Item -LiteralPath $script:DNS_CEILING_FILE -ErrorAction SilentlyContinue } catch {}
}

# ── DNS-latency server selection (self-healing) ──────────────────────────────
# Faithful port of common.sh dns_lat_* (:298-348). Keeps a POOL of servers and
# uses ONE confirmed slow (>= threshold), rotating to the next when the current
# drops below threshold. A TIMEOUT counts as slow (kept); only a FAST response
# rotates. Persists the current so a good one STICKS.
$script:DNS_LAT_STATE_FILE = Join-Path $script:ScriptsRoot 'dns_latency_server.state'
$script:DNS_LAT_MAX_PROBES = 10

function Get-DnsLatThresholdMs {
    $t = get_value 'simulation' 'dns_latency_threshold_ms'
    if ($t -match '^[0-9]+$') { return [int]$t }
    return 500
}
function Get-DnsLatRecheckS {
    $s = get_value 'simulation' 'dns_latency_recheck_s'
    if ($s -match '^[0-9]+$') { return [int]$s }
    return 30
}
# Wall-clock ms of ONE lookup against $Server (record $Record). A fast answer
# reads < threshold; a slow answer OR a ~1s timeout both read high (both keep the
# server) — mirrors common.sh dns_lat_probe_ms (dig +time=1 +tries=1).
function Measure-DnsLatProbeMs {
    param([string]$Server, [string]$Record = 'example.com')
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        Resolve-DnsName -Name $Record -Server $Server -Type A -QuickTimeout -DnsOnly -ErrorAction SilentlyContinue | Out-Null
    } catch {}
    $sw.Stop()
    [int]$sw.ElapsedMilliseconds
}
# True when $Server's lookups are still slow enough (>= threshold) to feed the alert.
function Test-DnsLatOk {
    param([string]$Server, [string]$Record = 'example.com')
    if ([string]::IsNullOrEmpty($Server)) { return $false }
    return ((Measure-DnsLatProbeMs $Server $Record) -ge (Get-DnsLatThresholdMs))
}
function Get-DnsLatSaved {
    if (Test-Path -LiteralPath $script:DNS_LAT_STATE_FILE) {
        $v = (Get-Content -LiteralPath $script:DNS_LAT_STATE_FILE -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($v) { return ($v -replace '[\r\n\s]', '') }
    }
    return ''
}
function Set-DnsLatSaved {
    param([string]$Server)
    try { Set-Content -LiteralPath $script:DNS_LAT_STATE_FILE -Value $Server -ErrorAction SilentlyContinue } catch {}
}
# Pick a server from $Pool whose lookups clear the threshold. Starts from the
# persisted current (STICKS), then walks the rest (capped). Persists + returns the
# chosen server; if none clears, keeps current (else first) best-effort.
function Select-DnsLatServer {
    param([string]$Record, [string[]]$Pool)
    if (-not $Pool -or $Pool.Count -eq 0) { return '' }
    $cur = Get-DnsLatSaved
    $ordered = New-Object System.Collections.Generic.List[string]
    if ($cur) { foreach ($s in $Pool) { if ($s -eq $cur) { $ordered.Add($s) } } }
    foreach ($s in $Pool) { if ($s -ne $cur) { $ordered.Add($s) } }
    $n = 0
    foreach ($s in $ordered) {
        if ($n -ge $script:DNS_LAT_MAX_PROBES) { break }
        $n++
        if (Test-DnsLatOk $s $Record) { Set-DnsLatSaved $s; return $s }
    }
    $best = if ($cur) { $cur } else { $Pool[0] }
    Set-DnsLatSaved $best
    return $best
}
