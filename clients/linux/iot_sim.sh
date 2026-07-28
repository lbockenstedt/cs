#!/bin/bash
# iot_sim.sh — T3 IoT-fleet ("vwlan") simulation mode for the linux client.
#
# Sourced by simulation.sh AFTER common.sh (needs get_value + $LOG_FILE + the
# _rsleep helper). This is the ported T3 engine: one host stands up many virtual
# WiFi station interfaces (vwlan1..N), each a distinct IoT device with a vendor
# OUI MAC + a per-device DHCP fingerprint, then generates realistic per-device
# traffic. It replaces the RESULT of clients/t3/wireless.sh — NOT its structure.
#
# What changed vs the legacy wireless.sh (deliberately dropped):
#   * device data is 100% data-driven from clients/t3/iot_catalog.json via
#     catalog.py (--emit-run-plan / --emit-traffic) — no 19-branch hardcoded
#     case statement, no per-device inline curl/dig/firefox;
#   * NO inline self-update / chmod 777 / shutdown -r (update.sh owns updates);
#   * the `active=5` bug (only ever simulated PlaySignage) is gone — every
#     device is exercised in turn;
#   * the vwlan10 numbering gap is gone (indices are contiguous from the plan).
#
# The ONE genuinely T3-specific mechanic kept: iw multi-VIF on the WiFi phy
# (the passed-through T3 adapter, or mac80211_hwsim in a lab host) + per-VIF
# wpa_supplicant associate + dhcpcd with the catalog opt60/opt55 fingerprint.
# Raw iw/wpa_supplicant/dhcpcd (not nmcli): NetworkManager manages the single
# T1/T2 adapter, but can't cleanly own N station VIFs on one phy, so the vwlan
# interfaces are marked unmanaged and driven directly.

# Deployed script dir (same as simulation.sh). iot_catalog.json + catalog.py are
# shipped here by install.sh/update.sh.
IOT_SCRIPTS_DIR="/usr/local/scripts"
IOT_CATALOG_PY="${IOT_SCRIPTS_DIR}/catalog.py"
IOT_CATALOG_JSON="${IOT_SCRIPTS_DIR}/iot_catalog.json"
IOT_WPA_CONF="/tmp/iot-wpa.conf"
# HTTP/DNS dispatch bounds — keep a single device's traffic sweep short so the
# outer cadence stays responsive (mirrors the linux client's bounded helpers).
IOT_HTTP_TIMEOUT=8
IOT_DNS_TIMEOUT=3

_iot_log() { echo "iot: $*" | tee -a "${LOG_FILE:-/dev/stderr}"; }

# detect_t3_pci — true (0) when this guest exposes a PCI device whose vid:pid is
# in the cs-module T3 allow-list. That device is the passed-through onboard WiFi
# adapter that designates the guest a T3 IoT host (the SAME t3_pci_vidpids the
# pxmx agent provisions against — one source of truth). The list arrives in
# simulation.conf [simulation] t3_pci_vidpids as a space/comma list of
# "vvvv:pppp". lspci -Dn prints "<addr> <class>: <vvvv:pppp> ...".
detect_t3_pci() {
  local list; list=$(get_value 'simulation' 't3_pci_vidpids' 2>/dev/null)
  [[ -z "$list" ]] && return 1
  command -v lspci >/dev/null 2>&1 || return 1
  local want present
  want=$(printf '%s' "$list" | tr 'A-Z,' 'a-z ' | tr -s ' ' '\n' \
         | grep -E '^[0-9a-f]{4}:[0-9a-f]{4}$')
  [[ -z "$want" ]] && return 1
  present=$(lspci -Dn 2>/dev/null | grep -oE '[0-9a-f]{4}:[0-9a-f]{4}')
  [[ -z "$present" ]] && return 1
  # 0 if any configured vid:pid is present on the guest's PCI bus.
  grep -Fxq -f <(printf '%s\n' "$want") <(printf '%s\n' "$present")
}

# catalog.py needs iot_catalog.json alongside it (CATALOG_PATH is relative to the
# script). We copy the json next to the deployed catalog.py, so a plain call
# resolves it. Returns non-zero (and logs) when the toolchain isn't deployed.
iot_available() {
  local missing=""
  [[ -f "$IOT_CATALOG_PY" ]]   || missing+=" catalog.py"
  [[ -f "$IOT_CATALOG_JSON" ]] || missing+=" iot_catalog.json"
  command -v iw          >/dev/null 2>&1 || missing+=" iw"
  command -v dhcpcd      >/dev/null 2>&1 || missing+=" dhcpcd"
  command -v python3     >/dev/null 2>&1 || missing+=" python3"
  if [[ -n "$missing" ]]; then
    _iot_log "unavailable — missing:${missing}"
    return 1
  fi
  return 0
}

# Emit the run plan TSV (iface id oui hostname opt60 opt55), honoring a
# configurable interface cap (iot_max_ifaces, default 25 = the hwsim limit).
iot_run_plan() {
  local maxif; maxif=$(get_value 'simulation' 'iot_max_ifaces'); maxif="${maxif:-25}"
  [[ "$maxif" =~ ^[0-9]+$ ]] || maxif=25
  python3 "$IOT_CATALOG_PY" --emit-run-plan --max "$maxif" 2>>"${LOG_FILE:-/dev/null}"
}

# Deterministic host octet for the MAC (same formula as clients/t3/gen_macs.sh:
# md5(hostname) first two hex chars) so a given hostname always maps to the same
# MAC block — stable device identity across reboots.
iot_host_oct() { printf "%s" "$(hostname)" | md5sum | cut -c1-2; }

# Pick the WiFi phy to build VIFs on. On real T3 hardware this is the
# passed-through adapter; on a lab/software host with none, load mac80211_hwsim
# with enough radios. Echoes the phy name (empty on failure).
iot_pick_phy() {
  local need="${1:-25}" phy
  phy=$(ls /sys/class/ieee80211/ 2>/dev/null | head -1)
  if [[ -z "$phy" ]]; then
    _iot_log "no WiFi phy present — loading mac80211_hwsim radios=$((need + 1))"
    sudo modprobe mac80211_hwsim "radios=$((need + 1))" 2>>"${LOG_FILE:-/dev/null}" || true
    sleep 2
    phy=$(ls /sys/class/ieee80211/ 2>/dev/null | head -1)
  fi
  echo "${phy:-phy0}"
}

# Write the shared wpa_supplicant config used by every vwlan. Open network when
# no PSK, WPA-PSK otherwise. (1X per-VIF is a future add — the fleet associates
# with the lab PSK today, matching the legacy wireless.sh.)
iot_write_wpa_conf() {
  local ssid="$1" psk="$2"
  if [[ -z "$ssid" ]]; then return 1; fi
  {
    echo "ctrl_interface=/run/wpa_supplicant"
    echo "network={"
    echo "    ssid=\"${ssid}\""
    if [[ -n "$psk" ]]; then
      echo "    psk=\"${psk}\""
    else
      echo "    key_mgmt=NONE"
    fi
    echo "}"
  } > "$IOT_WPA_CONF" 2>/dev/null || return 1
  chmod 600 "$IOT_WPA_CONF" 2>/dev/null || true
  return 0
}

# Create + associate + DHCP one vwlan interface for a plan row. Idempotent: an
# interface that already exists is left as-is (a re-entry each outer cycle must
# not tear down a working device). Returns 0 on create/attempt.
_iot_bring_up_one() {
  local phy="$1" host_oct="$2" iface="$3" oui="$4" host="$5" opt60="$6" opt55="$7"
  local dev="vwlan${iface}"
  local mac; mac=$(printf "%s:07:%s:%02d" "$oui" "$host_oct" "$iface")
  if ip link show "$dev" >/dev/null 2>&1; then
    return 0   # already up — leave the running device alone
  fi
  sudo iw phy "$phy" interface add "$dev" type station addr "$mac" 2>>"${LOG_FILE:-/dev/null}" \
    || { _iot_log "! ${dev} ${mac} — iw add failed"; return 1; }
  # Keep NetworkManager off the vwlan devices (it owns the single T1/T2 adapter).
  nmcli device set "$dev" managed no >/dev/null 2>&1 || true
  sudo ip link set "$dev" up 2>>"${LOG_FILE:-/dev/null}" || true
  # Associate to the lab SSID, then request a lease with THIS device's DHCP
  # fingerprint: -h hostname, -i opt60 (vendor-class-id), -o opt55 (requested
  # options) — the shape the NAC/Central profiler classifies the device by.
  if [[ -s "$IOT_WPA_CONF" ]]; then
    sudo wpa_supplicant -B -i "$dev" -c "$IOT_WPA_CONF" >>"${LOG_FILE:-/dev/null}" 2>&1 || true
  fi
  sudo dhcpcd -h "$host" ${opt60:+-i "$opt60"} ${opt55:+-o "$opt55"} "$dev" \
    >>"${LOG_FILE:-/dev/null}" 2>&1 || true
  _iot_log "+ ${dev} ${mac} (${host})"
  return 0
}

# Bring the whole fleet up from the run plan. Populates IOT_IFACE_IDS (parallel
# arrays: index → device id) so the traffic sweep knows each interface's device.
declare -a IOT_IFACE_NUMS=()
declare -a IOT_IFACE_DEV_IDS=()
iot_bring_up_fleet() {
  local ssid="$1" psk="$2" phy host_oct
  IOT_IFACE_NUMS=(); IOT_IFACE_DEV_IDS=()
  local plan; plan=$(iot_run_plan)
  if [[ -z "$plan" ]]; then _iot_log "empty run plan — nothing to build"; return 1; fi
  local count; count=$(echo "$plan" | tail -n +2 | grep -c .)
  phy=$(iot_pick_phy "$count")
  host_oct=$(iot_host_oct)
  _iot_log "building ${count} vwlan device(s) on phy=${phy} (ssid=${ssid:-<none>})"
  iot_write_wpa_conf "$ssid" "$psk" || _iot_log "no ssid — interfaces up without association"
  # Skip the TSV header, then one row per interface.
  while IFS=$'\t' read -r iface id oui host opt60 opt55; do
    [[ "$iface" == "iface" || -z "$iface" ]] && continue
    _iot_bring_up_one "$phy" "$host_oct" "$iface" "$oui" "$host" "$opt60" "$opt55"
    IOT_IFACE_NUMS+=("$iface")
    IOT_IFACE_DEV_IDS+=("$id")
  done <<< "$plan"
  return 0
}

# Run one device's catalog traffic over its interface. dns → nslookup, http/curl
# → curl, wget → wget — all bounded, all best-effort (the point is to put the
# device's real endpoints on the wire for stats/telemetry, not to succeed).
iot_device_traffic() {
  local id="$1" dev="$2" kind target
  while IFS=$'\t' read -r kind target; do
    [[ -z "$kind" || -z "$target" ]] && continue
    case "$kind" in
      dns)
        nslookup -timeout="$IOT_DNS_TIMEOUT" "$target" >/dev/null 2>&1 || true ;;
      http|curl)
        curl --interface "$dev" -s -k -m "$IOT_HTTP_TIMEOUT" -o /dev/null "$target" 2>/dev/null || true ;;
      wget)
        wget -q --timeout="$IOT_HTTP_TIMEOUT" --tries=1 -O /dev/null "$target" 2>/dev/null || true ;;
    esac
  done < <(python3 "$IOT_CATALOG_PY" --emit-traffic "$id" 2>>"${LOG_FILE:-/dev/null}")
}

# One ambient sweep: walk every device once, firing its traffic. Honors the
# kill_switch (checked between devices) so a stop lands promptly.
iot_traffic_sweep() {
  local i id dev
  for i in "${!IOT_IFACE_NUMS[@]}"; do
    [[ "${kill_switch:-off}" == "on" ]] && { _iot_log "kill_switch on — halting sweep"; return 0; }
    id="${IOT_IFACE_DEV_IDS[$i]}"
    dev="vwlan${IOT_IFACE_NUMS[$i]}"
    iot_device_traffic "$id" "$dev"
  done
}

# Mode entry, called once per outer orchestrator cycle. Ensures the fleet is up
# (idempotent) and runs a single ambient sweep; the orchestrator handles the
# inter-cycle sleep + config reload. Returns 0 always (best-effort mode).
run_iot_simulation() {
  local ssid="$1" psk="$2"
  if ! iot_available; then
    _iot_log "toolchain unavailable — skipping iot cycle"
    return 0
  fi
  iot_bring_up_fleet "$ssid" "$psk"
  iot_traffic_sweep
  return 0
}
