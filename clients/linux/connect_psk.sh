#!/bin/bash
# connect_psk.sh — PSK (personal) wifi connect functions for simulation.sh
# ----------------------------------------------------------------------------
# Sourced after network_common.sh. Owns the two PSK connect paths:
#   connect_wifi       — the normal, genuine associate (also the 1X dispatcher).
#   connect_wifi_fail  — the fast wrong-password loop for the ssidpw_fail sim.
#
# Readable over clever. The one tricky bit (background nmcli + wait on its PID
# for an event-driven result) is explained inline in each function — do NOT
# "simplify" it back to a blocking `nmcli -w` call; that reintroduces the
# connect thrash documented in _connect_outcome (network_common.sh).
#
# Depends on (set by simulation.sh before these are CALLED, not when sourced):
#   $ssid, $ssidpw, $wsite, $site_based_ssid — the SSID + creds to connect with
#   $wladapter                              — from detect_wlan_adapter (common.sh)
# Plus the shared state/helpers in network_common.sh: _reconnect_fails,
# _RADIO_CYCLE_AFTER, _wifi_busy, _wait_radio_ready, _wait_ssid_seen,
# _connect_outcome, delete_matching_connections.
version=1.0

# connect_wifi — the primary PSK wifi associate.
#   $1 = wait      — nmcli -w backstop, in seconds (the silent-AP cap; we return
#                    the instant nmcli finishes, NOT after this whole window).
#   $2 = reset     — pass the literal word "reset" to FORCE a radio cycle now
#                    (used at the explicit "reset the adapter" recovery site).
#   $3 = scan_cap  — max seconds to wait for the SSID to appear in the scan cache
#                    before connecting. Empty -> ramp up from _reconnect_fails.
#   $4 = track     — 1 (default) = adjust _reconnect_fails on success/failure.
#                    0 = don't (used by the fail-sim/flap paths whose expected
#                    failures must not pollute the genuine-reconnect ramp).
connect_wifi() {
  local wait_time="${1:-180}" reset="${2:-}" scan_cap="${3:-}" track="${4:-1}"

  # An 802.1X (enterprise) SSID is flagged by ssid == "1X" in the SSID matrix —
  # route it to the 1X path in connect_1x.sh. Every other SSID is PSK, handled
  # below.
  if [[ "$ssid" == "1X" ]]; then
    connect_1x "$wait_time" "$reset"
    return
  fi

  # Build the SSID we actually connect to. With site_based_ssid=on the SSID is
  # prefixed with the site name so the same bucket can map to different SSIDs
  # per site.
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi

  # ---- Radio cycle (only as a LAST resort) ---------------------------------
  # Cycle the radio only when the caller forces it (reset) OR we've failed
  # _RADIO_CYCLE_AFTER (5) times in a row since the last success. The early
  # retries just wait longer on the scan-wait ramp with the scan cache kept WARM
  # — a radio bounce wipes that cache, which stranded slow drivers (the SSID can
  # take ~1 min to surface). Only a persistent failure escalates to a bounce.
  # Never cycle while the adapter is mid-association (would tear down a working
  # connect + wipe the cache).
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

  # ---- Wait for the SSID to appear in the scan cache -----------------------
  # _wait_radio_ready only means the radio is up, NOT that a scan completed.
  # Cap ramps +5s per consecutive failure up to 60s, reset to 20s on success.
  if [[ -z "$scan_cap" ]]; then
    scan_cap=$(( 20 + 5 * ${_reconnect_fails:-0} ))
    if (( scan_cap > 60 )); then scan_cap=60; fi
  fi
  _wait_ssid_seen "$target_ssid" "$scan_cap" || true

  # ---- Event-driven connect ------------------------------------------------
  # Launch nmcli in the BACKGROUND and wait on its PID. nmcli -w returns the
  # instant it reaches ACTIVATED (success) or the AP drops the link (failure) —
  # it does NOT block for the whole -w window, which is only a silent-AP
  # backstop. Waiting on the bg PID gives us that exit code the moment nmcli
  # finishes. This is load-bearing; see _connect_outcome (network_common.sh).
  nmcli -w "$wait_time" device wifi connect "$target_ssid" password "$ssidpw" >/dev/null 2>&1 &
  if _connect_outcome "$wait_time" $!; then
    if [[ "$track" == "1" ]]; then
      _reconnect_fails=0
      echo "  [connect] SUCCESS — scan-wait ramp reset to 20s" | tee -a ${LOG_FILE}
    fi
    return 0
  fi
  if [[ "$track" == "1" ]]; then
    _reconnect_fails=$((_reconnect_fails + 1))
    local next_cap=$(( 20 + 5 * _reconnect_fails )); (( next_cap > 60 )) && next_cap=60
    echo "  [connect] FAILED — reconnect-fails now ${_reconnect_fails}; next scan-wait up to ${next_cap}s" | tee -a ${LOG_FILE}
  fi
  return 1
}

# connect_wifi_fail — fast wrong-PSK loop for the ssidpw_fail sim.
#   $1 = cap — nmcli -w backstop in seconds (default 5).
# The normal connect_wifi cycles the radio + scan-waits, capping the wrong-
# password loop at ~4 attempts/min. To trigger the "WPA Passphrase is Incorrect"
# insight at least 10x/min we need <=6s/attempt. The AP records the failed WPA
# 4-way handshake within ~1-2s of the association request, so a SHORT nmcli cap
# still registers the event. We drop the scan-wait, delete saved profiles first
# (each attempt is then a distinct AP event), and cap nmcli at $1 sec.
# PSK-only here — 1X wrong-password uses connect_1x_fail (connect_1x.sh).
connect_wifi_fail() {
  local cap="${1:-5}"
  delete_matching_connections
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi
  # Same background-nmcli + wait-on-PID trick as connect_wifi: return the instant
  # the AP rejects the bad PSK (deauth — the real "WPA Passphrase Incorrect"
  # event) instead of blocking for the whole cap. $cap is only a silent-AP
  # backstop.
  nmcli -w "$cap" device wifi connect "$target_ssid" password "$ssidpw" >/dev/null 2>&1 &
  _connect_outcome "$cap" $! || true
}