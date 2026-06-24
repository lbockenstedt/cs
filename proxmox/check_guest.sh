#!/bin/bash
# check_guest.sh — Monitor QEMU guest agent responsiveness and auto-recover hung VMs.
#
# Logic per VM (VMID > 90000):
#   - Agent responds       → update /tmp/<vmid>.lastup timestamp
#   - No response < 10min  → log and skip (may be rebooting)
#   - No response 10-20min → reset (soft recovery)
#   - No response > 20min  → unlock + stop + start (hard power cycle)
#
# Add to cron to run every 5 minutes:
#   */5 * * * * root bash /etc/pve/scripts/check_guest.sh
#
# NOTE: /etc/pve is pmxcfs — never chmod +x files here.
# Always invoke with: bash /etc/pve/scripts/check_guest.sh

LOG="/tmp/check_guest.log"
QM="/usr/sbin/qm"

# Truncate log at start of each run
echo "$(date) - Running Agent Checks on VMs" > "$LOG"

log() { echo "$(date) $*" >> "$LOG"; }

for conf in /etc/pve/qemu-server/*.conf; do
    # Skip if glob matched nothing
    [[ -f "$conf" ]] || continue

    # Extract VMID from filename (e.g. /etc/pve/qemu-server/90001.conf → 90001)
    base="${conf##*/}"
    n="${base%.conf}"

    # Guard: skip if not a number (e.g. template files or backup artifacts)
    [[ "$n" =~ ^[0-9]+$ ]] || continue

    # Only manage VMs in the client-sim range (> 90000)
    (( n > 90000 )) || continue

    if "$QM" agent "$n" ping >/dev/null 2>&1; then
        # Agent is alive — refresh the heartbeat file
        log "VM $n is responding — updating lastup"
        touch "/tmp/${n}.lastup"
    else
        # Agent is not responding — check how long it has been down.
        # If the lastup file doesn't exist, /tmp was cleared (host rebooted).
        # By design: start the VM so it comes back up automatically after a reboot.
        if [[ ! -f "/tmp/${n}.lastup" ]]; then
            log "VM $n — no lastup file (host likely rebooted), starting VM"
            "$QM" start "$n" 2>/dev/null || log "VM $n — start skipped (may already be running)"
            continue
        fi

        timestamp="$(date -r "/tmp/${n}.lastup" +%s)"
        now_minus_20="$(date -d "20 minutes ago" +%s)"
        now_minus_10="$(date -d "10 minutes ago" +%s)"

        if (( timestamp < now_minus_20 )); then
            # Down for more than 20 minutes — hard power cycle
            log "VM $n has not responded for more than 20 minutes — power cycling"
            "$QM" unlock "$n"
            "$QM" stop "$n"
            # Wait up to 30s for the VM to fully stop before restarting
            for (( i=0; i<30; i++ )); do
                sleep 1
                status=$("$QM" status "$n" 2>/dev/null | awk '{print $2}')
                [[ "$status" == "stopped" ]] && break
            done
            "$QM" start "$n"

        elif (( timestamp < now_minus_10 )); then
            # Down 10–20 minutes — soft reset (less disruptive than power cycle)
            log "VM $n has not responded for more than 10 minutes — resetting"
            "$QM" reset "$n"

        else
            # Down less than 10 minutes — may just be rebooting, leave it alone
            log "VM $n has not responded for less than 10 minutes — skipping"
        fi
    fi
done
