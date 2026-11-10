#!/bin/bash
# open_ssid_test.sh — regression test for open-SSID routing in the Linux client.
#
# Checks:
#   - _ssid_auth (network_common.sh) honours ssid_auth and falls back to the old
#     SSID-name rule ("1X" = 802.1X, else PSK) when it is unset/unknown.
#   - connect_wifi (connect_psk.sh) routes open SSIDs to connect_open, and
#     connect_open builds a profile with no wifi-sec (no password), even when a
#     stale ssidpw from an earlier PSK cell is still set.
# nmcli and the radio/scan helpers are mocked, so no real network I/O happens.
#
# Usage: bash clients/tests/open_ssid_test.sh

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINUX="$HERE/../linux"
LOG_FILE=/dev/null

# shellcheck disable=SC1090
source "$LINUX/network_common.sh" >/dev/null 2>&1
# shellcheck disable=SC1090
source "$LINUX/connect_psk.sh" >/dev/null 2>&1
# shellcheck disable=SC1090
source "$LINUX/connect_open.sh" >/dev/null 2>&1

fails=0
# check — compare an actual value with the expected one and report.
check() {
    local name="$1" got="$2" want="$3"
    if [[ "$got" == "$want" ]]; then
        echo "PASS: $name"
    else
        echo "FAIL: $name (got '$got', want '$want')"
        fails=$((fails + 1))
    fi
}

# ---- _ssid_auth -------------------------------------------------------------
ssid="Guest"; ssid_auth="open"; check "explicit open" "$(_ssid_auth)" "open"
ssid="Guest"; ssid_auth="OPEN"; check "case-insensitive" "$(_ssid_auth)" "open"
ssid="1X";    ssid_auth="";     check "legacy 1X name" "$(_ssid_auth)" "1x"
ssid="Corp";  ssid_auth="";     check "legacy default psk" "$(_ssid_auth)" "psk"
ssid="Corp";  ssid_auth="bogus"; check "unknown -> name rule" "$(_ssid_auth)" "psk"
ssid="1X";    ssid_auth="open"; check "ssid_auth wins over name" "$(_ssid_auth)" "open"

# ---- connect_wifi -> connect_open ---------------------------------------------
NMCLI_LOG="$(mktemp)"
trap 'rm -f "$NMCLI_LOG"' EXIT
# Mocks: record every nmcli call; radio/scan/outcome helpers succeed instantly.
nmcli() { echo "$*" >>"$NMCLI_LOG"; return 0; }
_wifi_busy() { return 1; }
_wait_radio_ready() { :; }
_wait_ssid_seen() { return 0; }
_connect_outcome() { wait "$2" 2>/dev/null; return 0; }
connect_1x() { echo "connect_1x" >>"$NMCLI_LOG"; }

wladapter="wlan0"; wsite=""; site_based_ssid="off"
ssid="Guest"; ssid_auth="open"; ssidpw="stale-psk-pw"; _reconnect_fails=0
connect_wifi 5
rc=$?
check "open connect returns 0" "$rc" "0"
check "open profile added without security" \
    "$(grep -c '^connection add type wifi con-name Guest ifname wlan0 ssid Guest$' "$NMCLI_LOG")" "1"
check "no password passed on open" "$(grep -c 'password\|psk' "$NMCLI_LOG")" "0"
check "brought up by profile name" "$(grep -c '^-w 5 connection up Guest$' "$NMCLI_LOG")" "1"
check "1X path not taken" "$(grep -c '^connect_1x$' "$NMCLI_LOG")" "0"

if (( fails )); then
    echo "$fails check(s) failed"
    exit 1
fi
echo "all checks passed"
