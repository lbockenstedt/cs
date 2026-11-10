#!/bin/bash
# connect_open.sh — open (no encryption, no password) wifi connect for simulation.sh
# ----------------------------------------------------------------------------
# Sourced after network_common.sh. connect_wifi (connect_psk.sh) routes here
# when the client's SSID is open (ssid_auth=open — the SSID matrix cell's Auth).
# Owns:
#   connect_open                — the normal, genuine open associate.
#   _connect_open_build_profile — delete + re-add the open nmcli profile.
#
# Same flow as the PSK connect: radio cycle only as a last resort, wait for the
# SSID in the scan cache, then an event-driven connect. The only difference is
# that there is no password.
#
# The profile is rebuilt on every connect (like the 1X path) instead of using
# `nmcli device wifi connect`, which reuses a saved profile of the same name. If
# the cell was PSK before, that saved profile still asks for a WPA key and the
# open connect would fail. The profile is named after the SSID, so
# manage_connection (recovery.sh) and the mac_auth_fail loop can bring it up/down.
#
# Depends on (set by simulation.sh before these are CALLED, not when sourced):
#   $ssid, $wsite, $site_based_ssid — the SSID to connect to
#   $wladapter                      — from detect_wlan_adapter (common.sh)
# Plus the shared state/helpers in network_common.sh.
version=0.01

# _connect_open_build_profile — build an open (no wifi-sec) nmcli profile for
# $1 on $wladapter, deleting any existing profile of the same name first.
_connect_open_build_profile() {
  local target_ssid="$1"
  nmcli -t -f NAME connection show | grep -Fxq "$target_ssid" && nmcli connection delete "$target_ssid"
  nmcli connection add type wifi con-name "$target_ssid" ifname "$wladapter" ssid "$target_ssid"
}

# connect_open — the open wifi associate. Same arguments as connect_wifi:
#   $1 = wait      — nmcli -w backstop, in seconds (default 180).
#   $2 = reset     — "reset" to force a radio cycle now.
#   $3 = scan_cap  — max seconds to wait for the SSID in the scan cache.
#                    Empty -> ramp up from _reconnect_fails.
#   $4 = track     — 1 (default) = adjust _reconnect_fails; 0 = don't.
connect_open() {
  local wait_time="${1:-180}" reset="${2:-}" scan_cap="${3:-}" track="${4:-1}"
  local target_ssid
  if ! target_ssid=$(_target_ssid); then
    echo "  [connect] no SSID assigned yet (awaiting spoke placement) — skipping connect" | tee -a ${LOG_FILE}
    return 1
  fi

  # ---- Radio cycle (only as a LAST resort) ---------------------------------
  # Same rule as connect_wifi: cycle only on reset OR after _RADIO_CYCLE_AFTER
  # (5) consecutive failures, and never while the adapter is mid-association.
  if [[ "$reset" == "reset" || ( "$track" == "1" && "${_reconnect_fails:-0}" -ge "${_RADIO_CYCLE_AFTER:-5}" ) ]]; then
    if _wifi_busy; then
      echo "  [radio] cycle deferred — adapter mid-connect/associated, letting it finish" | tee -a ${LOG_FILE}
    else
      echo "  [radio] cycling radio (reset=${reset:-no}, reconnect-fails=${_reconnect_fails:-0})" | tee -a ${LOG_FILE}
      nmcli radio wifi off
      nmcli radio wifi on
      _wait_radio_ready 15
    fi
  fi

  # ---- Wait for the SSID to appear, then rebuild the profile ----------------
  if [[ -z "$scan_cap" ]]; then
    scan_cap=$(( 20 + 5 * ${_reconnect_fails:-0} ))
    if (( scan_cap > 60 )); then scan_cap=60; fi
  fi
  _wait_ssid_seen "$target_ssid" "$scan_cap" || true
  _connect_open_build_profile "$target_ssid" || return 1

  # ---- Event-driven connect ------------------------------------------------
  # Same background-nmcli + wait-on-PID trick as connect_wifi: nmcli returns the
  # instant the link is ACTIVATED or dropped; $wait_time is only the backstop.
  nmcli -w "$wait_time" connection up "$target_ssid" >/dev/null 2>&1 &
  if _connect_outcome "$wait_time" $!; then
    if [[ "$track" == "1" ]]; then
      _reconnect_fails=0
      echo "  [connect] SUCCESS (open) — scan-wait ramp reset to 20s" | tee -a ${LOG_FILE}
    fi
    return 0
  fi
  if [[ "$track" == "1" ]]; then
    _reconnect_fails=$((_reconnect_fails + 1))
    local next_cap=$(( 20 + 5 * _reconnect_fails )); (( next_cap > 60 )) && next_cap=60
    echo "  [connect] FAILED (open) — reconnect-fails now ${_reconnect_fails}; next scan-wait up to ${next_cap}s" | tee -a ${LOG_FILE}
  fi
  return 1
}
