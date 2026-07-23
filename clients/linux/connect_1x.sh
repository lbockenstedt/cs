#!/bin/bash
# connect_1x.sh — 802.1X (WPA-Enterprise) wifi connect functions for simulation.sh
# ----------------------------------------------------------------------------
# Sourced after network_common.sh. Owns the 1X connect paths:
#   connect_1x        — the normal, genuine 1X associate (calls _connect_1x_normal).
#   connect_1x_fail   — the fast wrong-password 1X loop for ssidpw_fail (calls
#                       _connect_1x_fast).
#   _connect_1x_normal / _connect_1x_fast — the two plain implementations,
#                       split from the old dual-mode _connect_1x_core so each
#                       reads top-to-bottom with NO flag branching.
#   _connect_1x_build_profile — shared profile-build helper (delete + add).
#
# nmcli's "device wifi connect" is PSK-only, so 802.1X needs an explicit
# connection profile with 802-1x.* settings. The profile is rebuilt every run so
# identity / password / SSID always re-apply.
#
# Depends on (set by simulation.sh before these are CALLED, not when sourced):
#   $ssid, $wsite, $site_based_ssid, $username
#   $dot1x_eap, $dot1x_password, $ssidpw
#   $dot1x_client_cert, $dot1x_private_key, $dot1x_ca_cert  (EAP-TLS only)
#   $wladapter
# Plus the shared state/helpers in network_common.sh.
version=1.0

# connect_1x — normal genuine 1X associate. $1 = nmcli -w backstop (sec),
# $2 = "reset" to force a radio cycle. Tracks the reconnect ramp (track=1).
connect_1x() {
  _connect_1x_normal "$1" "$2"
}

# connect_1x_fail — fast wrong-password 1X loop for ssidpw_fail. $1 = nmcli -w
# backstop (default 5). Does NOT track the reconnect ramp: this is a fail-sim,
# its expected auth failures must not pollute the genuine-reconnect counter/ramp.
connect_1x_fail() {
  _connect_1x_fast "${1:-5}"
}

# _connect_1x_build_profile — build the 1X nmcli profile for $target_ssid on
# $wladapter. PEAP-MSCHAPv2 (username/password, the legacy default) or EAP-TLS
# (cert-based, for Cloud NAC — certs provisioned by cloud_nac_onboard.py).
# EAP identity is the short username; the PEAP password is dot1x_password if set
# (Cloud NAC per-user) else the shared SSID password $ssidpw. No server-cert
# validation (lab). Deletes any existing profile of the same name first.
# Returns 1 (and skips the add) if EAP-TLS is selected but a cert path is empty.
_connect_1x_build_profile() {
  local target_ssid="$1"
  local eap="${dot1x_eap:-peap}"

  nmcli -t -f NAME connection show | grep -Fxq "$target_ssid" && nmcli connection delete "$target_ssid"

  if [[ "$eap" == "tls" ]]; then
    # EAP-TLS (cert-based) — for Cloud NAC. No password/phase2.
    if [[ -z "$dot1x_client_cert" || -z "$dot1x_private_key" || -z "$dot1x_ca_cert" ]]; then
      echo "EAP-TLS selected but cert paths missing (dot1x_client_cert/private_key/ca_cert)" | tee -a ${LOG_FILE}
      return 1
    fi
    nmcli connection add type wifi con-name "$target_ssid" ifname "$wladapter" ssid "$target_ssid" \
      wifi-sec.key-mgmt wpa-eap \
      802-1x.eap tls \
      802-1x.identity "$username" \
      802-1x.client-cert "$dot1x_client_cert" \
      802-1x.private-key "$dot1x_private_key" \
      802-1x.ca-cert "$dot1x_ca_cert" \
      802-1x.system-ca-certs no
  else
    # PEAP-MSCHAPv2 (username/password) — the legacy default.
    nmcli connection add type wifi con-name "$target_ssid" ifname "$wladapter" ssid "$target_ssid" \
      wifi-sec.key-mgmt wpa-eap \
      802-1x.eap "$eap" \
      802-1x.phase2-auth mschapv2 \
      802-1x.identity "$username" \
      802-1x.password "${dot1x_password:-$ssidpw}" \
      802-1x.system-ca-certs no
  fi
}

# _connect_1x_normal — the genuine 1X associate.
#   $1 = wait_time — nmcli -w backstop (sec).
#   $2 = reset     — "reset" to force a radio cycle.
# Tracks the reconnect ramp (the genuine-connect path).
_connect_1x_normal() {
  local wait_time="$1" reset="${2:-}" scan_cap="" track=1
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi

  # ---- Radio cycle (only as a LAST resort) ---------------------------------
  # Same logic as connect_wifi: cycle only on reset OR after _RADIO_CYCLE_AFTER
  # (5) consecutive failures; early retries just extend the scan-wait ramp. Never
  # cycle while the adapter is mid-association.
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
  _connect_1x_build_profile "$target_ssid" || return 1

  # ---- Event-driven connect ------------------------------------------------
  # Same background-nmcli + wait-on-PID trick as connect_wifi: `nmcli connection
  # up` returns the instant it reaches ACTIVATED (success) or RADIUS rejects the
  # creds (deauth — failure). $wait_time is only the silent-AP backstop.
  nmcli -w "$wait_time" connection up "$target_ssid" >/dev/null 2>&1 &
  if _connect_outcome "$wait_time" $!; then
    _reconnect_fails=0
    echo "  [connect] SUCCESS — scan-wait ramp reset to 20s" | tee -a ${LOG_FILE}
    return 0
  fi
  _reconnect_fails=$((_reconnect_fails + 1))
  local next_cap=$(( 20 + 5 * _reconnect_fails )); (( next_cap > 60 )) && next_cap=60
  echo "  [connect] FAILED — reconnect-fails now ${_reconnect_fails}; next scan-wait up to ${next_cap}s" | tee -a ${LOG_FILE}
  return 1
}

# _connect_1x_fast — the wrong-password 1X loop for ssidpw_fail.
#   $1 = cap — nmcli -w backstop (default 5).
# No radio cycle / scan-wait / reconnect tracking — the profile delete + rebuild
# forces a fresh association each attempt (each is a distinct AP/RADIUS event),
# and a short cap still registers the failed EAP within ~1-2s so the loop
# sustains >=10 auth-failure attempts/min. The bad password (set by the caller
# before this runs) is what makes this fail fast on the deauth event.
_connect_1x_fast() {
  local cap="${1:-5}"
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi

  # Delete + rebuild the profile so each attempt is a fresh association.
  _connect_1x_build_profile "$target_ssid" || return 1

  # Event-driven: return the instant RADIUS rejects the creds (deauth) instead of
  # blocking for the whole cap. $cap is only a silent-AP backstop.
  nmcli -w "$cap" connection up "$target_ssid" >/dev/null 2>&1 &
  _connect_outcome "$cap" $! || true
}