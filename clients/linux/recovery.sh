#!/bin/bash
# recovery.sh — connection up/down management for simulation.sh
# ----------------------------------------------------------------------------
# Sourced after network_common.sh. Owns manage_connection, the up/down wrapper
# used by:
#   - the auth_fail flap loop (up with reset + down, repeated), and
#   - the "skip sims but stay associated" path (a plain up when sim-load gates).
#
# Readable over clever; the background-nmcli + wait-on-PID trick is explained
# inline — do NOT collapse it to a blocking nmcli call (see _connect_outcome in
# network_common.sh).
#
# Depends on (set by simulation.sh before this is CALLED, not when sourced):
#   $ssid, $wsite, $site_based_ssid, $wladapter
# Plus the shared state/helpers in network_common.sh.
version=0.01

# manage_connection — bring an existing nmcli connection up or down.
#   $1 = action     — "up" or "down".
#   $2 = wait_time  — nmcli -w backstop, in seconds.
#   $3 = reset      — "reset" to force a radio cycle (used by the auth_fail flap
#                     so each iter is a distinct AP event).
#   $4 = scan_cap   — max seconds to wait for the SSID in the scan cache before
#                     (re)associating. Empty -> ramp from _reconnect_fails.
#   $5 = track      — 1 (default) = adjust _reconnect_fails. 0 = don't (the
#                     auth_fail flap passes 0 so its expected deauths don't
#                     pollute the genuine-reconnect ramp).
manage_connection() {
  local action="$1" wait_time="$2" reset="${3:-}" scan_cap="${4:-}" track="${5:-1}"
  local target_ssid
  if [[ "$site_based_ssid" == "on" ]]; then
    target_ssid="$wsite-$ssid"
  else
    target_ssid="$ssid"
  fi

  # ---- down: deactivate immediately, no event watch needed -----------------
  if [[ "$action" == "down" ]]; then
    nmcli -w "$wait_time" connection down "$target_ssid" >/dev/null 2>&1 || true
    return
  fi

  # ---- up: cycle (only if forced/persistent-fail), scan-wait, then associate -
  # Same radio-cycle + scan-wait-ramp logic as connect_wifi / _connect_1x_normal.
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
  if [[ -z "$scan_cap" ]]; then
    scan_cap=$(( 20 + 5 * ${_reconnect_fails:-0} ))
    if (( scan_cap > 60 )); then scan_cap=60; fi
  fi
  _wait_ssid_seen "$target_ssid" "$scan_cap" || true

  # Event-driven: `nmcli connection up` returns the instant activation completes
  # (success) or the AP drops the link (failure — e.g. a blocked-MAC deauth in
  # the auth_fail flap). $wait_time is only the silent-AP backstop.
  nmcli -w "$wait_time" connection up "$target_ssid" >/dev/null 2>&1 &
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