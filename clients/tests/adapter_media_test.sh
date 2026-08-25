#!/bin/bash
# adapter_media_test.sh — regression test for detect_adapter_inventory's
# interface-name -> media_type classification in clients common.sh.
#
# Pins the fix for the T1-locked-out-of-wireless-sims bug: the adapter-inventory
# classifier once matched wireless only as `wlx*|wlan*|wlp*`, so an ONBOARD
# radio named `wlo1` (or a slot `wls*`) was misfiled media_type="other". The
# engine's fail-closed media gate (_media_ok) then made that client ineligible
# for every wireless-only sim (assoc_fail/ssidpw_fail) while a T2 dongle took
# the sim — even though the box has a real wireless radio. The classifier now
# matches any `wl*` (aligned with detect_phy_type). This test locks that in and
# guards against re-narrowing the glob.
#
# Usage: bash clients/tests/adapter_media_test.sh [path-to-common.sh]
# Defaults to clients/lib/common.sh (falls back to clients/linux/).

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMON="${1:-}"
if [[ -z "$COMMON" ]]; then
    if [[ -f "$HERE/../lib/common.sh" ]]; then
        COMMON="$HERE/../lib/common.sh"
    else
        COMMON="$HERE/../linux/common.sh"
    fi
fi

# shellcheck disable=SC1090
source "$COMMON" >/dev/null 2>&1

# Mock `ip` so detect_adapter_inventory classifies a fixed fleet of interfaces
# with NO real network I/O. Handles the two invocations the function makes:
#   `ip route show default`  -> default route line (picks wlo1 as the uplink)
#   `ip -br link`            -> one line per iface: NAME STATE MAC <FLAGS>
ip() {
    case "$*" in
        "route show default")
            echo "default via 192.168.1.1 dev wlo1 proto dhcp metric 600" ;;
        "-br link")
            cat <<'EOF'
lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>
wlo1             UP             a4:c3:f0:11:22:33 <BROADCAST,MULTICAST,UP,LOWER_UP>
wlp3s0           UP             a4:c3:f0:44:55:66 <BROADCAST,MULTICAST,UP,LOWER_UP>
wlx001122334455  DOWN           00:11:22:33:44:55 <BROADCAST,MULTICAST>
wlan0            UP             00:11:22:33:44:77 <BROADCAST,MULTICAST,UP,LOWER_UP>
wls3             UP             00:11:22:33:44:88 <BROADCAST,MULTICAST,UP,LOWER_UP>
eno1             UP             aa:bb:cc:dd:ee:01 <BROADCAST,MULTICAST,UP,LOWER_UP>
enp2s0           UP             aa:bb:cc:dd:ee:02 <BROADCAST,MULTICAST,UP,LOWER_UP>
enx00aabbccddee  UP             00:aa:bb:cc:dd:ee <BROADCAST,MULTICAST,UP,LOWER_UP>
eth0             UP             aa:bb:cc:dd:ee:03 <BROADCAST,MULTICAST,UP,LOWER_UP>
wwan0            DOWN           02:11:22:33:44:99 <BROADCAST,MULTICAST>
docker0          DOWN           02:42:aa:bb:cc:dd <BROADCAST,MULTICAST>
EOF
            ;;
        *) command ip "$@" 2>/dev/null || true ;;
    esac
}

detect_adapter_inventory

fails=0
checks=0

# Assert the classifier tagged $name with media_type $want in adapters_json.
expect_media() {
    local name="$1" want="$2"
    checks=$((checks + 1))
    # Pull the media_type of the object whose "name" is $name.
    local got
    got="$(printf '%s' "$adapters_json" \
        | grep -oE "\{[^}]*\"name\":\"$name\"[^}]*\}" \
        | grep -oE '"media_type":"[^"]*"' | head -1 | cut -d'"' -f4)"
    if [[ "$got" != "$want" ]]; then
        echo "FAIL: $name -> media_type [$got], want [$want]"
        fails=$((fails + 1))
    fi
}

# Wireless: every wl* form, including the previously-missed onboard wlo* and
# slot wls* (the actual bug) plus the already-covered wlp/wlx/wlan.
expect_media wlo1            wireless   # onboard/LOM radio — the regression
expect_media wlp3s0          wireless   # PCI
expect_media wlx001122334455 wireless   # USB (MAC-named)
expect_media wlan0           wireless   # legacy
expect_media wls3            wireless   # hotplug slot
# Wired: en*/eth* variants.
expect_media eno1            wired
expect_media enp2s0          wired
expect_media enx00aabbccddee wired
expect_media eth0            wired
# Neither: WWAN cellular and virtual bridges must NOT be tagged wireless (a
# wireless-only sim on a cellular modem would never associate to an SSID).
expect_media wwan0           other
expect_media docker0         other
# lo is skipped entirely — it must not appear in the inventory.
checks=$((checks + 1))
if printf '%s' "$adapters_json" | grep -q '"name":"lo"'; then
    echo "FAIL: loopback 'lo' should be excluded from the inventory"
    fails=$((fails + 1))
fi

if [[ $fails -eq 0 ]]; then
    echo "OK: $checks checks passed ($COMMON)"
    exit 0
fi
echo "FAILED: $fails of $checks checks ($COMMON)"
exit 1
