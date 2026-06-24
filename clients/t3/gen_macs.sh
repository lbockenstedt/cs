#!/bin/bash
# gen_macs.sh — Dynamic MAC address regeneration for T3 simulation
#
# Reads /usr/scripts/mac_config.json (pushed from API/WebUI), assigns each
# vendor OUI to sequential vwlan interfaces, live-updates the running
# interfaces, and rewrites 90-Wireless.rules for reboot persistence.
#
# mac_config.json format:
#   [{"vendor": "Apple", "oui": "3c:15:c2", "count": 5}, ...]
#   Total count across all entries must be ≤ 25.

SCRIPT_DIR="/usr/scripts"
MAC_CONFIG="$SCRIPT_DIR/mac_config.json"
OUI_POOL="$SCRIPT_DIR/oui_pool.csv"
UDEV_RULES="/etc/udev/rules.d/90-Wireless.rules"
HASH_FILE="/tmp/mac_config_hash"
LOGFILE="$SCRIPT_DIR/wireless.log"
MAX_IFACES=25

log() { echo "$1" | tee -a "$LOGFILE"; }

log "=== gen_macs.sh — $(date) ==="

# ── Sanity check ──────────────────────────────────────────────────────────────
if [ ! -f "$MAC_CONFIG" ]; then
    log "gen_macs: $MAC_CONFIG not found — aborting"
    exit 1
fi

# ── Detect WiFi PHY ───────────────────────────────────────────────────────────
PHY=$(ls /sys/class/ieee80211/ 2>/dev/null | head -1)
PHY="${PHY:-phy0}"
log "gen_macs: Using PHY=$PHY"

# ── Deterministic MAC host seed from hostname hash ────────────────────────────
HOST_OCT=$(printf "%s" "$(hostname)" | md5sum | cut -c1-2)
log "gen_macs: Hostname=$(hostname)  HOST_OCT=${HOST_OCT}"

# ── Parse mac_config.json → "iface_num oui vendor" lines ─────────────────────
MAPPING=$(python3 - <<'PYEOF'
import json, sys

try:
    with open('/usr/scripts/mac_config.json') as f:
        config = json.load(f)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

total = sum(int(e.get('count', 0)) for e in config)
if total > 25:
    print(f"ERROR: total count {total} exceeds maximum of 25", file=sys.stderr)
    sys.exit(1)
if total == 0:
    print("ERROR: total interface count is 0", file=sys.stderr)
    sys.exit(1)

iface = 1
for entry in config:
    vendor = entry.get('vendor', 'Unknown')
    oui    = entry.get('oui', '00:00:00').lower()
    count  = int(entry.get('count', 0))
    for _ in range(count):
        print(f"{iface} {oui} {vendor}")
        iface += 1
PYEOF
)

if [ $? -ne 0 ] || [ -z "$MAPPING" ]; then
    log "gen_macs: JSON parse failed — keeping existing MACs"
    exit 1
fi

TOTAL=$(echo "$MAPPING" | wc -l | tr -d ' ')
log "gen_macs: $TOTAL interface(s) to configure"

# ── Remove existing vwlan interfaces ─────────────────────────────────────────
log "gen_macs: Removing existing vwlan interfaces"
for i in $(seq 1 $MAX_IFACES); do
    iw dev "vwlan${i}" del 2>/dev/null && log "  Removed vwlan${i}" || true
done
sleep 1

# ── Create interfaces with new MACs and build udev rule lines ─────────────────
log "gen_macs: Creating interfaces with new MACs"
declare -a UDEV_LINES=()

while IFS=' ' read -r iface_num oui vendor; do
    mac="${oui}:07:${HOST_OCT}:$(printf "%02d" "$iface_num")"
    sudo iw phy "$PHY" interface add "vwlan${iface_num}" type station addr "$mac" 2>/dev/null \
        && log "  + vwlan${iface_num}  ${mac}  (${vendor})" \
        || log "  ! vwlan${iface_num}  ${mac}  — iw add failed (may need udev trigger)"
    UDEV_LINES+=("	RUN+=\"/usr/sbin/iw phy %k interface add vwlan${iface_num} type station addr ${mac}\"")
done <<< "$MAPPING"

# ── Write new udev rules for reboot persistence ───────────────────────────────
log "gen_macs: Writing $UDEV_RULES"
{
    printf 'ACTION=="add", SUBSYSTEM=="ieee80211", KERNEL=="%s", \\\n' "$PHY"
    for (( i=0; i<${#UDEV_LINES[@]}-1; i++ )); do
        printf '%s, \\\n' "${UDEV_LINES[$i]}"
    done
    printf '%s\n' "${UDEV_LINES[-1]}"
} > "$UDEV_RULES"

chmod -x "$UDEV_RULES"

# ── Reload udev ───────────────────────────────────────────────────────────────
udevadm control --reload-rules
log "gen_macs: udev rules reloaded"

# ── Save new hash so wireless.sh won't re-trigger this run ───────────────────
md5sum "$MAC_CONFIG" | awk '{print $1}' > "$HASH_FILE"
log "gen_macs: Done — hash saved to $HASH_FILE"
