#!/bin/bash
# network_common.sh — shared network helpers + connect state for simulation.sh
# ----------------------------------------------------------------------------
# Sourced by simulation.sh (and relied on by connect_psk.sh / connect_1x.sh /
# recovery.sh, which are sourced AFTER this file). Holds the connection-state
# variables and the low-level wait/detect helpers that every connect path uses.
#
# This file is intentionally readable over clever. A human should be able to
# open it, read a function top-to-bottom, and understand what it does. The one
# genuinely tricky pattern — launching nmcli in the background and waiting on
# its PID to get an event-driven result — lives in the connect_*.sh files, not
# here; the helpers below are all plain polls.
#
# Depends on (set by simulation.sh / common.sh before any helper is CALLED, not
# when this file is sourced):
#   $LOG_FILE   — log path (set at the top of simulation.sh)
#   $wladapter  — wifi adapter name (set by detect_wlan_adapter in common.sh)
#   $eadapter   — wired adapter name (set by detect_eth_adapter in common.sh)
version=0.02

# ----------------------------------------------------------------------------
# Connection state — shared across all connect paths.
# ----------------------------------------------------------------------------
# Consecutive genuine-connect failures since the last success — drives the
# scan-wait ramp (different drivers scan at different speeds; a flat long wait
# would delay every healthy connect) and gates the radio cycle (a fresh attempt
# doesn't cycle; a retry-after-failure does — cycling is a reset-on-failure, not
# routine). Incremented by connect_wifi / connect_1x / manage_connection on a
# failed connect, reset to 0 on success. Process-global — persists across loop
# iterations (one sourced while-loop), resets on re-exec. Excludes the fail-sim
# fast paths and the auth_fail flap (intentional cycling), which pass track=0.
_reconnect_fails=0

# How many consecutive genuine-connect failures before we escalate to a HARD
# radio cycle (nmcli radio off/on). The early retries just ride the scan-wait
# ramp with the scan cache kept WARM — a radio bounce wipes that cache, so on a
# slow driver (SSID can take ~1 min to surface) bouncing on every retry reset
# discovery to zero and the client never landed. Only a persistent failure
# (>= this many) now triggers the reset. An explicit `reset` arg still forces a
# cycle immediately (the "reset the adapter" recovery site).
_RADIO_CYCLE_AFTER=5

# delete_matching_connections — remove every saved NetworkManager wifi profile
# whose name contains "PSK". Forces a fresh association on the next connect
# (each attempt is then a distinct AP event, not a cached profile reuse). Used
# by the wrong-password / auth-fail loops and by the reset-adapter recovery path.
delete_matching_connections() {
  while IFS= read -r conn; do
    [[ -n "$conn" ]] && sudo nmcli con del "$conn"
  done < <(nmcli -t -f NAME con show 2>/dev/null | grep 'PSK' || true)
}

# _wifi_busy — is the wlan adapter actively connecting or already associated?
# Returns 0 (true -> BUSY, do NOT cycle the radio) when the device state is
# connecting* or connected*. Returns 1 (false -> idle, cycling is allowed) when
# the adapter is disconnected/unavailable/failed or no adapter is present.
# Used to VETO a radio cycle: bouncing the radio while NetworkManager is
# mid-association would tear that down and wipe the scan cache, forcing a slow
# driver to rediscover from scratch — the exact thrash we avoid. Reads
# $wladapter at call time.
_wifi_busy() {
  [[ -n "${wladapter:-}" ]] || return 1
  local st
  st=$(nmcli -t -f DEVICE,STATE device status 2>/dev/null | grep "^${wladapter}:" | head -1 | cut -d: -f2)
  [[ "$st" == connecting* || "$st" == connected* ]]
}

# _is_wifi_connected — is the wlan adapter fully activated (associated + has IP)?
# Used by the "skip sims but stay associated" path to decide whether a reconnect
# is actually needed, so we don't tear down a working link every iteration the
# sim-load gate trips. NM-managed wifi reports STATE=connected once activated.
_is_wifi_connected() {
  [[ -n "${wladapter:-}" ]] || return 1
  nmcli -t -f DEVICE,STATE device status 2>/dev/null | grep -q "^${wladapter}:connected"
}

# _wait_radio_ready — poll until the wifi radio leaves the post-power-on
# "unavailable" limbo and is ready to associate. Replaces the blind `sleep 15`
# that used to follow every radio cycle. Returns 0 the instant a wifi device is
# past "unavailable" (usually <15s, often ~1-2s); returns 1 after $1 sec
# (default 15) and the caller proceeds anyway — nmcli -w handles a not-yet-ready
# device itself. $1 = backstop cap in seconds.
_wait_radio_ready() {
  local cap="${1:-15}" i st
  echo "  [radio] waiting up to ${cap}s for wifi radio to leave 'unavailable'..." | tee -a ${LOG_FILE}
  for ((i=0; i<cap; i++)); do
    st=$(nmcli -t -f DEVICE,TYPE,STATE device status 2>/dev/null | grep ':wifi:' | head -1)
    if [[ -n "$st" && "$st" != *":unavailable" ]]; then
      echo "  [radio] ready after ${i}s (state: ${st##*:})" | tee -a ${LOG_FILE}
      return 0
    fi
    sleep 1
  done
  echo "  [radio] STILL unavailable after ${cap}s — proceeding anyway" | tee -a ${LOG_FILE}
  return 1
}

# _wait_ssid_seen — poll NetworkManager's scan cache until the target SSID's
# beacon has been heard (at least one scan result for it). _wait_radio_ready only
# confirms the radio left "unavailable"; it does NOT mean a scan completed, so
# connecting before the SSID is known fails with "No network with SSID found"
# and the recovery path misreads that as a dead adapter -> thrash. This polls the
# cache directly and returns 0 the instant the SSID appears (usually <10s, often
# ~2-5s); returns 1 after $2 sec (default 20) and the caller proceeds anyway
# (nmcli's own scan+connect is the backstop). $1 = SSID, $2 = cap sec. A rescan
# is kicked up front and again every 3 seconds so a passive/quiet channel
# doesn't strand us on the stale empty cache left by a radio cycle.
_wait_ssid_seen() {
  local ssid="$1" cap="${2:-20}" i
  [[ -z "$ssid" ]] && return 1
  echo "  [scan] waiting up to ${cap}s for SSID '${ssid}' to appear (reconnect-fails=${_reconnect_fails:-0})..." | tee -a ${LOG_FILE}
  nmcli device wifi rescan >/dev/null 2>&1 || true
  for ((i=0; i<cap; i++)); do
    if nmcli -t -f SSID device wifi list 2>/dev/null | grep -Fxq "$ssid"; then
      echo "  [scan] SSID '${ssid}' seen after ${i}s" | tee -a ${LOG_FILE}
      return 0
    fi
    if (( i > 0 && i % 3 == 0 )); then
      nmcli device wifi rescan >/dev/null 2>&1 || true
    fi
    sleep 1
  done
  echo "  [scan] SSID '${ssid}' NOT seen after ${cap}s — connecting blind (nmcli backstop)" | tee -a ${LOG_FILE}
  return 1
}

# _connect_outcome — wait for an in-flight nmcli activation (PID $2) to finish
# and return its exit code. nmcli is ALREADY an event-driven signal: `nmcli -w N`
# returns the instant NetworkManager reaches ACTIVATED (association + DHCP/IP) on
# success, or exits non-zero on failure (wrong PSK / blocked MAC / RADIUS reject
# / no network) — it does NOT blind-sleep, and every caller passes -w N as the
# backstop cap, so nmcli self-terminates at N sec. We just trust its exit code.
#
# This REPLACES an earlier `iw event` deauth-watcher race that killed HEALTHY
# connects (iw event has no -m flag, so the watcher errored out within
# milliseconds; the loop read that as a deauth and killed nmcli ~1s in, returning
# FAILURE on every connect the instant the SSID was found). $1 (cap) is kept for
# signature compatibility with every existing caller.
_connect_outcome() {
  local nm_pid="$2"
  wait "$nm_pid" 2>/dev/null
  return $?
}

# _wait_wlan_adapter — poll until a wlan adapter appears. Replaces the blind
# `sleep 15` that used to precede wladapter re-detection in the recovery paths.
# Sets $wladapter (via detect_wlan_adapter) and returns 0 the instant one is
# present; returns 1 after $1 sec (default 15). $1 = cap sec.
_wait_wlan_adapter() {
  local cap="${1:-15}" i
  for ((i=0; i<cap; i++)); do
    detect_wlan_adapter
    [[ -n "$wladapter" ]] && return 0
    sleep 1
  done
  return 1
}

# _wait_gateway — poll the default gateway until it answers. Replaces the blind
# `ping -c2 $dfgw` liveness check. _connect_outcome already confirms nmcli
# reached activated (association + DHCP + IP), so the route is present; the
# remaining unknown is whether the gateway has answered ARP / is pingable yet. A
# fixed `ping -c2` can false-negative on a slow ARP (triggering a spurious
# recovery) or waste time when it's already up. Returns 0 the instant it replies,
# 1 after $2 sec (default 10). $1 = gateway IP, $2 = cap sec.
_wait_gateway() {
  local gw="$1" cap="${2:-10}" tmo attempts=0
  [[ -z "$gw" ]] && return 1
  # Per-ping timeout tolerant of a slow-but-alive gateway under load — the old
  # fixed -W1 false-negated on any RTT >1s and triggered spurious recoveries.
  tmo=$(gw_ping_timeout_s 2>/dev/null); [[ "$tmo" =~ ^[0-9]+$ ]] || tmo=4
  local end=$(( $(date +%s) + cap ))
  # Return the instant any ping replies. Poll until the cap elapses, but always
  # try at least 5 times (any single reply = gateway up).
  while (( attempts < 5 || $(date +%s) < end )); do
    ping -c1 -W"$tmo" "$gw" >/dev/null 2>&1 && return 0
    attempts=$((attempts + 1))
    sleep 1
  done
  return 1
}

# ea_is_mgmt — is the wired ($eadapter) interface currently carrying a 169.253.*
# link-local management IP? Such an IP means this interface is the out-of-band
# management path and must NEVER be shut down (the sim would strand the box).
# Used to guard ea_down and the (currently disabled) offline-window link-down.
ea_is_mgmt() {
  [[ -n "$eadapter" ]] && ip -4 addr show dev "$eadapter" 2>/dev/null | grep -q "169\.253\."
}

# ea_down — shut down the wired adapter, UNLESS it's carrying a management IP
# (ea_is_mgmt). Used at the pre-sim "disable the unused interface" step.
ea_down() {
  if ea_is_mgmt; then
    echo "Blocked ethernet shutdown — management IP active on $eadapter" | tee -a ${LOG_FILE}
  elif [[ -n "$eadapter" ]]; then
    sudo ip link set dev "$eadapter" down
  fi
}