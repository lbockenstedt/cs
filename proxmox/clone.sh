#!/bin/bash
# clone.sh — Proxmox VM clone manager for Client-Sim
# Clones, re-creates, deletes, or configures a range of VMs from a template.
#
# Usage:
#   bash clone.sh <command> <start_vmid> <end_vmid> <sleep_time> <bridge_id> <vlan_id> <pool_name> <vm_name> <tpl_id> <passthrough>
#
# Commands:
#   automated      Auto-detect USB adapters and create one VM per adapter.
#                  start_vmid/end_vmid are computed from the last 3 digits of the hostname.
#   re-create      Stop, destroy, and re-clone VMs in the given VMID range.
#   delete         Stop and destroy VMs in the given VMID range.
#   config         Push hostname and reboot to VMs in the given VMID range.
#
# Passthrough:
#   usb            Assign matched USB adapters to VMs (see vidpids list below).
#   pci            Assign matched PCIe adapters to VMs (see pcistr below).
#
# Example:
#   bash clone.sh delete 90001 90024 15 vmbr0 5 Simulations sim-rpi 100 usb
#
version="0.01"

source '/etc/pve/scripts/ini-parser.sh'
process_ini_file '/etc/pve/scripts/client-setup.conf'

# ---------------------------------------------------------------------------
# Known USB VID:PID pairs for WiFi adapters to pass through
# ---------------------------------------------------------------------------
vidpids=(
    "2357:012d"   # TP-Link Archer T4U
    "2357:011e"   # TP-Link Archer T3U
    "2357:012e"   # TP-Link Archer T3U Plus
    "0bda:8179"   # Realtek RTL8188EUS
    "0846:9041"   # Netgear A6100
    "35bc:0108"
)

# PCIe device string to match for PCI passthrough
pcistr="Renesas Electronics Corp. uPD720202"

# ---------------------------------------------------------------------------
# Host ID — last 3 digits of hostname, used in automated mode to compute
# a deterministic VM ID range so multiple hosts don't collide.
# ---------------------------------------------------------------------------
h=$(hostname)
last3="${h: -3}"
[[ "$last3" =~ ^[0-9]{3}$ ]] && host_id="$last3" || host_id=""

# ---------------------------------------------------------------------------
# Arguments
# NOTE: $2/$3 are start_vmid/end_vmid for manual modes.
#       In automated mode they are ignored and computed from the hostname.
# ---------------------------------------------------------------------------
cmd="${1}"
start_vmid="${2}"
end_vmid="${3}"
sleep_time="${4}"
bridge_id="${5}"
vlan_id="${6}"
pool_name="${7}"
vm_name="${8}"
tpl_id="${9}"
local_passthrough="${10}"

# ---------------------------------------------------------------------------
# USB detection — scan sysfs for devices matching the VID:PID list.
# LXC/container veth suffixes (e.g. eth0@if5) are stripped where needed.
# The '|| true' prevents pipefail from killing the script if sort finds nothing.
# ---------------------------------------------------------------------------
matches_vidpid() {
    local candidate="$1"
    for vp in "${vidpids[@]}"; do
        [[ "$vp" == "$candidate" ]] && return 0
    done
    return 1
}

mapfile -t devices < <(
    for dev in /sys/bus/usb/devices/*; do
        [[ -f "$dev/idVendor" && -f "$dev/idProduct" ]] || continue
        # Skip interface nodes (e.g. 1-1:1.0) — only want device nodes
        [[ "$dev" == *:* ]] && continue

        vid=$(<"$dev/idVendor")
        pid=$(<"$dev/idProduct")
        vidpid="$vid:$pid"

        if matches_vidpid "$vidpid"; then
            echo "$(basename "$dev")"
        fi
    done | sort -V || true
)

usb_count=${#devices[@]}

mapfile -t pci_devices < <(lspci | grep -i "$pcistr" | awk '{print $1}')

# ---------------------------------------------------------------------------
# AUTOMATED MODE
# Computes a 24-slot VMID range from the last 3 hostname digits so multiple
# Proxmox hosts can run this script without VMID collisions.
# One VM is created per detected USB adapter (up to 24).
# ---------------------------------------------------------------------------
if [[ -n "$host_id" && "$cmd" == "automated" ]]; then

    id_num=$((10#$host_id))

    # Each host owns a fixed 24-slot VMID block starting at 90001
    start_vmid=$((90000 + (id_num - 1) * 24 + 1))
    end_vmid=$((start_vmid + 23))

    # Automated mode always uses these defaults; ignore CLI args for these
    vm_name="sim-client"
    tpl_id="100"
    local_passthrough="usb"

    # Cap at 24 even if more adapters are found
    (( usb_count > 24 )) && usb_count=24

    echo "Host $host_id → VM range $start_vmid - $end_vmid"
    echo "USB devices found: $usb_count"

    for (( idx=0; idx<usb_count; idx++ )); do

        vmid=$((start_vmid + idx))
        dev="${devices[$idx]}"
        vm_name=$(get_value "c${vmid}" 'vm_name')
        vm_name="${vm_name:-sim-client}"

        echo "Creating VM $vmid ($vm_name) for USB device $dev"

        # Destroy any existing VM at this ID so clone is idempotent
        qm stop "$vmid" 2>/dev/null
        qm destroy "$vmid" --skiplock --purge --destroy-unreferenced-disks 2>/dev/null

        qm clone "$tpl_id" "$vmid" --name "${vm_name}"
        # Enable autostart; startup order 2 with 60s up-delay so the WebUI LXC
        # (order 1) is fully ready before clients try to connect to the API.
        qm set "$vmid" --onboot 1 --startup "order=2,up=60"
        qm start "$vmid"

        # Wait for the QEMU guest agent to become responsive before running commands
        timeout=600
        elapsed=0
        until qm guest ping "$vmid" >/dev/null 2>&1; do
            sleep 2
            elapsed=$((elapsed + 2))
            (( elapsed >= timeout )) && break
        done

        qm guest exec "$vmid" --timeout 60 -- hostnamectl set-hostname "${vm_name}"
        qm guest exec "$vmid" -- reboot

        # Wait for VM to come back up after reboot before running update.sh
        reboot_wait=0
        until qm guest ping "$vmid" >/dev/null 2>&1; do
            sleep 5
            reboot_wait=$((reboot_wait + 5))
            (( reboot_wait >= 600 )) && { echo "WARNING: VM $vmid did not come back after reboot"; break; }
        done

        qm guest exec "$vmid" --timeout 300 -- bash /usr/local/scripts/update.sh \
            && echo "update.sh completed on VM $vmid" \
            || echo "WARNING: update.sh failed on VM $vmid"

        # Trigger hub self-update so the hub pulls latest scripts too
        _hub_url=$(tr -d '[:space:]' < /var/lib/client-sim/hub-server-url 2>/dev/null || true)
        if [[ -n "$_hub_url" ]]; then
            _hub_http=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
                -X POST "${_hub_url}/api/self-update" 2>/dev/null || true)
            [[ "$_hub_http" == "200" ]] \
                && echo "Hub self-update triggered at ${_hub_url}" \
                || echo "WARNING: Hub self-update returned HTTP ${_hub_http:-000} (non-fatal)"
        fi

        # USB assignment goes into config now; device is available after reboot
        if [[ -n "$dev" ]]; then
            echo "Assigning USB $dev -> VM $vmid"
            qm set "$vmid" -usb0 "host=${dev}"
        fi

    done
fi

# ---------------------------------------------------------------------------
# RE-CREATE MODE
# Destroys each VM in the range and re-clones it from the template.
# vm_name is pulled from the ini config file per VMID.
# ---------------------------------------------------------------------------
if [[ "$cmd" == "re-create" ]]; then
    for (( i=start_vmid; i<=end_vmid; i++ )); do
        vm_name=$(get_value "c${i}" 'vm_name')
        vm_name="${vm_name:-sim-client}"

        qm stop "$i"
        qm destroy "$i" --skiplock --purge --destroy-unreferenced-disks

        qm clone "$tpl_id" "$i" --name "${vm_name}" --pool "$pool_name"
        qm set "$i" --onboot 1 --startup "order=2,up=60"
        qm start "$i"
        sleep "$sleep_time"
    done
fi

# ---------------------------------------------------------------------------
# DELETE MODE
# Stops and permanently destroys all VMs in the range.
# ---------------------------------------------------------------------------
if [[ "$cmd" == "delete" ]]; then
    for (( i=start_vmid; i<=end_vmid; i++ )); do
        qm stop "$i"
        qm destroy "$i" --skiplock --purge --destroy-unreferenced-disks
    done
fi

# ---------------------------------------------------------------------------
# CONFIG MODE
# Pushes the correct hostname into each running VM and reboots it.
# vm_name is pulled from the ini config file per VMID.
# ---------------------------------------------------------------------------
if [[ "$cmd" == "config" ]]; then
    for (( i=start_vmid; i<=end_vmid; i++ )); do
        vm_name=$(get_value "c${i}" 'vm_name')
        vm_name="${vm_name:-sim-client}"
        qm guest exec "$i" --timeout 60 -- hostnamectl set-hostname "${vm_name}"
        qm guest exec "$i" -- reboot

        # Wait for VM to come back up after reboot before running update.sh
        reboot_wait=0
        until qm guest ping "$i" >/dev/null 2>&1; do
            sleep 5
            reboot_wait=$((reboot_wait + 5))
            (( reboot_wait >= 600 )) && { echo "WARNING: VM $i did not come back after reboot"; break; }
        done

        qm guest exec "$i" --timeout 300 -- bash /usr/local/scripts/update.sh \
            && echo "update.sh completed on VM $i" \
            || echo "WARNING: update.sh failed on VM $i"

        # Trigger hub self-update so the hub pulls latest scripts too
        _hub_url=$(tr -d '[:space:]' < /var/lib/client-sim/hub-server-url 2>/dev/null || true)
        if [[ -n "$_hub_url" ]]; then
            _hub_http=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 \
                -X POST "${_hub_url}/api/self-update" 2>/dev/null || true)
            [[ "$_hub_http" == "200" ]] \
                && echo "Hub self-update triggered at ${_hub_url}" \
                || echo "WARNING: Hub self-update returned HTTP ${_hub_http:-000} (non-fatal)"
        fi
    done
fi

# ---------------------------------------------------------------------------
# PCI PASSTHROUGH
# Runs after any of the above modes if local_passthrough == "pci".
# Assigns matched PCIe devices to VMs sequentially from start_vmid.
# Guest is shut down so the PCI device can be cleanly attached.
# ---------------------------------------------------------------------------
if [[ "$local_passthrough" == "pci" ]]; then
    echo "Setting up PCI devices"
    sleep "$sleep_time"

    i=0
    for pci_addr in "${pci_devices[@]}"; do
        vmid=$((start_vmid + i))

        echo "Assigning PCI $pci_addr -> VM $vmid"

        qm set "$vmid" -hostpci0 "$pci_addr"
        qm guest exec "$vmid" -- shutdown now

        sleep "$sleep_time"
        qm start "$vmid"

        (( i++ ))
    done
fi
