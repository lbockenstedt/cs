#!/bin/bash
# connect_wired_1x.sh — wired 802.1X (auth_fail) + wired MAC-Auth-Bypass
# (mac_auth_fail) functions for simulation.sh
# ----------------------------------------------------------------------------
# Sourced after network_common.sh. Wired counterpart to connect_1x.sh /
# connect_psk.sh: auth_fail and mac_auth_fail are media="any" on the engine
# side (see sim_quota.py SIM_META) because a switch port doing 802.1X or
# MAC-Auth-Bypass (MAB) can reject a client exactly like an AP/RADIUS does
# over wifi — so both sims have a genuine wired path here, in ADDITION to the
# wireless one in connect_1x.sh / simulation.sh's wifi auth_fail flap.
# ssidpw_fail and assoc_fail stay wireless-only (a WPA passphrase / SSID
# association have no wired equivalent) — simulation.sh picks this file's
# functions vs. the wifi path based on sim_phy, never on adapter presence
# alone (see the sim_phy=="ethernet" branch in the fail-sim block).
#
# Depends on (set by simulation.sh before these are CALLED, not when sourced):
#   $eadapter                          — from detect_eth_adapter (common.sh)
#   $username, $dot1x_eap, $dot1x_password, $ssidpw
#                                      — SAME identity/password scheme as the
#                                        wireless 1X path (connect_1x.sh)
#   $mac_auth_fail_mac                 — the SAME shared, predictable spoofed
#                                         MAC the wireless mac_auth_fail uses
#                                        (one known RADIUS/ClearPass MAC-Auth
#                                         deny-list entry, wired or wireless)
# Plus the shared helpers in network_common.sh (_connect_outcome, ea_is_mgmt).
version=0.01

# _wired_1x_conn_name — the nmcli connection-profile name for the wired
# 802.1X fail loop. Keyed off $eadapter so it can never collide with a plain
# DHCP "Wired connection" profile NM may already have auto-created for the
# same interface.
_wired_1x_conn_name() {
  echo "wired-1x-${eadapter:-eth}"
}

# _wired_mab_conn_name — same idea for the MAC-Auth-Bypass profile.
_wired_mab_conn_name() {
  echo "wired-mab-${eadapter:-eth}"
}

# _wired_auth_fail_build_profile — build (delete + add) the wired 802.1X
# profile on $eadapter with a DELIBERATELY WRONG password. Mirrors
# _connect_1x_build_profile's PEAP-MSCHAPv2 identity/password scheme
# (connect_1x.sh) but as an `ethernet` connection type instead of `wifi` — a
# wired switch port doing 802.1X validates the exact same EAP identity/
# password pair a wireless RADIUS does. Rebuilt every call so each attempt is
# a fresh authentication (a distinct switch/RADIUS event), same as the
# wireless fast-fail path.
_wired_auth_fail_build_profile() {
  local name; name=$(_wired_1x_conn_name)
  [[ -z "${eadapter:-}" ]] && return 1
  nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$name" && nmcli connection delete "$name" >/dev/null 2>&1
  nmcli connection add type ethernet con-name "$name" ifname "$eadapter" \
    802-1x.eap "${dot1x_eap:-peap}" \
    802-1x.phase2-auth mschapv2 \
    802-1x.identity "$username" \
    802-1x.password "${dot1x_password:-$ssidpw}_fail" \
    802-1x.system-ca-certs no \
    connection.autoconnect no >/dev/null 2>&1
}

# wired_auth_fail_run — the wired 802.1X bad-credential loop for auth_fail.
# Same shape as the wireless auth_fail flap in simulation.sh (100 iterations,
# rebuild + up + down each time) but drives an `ethernet` profile on
# $eadapter instead of flapping a wifi SSID connection — the switch/RADIUS
# rejects the bad EAP credentials on every attempt. mgmt-guarded: skip
# entirely if $eadapter is carrying the out-of-band management IP (never
# take THAT interface down/reconfigure it).
wired_auth_fail_run() {
  local name; name=$(_wired_1x_conn_name)
  if [[ -z "${eadapter:-}" ]]; then
    echo "  [wired auth_fail] no wired adapter detected — skipping" | tee -a ${LOG_FILE}
    return 1
  fi
  if ea_is_mgmt; then
    echo "  [wired auth_fail] skipped — $eadapter carries the mgmt IP" | tee -a ${LOG_FILE}
    return 1
  fi
  echo "Running Wired 802.1X Auth Failure (iface=${eadapter})" | tee -a ${LOG_FILE}
  for i in {1..100}; do
    echo Iteration $i of 100 | tee -a ${LOG_FILE}
    _wired_auth_fail_build_profile
    # Event-driven: `nmcli connection up` returns the instant the switch/
    # RADIUS rejects the bad EAP creds instead of blocking for the whole -w
    # window. $wait is only the silent-switch backstop (see _connect_outcome,
    # network_common.sh).
    nmcli -w 5 connection up "$name" >/dev/null 2>&1 &
    _connect_outcome 5 $! || true
    nmcli -w 5 connection down "$name" >/dev/null 2>&1 || true
  done
}

# _wired_mac_auth_fail_build_profile — build (delete + add) a PLAIN (no
# 802.1X) DHCP ethernet profile on $eadapter with its cloned MAC pinned to
# the SAME shared $mac_auth_fail_mac the wireless mac_auth_fail uses. A
# switch's MAC-Auth-Bypass (MAB) check evaluates the port's source MAC
# before/instead of 802.1X, so a plain profile with the spoofed MAC is
# enough to trigger (and fail) MAB.
_wired_mac_auth_fail_build_profile() {
  local name; name=$(_wired_mab_conn_name)
  [[ -z "${eadapter:-}" ]] && return 1
  nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$name" && nmcli connection delete "$name" >/dev/null 2>&1
  nmcli connection add type ethernet con-name "$name" ifname "$eadapter" \
    ethernet.cloned-mac-address "$mac_auth_fail_mac" \
    connection.autoconnect no >/dev/null 2>&1
}

# wired_mac_auth_fail_run — wired MAC-Auth-Bypass (MAB) deny loop. Pins the
# SAME predictable spoofed MAC ($mac_auth_fail_mac) onto $eadapter and cycles
# the connection up/down repeatedly so the switch re-evaluates MAB on every
# link-up and denies it every time — the wired equivalent of the wireless
# mac_auth_fail spoofed-association loop. mgmt-guarded like wired auth_fail.
wired_mac_auth_fail_run() {
  local name; name=$(_wired_mab_conn_name)
  if [[ -z "${eadapter:-}" ]]; then
    echo "  [wired mac_auth_fail] no wired adapter detected — skipping" | tee -a ${LOG_FILE}
    return 1
  fi
  if ea_is_mgmt; then
    echo "  [wired mac_auth_fail] skipped — $eadapter carries the mgmt IP" | tee -a ${LOG_FILE}
    return 1
  fi
  echo "Running Wired MAC Auth Failure (spoofed MAC deny-list test, target=${mac_auth_fail_mac}, iface=${eadapter})" | tee -a ${LOG_FILE}
  _wired_mac_auth_fail_build_profile
  for i in {1..100}; do
    echo Iteration $i of 100 | tee -a ${LOG_FILE}
    nmcli -w 5 connection up "$name" >/dev/null 2>&1 &
    _connect_outcome 5 $! || true
    # Log the ACTUAL interface MAC read from sysfs (belt-and-suspenders,
    # mirrors the wireless mac_auth_fail loop) so a spoof that silently
    # doesn't land is diagnosable from the log alone.
    _mac_actual=$(cat "/sys/class/net/${eadapter}/address" 2>/dev/null)
    echo "  [wired mac_auth_fail] target_mac=${mac_auth_fail_mac} actual_iface_mac=${_mac_actual}" | tee -a ${LOG_FILE}
    nmcli -w 5 connection down "$name" >/dev/null 2>&1 || true
  done
  # Delete the spoofed profile so a subsequent maintenance reconnect (plain
  # DHCP) never inherits the cloned MAC.
  nmcli connection delete "$name" >/dev/null 2>&1 || true
}

# wired_1x_restore — maintenance step after either wired fail loop: delete
# both fail-sim profiles and bring the plain DHCP connection back up on
# $eadapter so the box is reachable again for updates between cycles — the
# wired counterpart of the `connect_wifi 5` maintenance reconnect at the end
# of the wireless block.
wired_1x_restore() {
  [[ -z "${eadapter:-}" ]] && return 0
  nmcli connection delete "$(_wired_1x_conn_name)" >/dev/null 2>&1 || true
  nmcli connection delete "$(_wired_mab_conn_name)" >/dev/null 2>&1 || true
  # `nmcli device connect` activates the best-matching profile for this
  # device (or NM's auto-created default DHCP one) — the ethernet equivalent
  # of the wireless connect_wifi maintenance reconnect.
  nmcli -w 10 device connect "$eadapter" >/dev/null 2>&1 || true
}
