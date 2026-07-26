# ============================================================================ #
# dhcp_fail.ps1 — intentional DHCP-failure generator (Windows sim client).      #
#                                                                               #
# PowerShell port of clients/linux/dhcp_fail.sh + clients/linux/dhcp_fire.py.   #
# The client's real wifi/wired connection and its NORMAL MAC are left           #
# UNTOUCHED — no adapter MAC change, no profile edit, no lease release. Instead  #
# we fire crafted DHCPDISCOVERs as fire-and-forget UDP datagrams (no reply       #
# waited for — the failure is the point, mirror of dns_fail.ps1):               #
#                                                                               #
#   A) -> the REAL DHCP server (detected from the adapter config), carrying a    #
#      FORGED identity 00:01:00:00:<o5>:<o6>. The good server rejects/ignores    #
#      the unknown id.                                                          #
#   B) -> a dead server (default 10.10.10.10), carrying the REAL mac. Nothing    #
#      responds (timeout).                                                      #
#                                                                               #
# The identity rides in BOTH the BOOTP chaddr field AND DHCP option-61          #
# (client-identifier), so whichever field the server/NAC keys on sees the same  #
# identity — byte-for-byte the packet dhcp_fire.py builds.                       #
#                                                                               #
# Loop model: 100 attempts then EXIT. The outer sim loop (simulation.ps1)        #
# relaunches this each iteration, which re-detects the real DHCP server fresh.   #
#                                                                               #
# Needs elevation for the broadcast-enabled UDP socket; the sim runs elevated.  #
# Target: Windows PowerShell 5.1 (Desktop).                                     #
# ============================================================================ #
. 'C:\Scripts\ini-parser.ps1'
. 'C:\Scripts\common.ps1'

$version = '0.01'
$debugPath = 'C:\Scripts\debug-dhcp-fail.log'

"DHCP Fail Script Version $version" | Tee-Object -FilePath $debugPath | Out-Null
Get-Date | Tee-Object -FilePath $debugPath -Append | Out-Null

$global:iniConfig = Parse-IniFile 'C:\Scripts\simulation.conf'
Write-SimVersionBanner 'dhcp_fail' $version

# ── resolve the active uplink adapter — wifi first, then wired (mirror sh) ────
$iface = Get-WlanAdapter
if ([string]::IsNullOrEmpty($iface)) { $iface = Get-EthAdapter }
if ([string]::IsNullOrEmpty($iface)) {
    # last resort: whatever owns the default route
    $idx = Get-DefaultRouteIfIndex
    if ($null -ne $idx) {
        $ad = Get-NetAdapter -InterfaceIndex $idx -ErrorAction SilentlyContinue
        if ($ad) { $iface = $ad.Name }
    }
}
if ([string]::IsNullOrEmpty($iface)) {
    Write-SimLog "$(Get-Date) dhcp_fail: no usable adapter — exiting"
    exit 1
}

$adapter = Get-NetAdapter -Name $iface -ErrorAction SilentlyContinue
if (-not $adapter) {
    Write-SimLog "$(Get-Date) dhcp_fail: cannot read adapter $iface — exiting"
    exit 1
}

# Real MAC via (Get-NetAdapter).MacAddress — normalize "AA-BB-.." / "AA:BB:.." to
# a 6-byte array.
$macHex = ($adapter.MacAddress -replace '[:\-]', '').Trim()
if ($macHex.Length -ne 12) {
    Write-SimLog "$(Get-Date) dhcp_fail: bad MAC '$($adapter.MacAddress)' on $iface — exiting"
    exit 1
}
$realMac = New-Object 'System.Byte[]' 6
for ($i = 0; $i -lt 6; $i++) { $realMac[$i] = [Convert]::ToByte($macHex.Substring($i * 2, 2), 16) }

# Forged identity: fixed 00:01 prefix (recognizable) + real last two octets
# (keeps every client unique) — matches dhcp_fail.sh's 00:01:00:00:<o5>:<o6>.
$forgedMac = [byte[]](0x00, 0x01, 0x00, 0x00, $realMac[4], $realMac[5])

# ── detect the real DHCP server from the adapter config ──────────────────────
# Primary: Win32_NetworkAdapterConfiguration.DHCPServer; fallback: ipconfig /all.
function Get-RealDhcpServer {
    param([int]$IfIndex)
    try {
        $cfg = Get-CimInstance -ClassName Win32_NetworkAdapterConfiguration `
               -Filter "InterfaceIndex=$IfIndex AND IPEnabled=True" -ErrorAction SilentlyContinue |
               Select-Object -First 1
        if ($cfg -and $cfg.DHCPServer -and $cfg.DHCPServer -match '^\d{1,3}(\.\d{1,3}){3}$' -and $cfg.DHCPServer -ne '255.255.255.255') {
            return $cfg.DHCPServer
        }
    } catch {}
    try {
        $out = ipconfig /all 2>$null
        $m = ($out | Select-String -Pattern 'DHCP Server[ .]*:\s*(\d{1,3}(?:\.\d{1,3}){3})' | Select-Object -First 1)
        if ($m) {
            $srv = $m.Matches[0].Groups[1].Value
            if ($srv -ne '255.255.255.255') { return $srv }
        }
    } catch {}
    return ''
}
$realServer = Get-RealDhcpServer -IfIndex $adapter.ifIndex

# Dead server (never responds) — configurable via [address] dhcp_fail_dead_server.
$deadServer = get_value 'address' 'dhcp_fail_dead_server'
if ([string]::IsNullOrWhiteSpace($deadServer)) { $deadServer = '10.10.10.10' }

# Rate: attempts per minute -> pause between attempts (mirror dns_fail / sh).
$ratePerMinute = get_value 'simulation' 'dhcp_fail_rate'
if (-not $ratePerMinute) { $ratePerMinute = 600 }
$ratePerMinute = [int]$ratePerMinute
if ($ratePerMinute -lt 60) { $ratePerMinute = 60 }
$pauseMs = [int](60000 / $ratePerMinute)

Write-SimLog ("$(Get-Date) dhcp_fail start: iface=$iface real_mac=$macHex " +
              "forged=0001-0000-$('{0:x2}{1:x2}' -f $realMac[4], $realMac[5]) " +
              "real_server=$(if ($realServer) { $realServer } else { '<none>' }) " +
              "dead=$deadServer rate=$ratePerMinute/min")

# Shared CSPRNG for xid so packets fired in the same millisecond get distinct
# transaction ids (a fresh System.Random would reseed off the clock and repeat).
$script:XidRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()

# ── build a 300-byte DHCPDISCOVER (BOOTREQUEST) — byte-identical to dhcp_fire.py
function New-DhcpDiscover {
    param([byte[]]$MacBytes)
    $pkt = New-Object System.Collections.Generic.List[byte]
    # op=1 BOOTREQUEST, htype=1 ethernet, hlen=6, hops=0
    $pkt.AddRange([byte[]](1, 1, 6, 0))
    # xid — 4 random bytes (network order is irrelevant for an opaque id)
    $xid = New-Object 'System.Byte[]' 4
    $script:XidRng.GetBytes($xid)
    $pkt.AddRange($xid)
    $pkt.AddRange([byte[]](0, 0))          # secs
    $pkt.AddRange([byte[]](0, 0))          # flags
    $pkt.AddRange((New-Object 'System.Byte[]' 4))  # ciaddr
    $pkt.AddRange((New-Object 'System.Byte[]' 4))  # yiaddr
    $pkt.AddRange((New-Object 'System.Byte[]' 4))  # siaddr
    $pkt.AddRange((New-Object 'System.Byte[]' 4))  # giaddr
    # chaddr = mac (6) + 10 pad = 16-byte hardware address field
    $pkt.AddRange($MacBytes)
    $pkt.AddRange((New-Object 'System.Byte[]' 10))
    $pkt.AddRange((New-Object 'System.Byte[]' 64))   # sname
    $pkt.AddRange((New-Object 'System.Byte[]' 128))  # file
    $pkt.AddRange([byte[]](0x63, 0x82, 0x53, 0x63))  # magic cookie
    $pkt.AddRange([byte[]](0x35, 0x01, 0x01))        # opt 53: DHCP DISCOVER
    $pkt.AddRange([byte[]](0x3d, 0x07, 0x01))        # opt 61: client-id (hw-type 1)
    $pkt.AddRange($MacBytes)
    $pkt.AddRange([byte[]](0x37, 0x04, 0x01, 0x03, 0x06, 0x0f))  # opt 55: param req list
    $pkt.Add([byte]0xff)                             # END
    # Pad to the BOOTP minimum (300B).
    while ($pkt.Count -lt 300) { $pkt.Add([byte]0) }
    return , $pkt.ToArray()
}

# Fire one fire-and-forget DHCPDISCOVER at $Dst:67 carrying identity $MacBytes.
function Send-DhcpDiscover {
    param([string]$Dst, [byte[]]$MacBytes)
    $udp = $null
    try {
        $pkt = New-DhcpDiscover -MacBytes $MacBytes
        $udp = New-Object System.Net.Sockets.UdpClient
        $udp.EnableBroadcast = $true
        [void]$udp.Send($pkt, $pkt.Length, $Dst, 67)
    } catch {
        # transient (no route yet, host down) — the failure is the point, keep going
    } finally {
        if ($udp) { $udp.Close() }
    }
}

# 100 attempts, then exit — the sim loop relaunches (re-detecting the server).
$fired = 0
for ($i = 0; $i -lt 100; $i++) {
    # A) real server + forged id (skip only if we couldn't detect the server)
    if (-not [string]::IsNullOrEmpty($realServer)) {
        Send-DhcpDiscover -Dst $realServer -MacBytes $forgedMac
        $fired++
    }
    # B) dead server + real mac
    Send-DhcpDiscover -Dst $deadServer -MacBytes $realMac
    $fired++
    Start-Sleep -Milliseconds $pauseMs
}

Write-SimLog "$(Get-Date) dhcp_fail: $fired discovers fired — exiting (sim loop will relaunch)"
