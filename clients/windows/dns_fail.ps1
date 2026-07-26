# ============================================================================ #
# dns_fail.ps1 — Windows port of clients/linux/dns_fail.sh.                      #
#                                                                               #
# Floods bogus/unreachable DNS lookups to trip Central's rate-based DNS-FAILURE #
# alarm. Fire-and-forget: we never wait for the (failing) answer — the failure  #
# IS the sim. The slow-responder set (dns_latency pool) is a DIFFERENT          #
# condition and lives in dns_latency.ps1 so each drives its own Central alert.  #
#                                                                               #
# Target: Windows PowerShell 5.1 (Desktop). Deploy path C:\Scripts\.            #
# ============================================================================ #
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

$version = '0.01'
$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'dns_fail.ps1' $version

# ── bogus records file ───────────────────────────────────────────────────────
# RANDOMIZE the record order each burst so every client/burst queries a
# different random subset of the (10k) bogus names instead of marching the same
# first-N in file order — makes the failure traffic look like scattered typo
# lookups. (Re-shuffled at the top of each burst below.)
try {
    $dnsfile = @((Get-Content -LiteralPath 'C:\Scripts\dns_fail.txt' -ErrorAction Stop) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
} catch {
    Write-SimLog "dns_fail.ps1: unable to read dns_fail.txt: $($_.Exception.Message)"
    exit 0
}

# ── servers (unreachable/bogus resolvers) ────────────────────────────────────
# dns_fail queries UNREACHABLE / bogus servers only (dns_bad_record_* +
# dns_bad_ip_*) so the lookups FAIL. Do NOT include the dns_latency pool.
$dns_bad_ip_1 = get_value 'address' 'dns_bad_ip_1'
$dns_bad_ip_2 = get_value 'address' 'dns_bad_ip_2'
$dns_bad_ip_3 = get_value 'address' 'dns_bad_ip_3'
$dns_bad_record_1 = get_value 'address' 'dns_bad_record_1'
$dns_bad_record_2 = get_value 'address' 'dns_bad_record_2'
$dns_bad_record_3 = get_value 'address' 'dns_bad_record_3'

# Per-user overrides — a [username] override of a bad DNS server must reach the
# lookup loop (mirror simulation.sh apply_override / dns_fail.sh).
$overrides = Get-SimOverrides
if ($overrides.ContainsKey('dns_bad_ip_1'))     { $dns_bad_ip_1     = $overrides['dns_bad_ip_1'] }
if ($overrides.ContainsKey('dns_bad_ip_2'))     { $dns_bad_ip_2     = $overrides['dns_bad_ip_2'] }
if ($overrides.ContainsKey('dns_bad_ip_3'))     { $dns_bad_ip_3     = $overrides['dns_bad_ip_3'] }
if ($overrides.ContainsKey('dns_bad_record_1')) { $dns_bad_record_1 = $overrides['dns_bad_record_1'] }
if ($overrides.ContainsKey('dns_bad_record_2')) { $dns_bad_record_2 = $overrides['dns_bad_record_2'] }
if ($overrides.ContainsKey('dns_bad_record_3')) { $dns_bad_record_3 = $overrides['dns_bad_record_3'] }

$bad_records = @($dns_bad_record_1, $dns_bad_record_2, $dns_bad_record_3)
$bad_ips     = @($dns_bad_ip_1, $dns_bad_ip_2, $dns_bad_ip_3)
$servers = @($bad_records + $bad_ips | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($servers.Count -eq 0) {
    Write-SimLog "dns_fail.ps1: no bad DNS servers configured — nothing to flood"
    exit 0
}

# ── per-user on/off gate ─────────────────────────────────────────────────────
if ($overrides.ContainsKey('dns_fail') -and $overrides['dns_fail'] -match '^(off|no|0|false)$') {
    Write-SimLog "dns_fail.ps1: disabled for this user via override — skipping"
    exit 0
}

# ── rate + burst window ──────────────────────────────────────────────────────
# How fast (lookups/min) and how long (seconds); safe defaults if unset. Never
# below the ~200/min the alarm needs. DNS_FAIL_RATE env forces the rate AND
# bypasses the persisted self-throttle (manual ceiling testing).
$ratePerMinute = get_value 'simulation' 'dns_fail_rate'
$burstSeconds  = get_value 'simulation' 'dns_fail_duration'
if (-not $ratePerMinute) { $ratePerMinute = 600 }
if (-not $burstSeconds)  { $burstSeconds  = 60 }
$ratePerMinute = [int]$ratePerMinute
$burstSeconds  = [int]$burstSeconds
if ($ratePerMinute -lt 200) { $ratePerMinute = 200 }

if ($env:DNS_FAIL_RATE -match '^[0-9]+$') {
    $configuredRate = [int]$env:DNS_FAIL_RATE
    $ratePerMinute  = [int]$env:DNS_FAIL_RATE
} else {
    $configuredRate = $ratePerMinute
    # Effective per-burst rate = min(configured, persisted ceiling). Shared DNS
    # ceiling with dns_latency (same client dig capacity).
    $ratePerMinute  = Get-DnsCeilingRate $ratePerMinute
}

# A ceiling of 0 = this client can't sustain ANY flood (bad USB/hub dongle):
# sideline it (also avoids divide-by-zero). Stays off until its state clears.
if ($ratePerMinute -le 0) {
    Write-SimLog "$(Get-Date) DNS self-throttle floored this client to 0/min — can't sustain the flood (bad USB/hub?); sidelining, not flooding"
    exit 0
}

# Milliseconds between launches to hit the target rate (600/min -> 100 ms).
$pauseMs = [int](60000 / $ratePerMinute)

# CPU guard: cap simultaneous in-flight nslookup processes. Precedence:
# DNS_MAX_INFLIGHT env > [simulation] dns_max_inflight > default 100. Floored 1.
$maxInflight = $env:DNS_MAX_INFLIGHT
if ($maxInflight -notmatch '^[0-9]+$') { $maxInflight = get_value 'simulation' 'dns_max_inflight' }
if ($maxInflight -notmatch '^[0-9]+$') { $maxInflight = 100 }
$maxInflight = [int]$maxInflight
if ($maxInflight -lt 1) { $maxInflight = 1 }

$throttleNote = ''
if ($ratePerMinute -lt $configuredRate) { $throttleNote = " (self-throttled from $configuredRate/min after a prior gateway DOS)" }
Write-SimLog "$(Get-Date) Firing DNS failures at $ratePerMinute/min for ${burstSeconds}s (max $maxInflight lookups in flight)$throttleNote"

# ── gateway circuit-breaker: start gate ──────────────────────────────────────
# The dongles sit on a passed-through USB PCI card the guest can't bus-reset, so
# recovery = remove load + let the bus clear. Don't (re)start the flood until the
# gateway is STABLY up (several pings in a row); until then hold it OFF.
$gw = Get-DnsDefaultGateway
if (-not [string]::IsNullOrEmpty($gw) -and -not (Test-DnsGatewayStable $gw)) {
    Write-SimLog "$(Get-Date) default gateway $gw not stably up (USB bus still clearing?) — holding the flood OFF this burst so the adapter can recover"
    exit 0
}

# ── flood loop ───────────────────────────────────────────────────────────────
$stopAt = (Get-Date).AddSeconds($burstSeconds)
$fired = 0
$bailed = $false
$inflight = New-Object 'System.Collections.Generic.List[object]'
$gwNextCheck = (Get-Date).AddSeconds(2)

while ((Get-Date) -lt $stopAt) {
    # Re-shuffle the record order each burst pass.
    $records = $dnsfile | Sort-Object { Get-Random }

    foreach ($record in $records) {
        if ((Get-Date) -ge $stopAt) { break }

        foreach ($server in $servers) {
            if ((Get-Date) -ge $stopAt) { break }

            # Gateway check (~every 2s): a confirmed-down gateway (5/5 pings fail)
            # means WE DOSed ourselves — bail, drop the operating rate 20% for next
            # burst, and hold until the gateway is stable again.
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

            # In-flight cap: prune finished procs; if still at the cap, pause (the
            # rate pause) and prune again until a slot frees.
            $inflight = New-Object 'System.Collections.Generic.List[object]' (,([object[]]($inflight | Where-Object { $_ -and (-not $_.HasExited) })))
            while ($inflight.Count -ge $maxInflight) {
                Start-Sleep -Milliseconds $pauseMs
                $inflight = New-Object 'System.Collections.Generic.List[object]' (,([object[]]($inflight | Where-Object { $_ -and (-not $_.HasExited) })))
            }

            # Fire-and-forget: hidden nslookup with a 1s timeout so failing lookups
            # clear quickly instead of piling up. The failure is the point.
            try {
                $p = Start-Process -FilePath 'nslookup.exe' `
                        -ArgumentList '-timeout=1', $record, $server `
                        -WindowStyle Hidden -PassThru -ErrorAction SilentlyContinue
                if ($p) { $inflight.Add($p) }
            } catch {}

            $fired++
            Start-Sleep -Milliseconds $pauseMs
        }
        if ($bailed) { break }
    }
    if ($bailed) { break }
}

# Let any lookups still in flight clear.
if ($bailed) {
    # Hold until the gateway is stable again so the recovering USB bus isn't
    # re-contended before this run exits (next run's start gate resumes the flood).
    if (-not [string]::IsNullOrEmpty($gw)) { Test-DnsGatewayStable $gw | Out-Null }
    Write-SimLog "$(Get-Date) DNS failures fired: $fired (BAILED on gateway loss — self-throttling next burst)"
} else {
    Write-SimLog "$(Get-Date) DNS failures fired: $fired"
}
