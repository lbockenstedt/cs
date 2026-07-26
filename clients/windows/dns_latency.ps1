# ============================================================================ #
# dns_latency.ps1 — Windows port of clients/linux/dns_latency.sh.               #
#                                                                               #
# Sibling of dns_fail.ps1. dns_fail queries UNREACHABLE/bogus servers (lookups  #
# fail → DNS-failure alert); this queries the SLOW responders (dns_latency      #
# pool) so lookups are delayed → DNS-LATENCY alert. Split so each condition     #
# drives its own Central alert. Same fire-and-forget, rate-based shape, and it  #
# SHARES the one DNS ceiling + gateway circuit-breaker with dns_fail.           #
#                                                                               #
# Target: Windows PowerShell 5.1 (Desktop). Deploy path C:\Scripts\.            #
# ============================================================================ #
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

$version = '0.01'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'dns_latency.ps1' $version

# ── bogus records file (probe/query names) ───────────────────────────────────
# RANDOMIZE the record order each burst so the query stream varies per
# client/burst instead of the same file-order first-N. Re-shuffled per burst.
try {
    $dnsfile = @((Get-Content -LiteralPath 'C:\Scripts\dns_fail.txt' -ErrorAction Stop) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    Write-SimLog "dns_latency.ps1: unable to read dns_fail.txt: $($_.Exception.Message)"
    exit 0
}
$probeRecord = $dnsfile | Get-Random
if ([string]::IsNullOrWhiteSpace($probeRecord)) { $probeRecord = 'example.com' }

# ── slow-responder POOL ──────────────────────────────────────────────────────
# Prefer the [address] `dns_latency` list (space/comma separated, UNLIMITED —
# real DNS servers blacklist a flooding client over time, so keep a big pool and
# rotate to a still-slow one). Fall back to the legacy dns_latency_1/2/3 keys.
$overrides = Get-SimOverrides
$dnsLatency = get_value 'address' 'dns_latency'
if ($overrides.ContainsKey('dns_latency')) { $dnsLatency = $overrides['dns_latency'] }

$pool = @()
if (-not [string]::IsNullOrWhiteSpace($dnsLatency)) {
    $pool = @(($dnsLatency -split '[,\s]+') | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}
if ($pool.Count -eq 0) {
    $l1 = get_value 'address' 'dns_latency_1'
    $l2 = get_value 'address' 'dns_latency_2'
    $l3 = get_value 'address' 'dns_latency_3'
    if ($overrides.ContainsKey('dns_latency_1')) { $l1 = $overrides['dns_latency_1'] }
    if ($overrides.ContainsKey('dns_latency_2')) { $l2 = $overrides['dns_latency_2'] }
    if ($overrides.ContainsKey('dns_latency_3')) { $l3 = $overrides['dns_latency_3'] }
    $pool = @(@($l1, $l2, $l3) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}
if ($pool.Count -eq 0) {
    Write-SimLog "dns_latency.ps1: no dns_latency servers configured — nothing to do"
    exit 0
}

# ── per-user on/off gate ─────────────────────────────────────────────────────
if ($overrides.ContainsKey('dns_latency') -and $overrides['dns_latency'] -match '^(off|no|0|false)$') {
    Write-SimLog "dns_latency.ps1: disabled for this user via override — skipping"
    exit 0
}

# Self-healing pick: ONE server whose lookups are actually SLOW (>= threshold),
# rotating away from any blacklisted (now-fast) one. Re-probed during the burst.
$recheckS = Get-DnsLatRecheckS
$current  = Select-DnsLatServer $probeRecord $pool
if ([string]::IsNullOrWhiteSpace($current)) { $current = $pool[0] }

# ── rate + burst window ──────────────────────────────────────────────────────
# DNS_LATENCY_RATE env forces the rate AND bypasses the persisted self-throttle.
$ratePerMinute = get_value 'simulation' 'dns_latency_rate'
$burstSeconds  = get_value 'simulation' 'dns_latency_duration'
if (-not $ratePerMinute) { $ratePerMinute = 600 }
if (-not $burstSeconds)  { $burstSeconds  = 60 }
$ratePerMinute = [int]$ratePerMinute
$burstSeconds  = [int]$burstSeconds
if ($ratePerMinute -lt 200) { $ratePerMinute = 200 }

if ($env:DNS_LATENCY_RATE -match '^[0-9]+$') {
    $configuredRate = [int]$env:DNS_LATENCY_RATE
    $ratePerMinute  = [int]$env:DNS_LATENCY_RATE
} else {
    $configuredRate = $ratePerMinute
    # Shared DNS ceiling with dns_fail (same client dig capacity).
    $ratePerMinute  = Get-DnsCeilingRate $ratePerMinute
}

if ($ratePerMinute -le 0) {
    Write-SimLog "$(Get-Date) DNS self-throttle floored this client to 0/min — can't sustain the flood (bad USB/hub?); sidelining, not flooding"
    exit 0
}

$pauseMs = [int](60000 / $ratePerMinute)

# CPU guard: cap simultaneous in-flight nslookup processes. Shared knob with
# dns_fail. Precedence: DNS_MAX_INFLIGHT env > [simulation] dns_max_inflight >
# default 100. Floored at 1.
$maxInflight = $env:DNS_MAX_INFLIGHT
if ($maxInflight -notmatch '^[0-9]+$') { $maxInflight = get_value 'simulation' 'dns_max_inflight' }
if ($maxInflight -notmatch '^[0-9]+$') { $maxInflight = 100 }
$maxInflight = [int]$maxInflight
if ($maxInflight -lt 1) { $maxInflight = 1 }

$throttleNote = ''
if ($ratePerMinute -lt $configuredRate) { $throttleNote = " (self-throttled from $configuredRate/min after a prior gateway DOS)" }
Write-SimLog "$(Get-Date) Firing DNS latency lookups at $ratePerMinute/min for ${burstSeconds}s (max $maxInflight lookups in flight; server $current, threshold $(Get-DnsLatThresholdMs)ms, recheck every ${recheckS}s)$throttleNote"

# ── gateway circuit-breaker: start gate (shared logic with dns_fail) ─────────
$gw = Get-DnsDefaultGateway
if (-not [string]::IsNullOrEmpty($gw) -and -not (Test-DnsGatewayStable $gw)) {
    Write-SimLog "$(Get-Date) default gateway $gw not stably up (USB bus still clearing?) — holding the flood OFF this burst so the adapter can recover"
    exit 0
}

# ── flood loop (single selected slow server, with periodic rotation) ─────────
$stopAt = (Get-Date).AddSeconds($burstSeconds)
$fired = 0
$bailed = $false
$inflight = New-Object 'System.Collections.Generic.List[object]'
$gwNextCheck  = (Get-Date).AddSeconds(2)
$latNextCheck = (Get-Date).AddSeconds($recheckS)

while ((Get-Date) -lt $stopAt) {
    $records = $dnsfile | Sort-Object { Get-Random }

    foreach ($record in $records) {
        if ((Get-Date) -ge $stopAt) { break }

        # Gateway check (~every 2s): confirmed-down = we DOSed ourselves → bail,
        # drop the operating rate 20%, hold until stable.
        if (-not [string]::IsNullOrEmpty($gw) -and (Get-Date) -ge $gwNextCheck) {
            $gwNextCheck = (Get-Date).AddSeconds(2)
            if ((-not (Test-DnsGatewayAlive $gw)) -and (Test-DnsGatewayConfirmedDown $gw)) {
                $newRate = Invoke-DnsCeilingPenalize $ratePerMinute
                Write-SimLog "$(Get-Date) default gateway $gw OFFLINE (5/5 pings failed) after $fired lookups at $ratePerMinute/min — BAILING; throttling to $newRate/min next burst"
                foreach ($p in $inflight) { try { if (-not $p.HasExited) { $p.Kill() } } catch {} }
                $bailed = $true
                break
            }
        }

        # Latency re-check (~every recheckS): if the current server dropped BELOW
        # threshold (blacklisted → now refusing fast), rotate to another
        # confirmed-slow server so the latency alert stays fed.
        if ((Get-Date) -ge $latNextCheck) {
            $latNextCheck = (Get-Date).AddSeconds($recheckS)
            if (-not (Test-DnsLatOk $current $probeRecord)) {
                $new = Select-DnsLatServer $probeRecord $pool
                if (-not [string]::IsNullOrWhiteSpace($new) -and $new -ne $current) {
                    Write-SimLog "$(Get-Date) dns_latency: server $current no longer slow (blacklisted?) — rotating to $new"
                    $current = $new
                }
            }
        }

        # In-flight cap: prune finished procs; pause (rate) until a slot frees.
        $inflight = New-Object 'System.Collections.Generic.List[object]' (,([object[]]($inflight | Where-Object { $_ -and (-not $_.HasExited) })))
        while ($inflight.Count -ge $maxInflight) {
            Start-Sleep -Milliseconds $pauseMs
            $inflight = New-Object 'System.Collections.Generic.List[object]' (,([object[]]($inflight | Where-Object { $_ -and (-not $_.HasExited) })))
        }

        # Fire-and-forget against the single selected slow server. 1s timeout so
        # the slow lookups clear instead of piling up — the delay is the point.
        try {
            $p = Start-Process -FilePath 'nslookup.exe' `
                    -ArgumentList '-timeout=1', $record, $current `
                    -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
            if ($p) { $inflight.Add($p) }
        } catch {}

        $fired++
        Start-Sleep -Milliseconds $pauseMs
    }
    if ($bailed) { break }
}

if ($bailed) {
    if (-not [string]::IsNullOrEmpty($gw)) { Test-DnsGatewayStable $gw | Out-Null }
    Write-SimLog "$(Get-Date) DNS latency lookups fired: $fired (BAILED on gateway loss — self-throttling next burst)"
} else {
    Write-SimLog "$(Get-Date) DNS latency lookups fired: $fired"
}
