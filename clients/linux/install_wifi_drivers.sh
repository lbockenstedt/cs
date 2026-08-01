#!/bin/bash
#------------------------------------------------------------
# install_wifi_drivers.sh — every known WiFi driver for Debian 13 (trixie)
#
# Built for the sim-client image: these VMs get an arbitrary USB WiFi dongle
# passed through, so the image must already carry a driver for ANY dongle in the
# fleet rather than being fixed up per-VM after the fact.
#
# Run ONCE while building the golden image (or on a live client when a new
# dongle type shows up):
#
#     sudo bash /usr/local/scripts/install_wifi_drivers.sh            # firmware + packaged drivers
#     sudo bash /usr/local/scripts/install_wifi_drivers.sh --out-of-tree   # + DKMS builds from source
#
# DESIGN: every install is BEST EFFORT and independently reported. A package
# that does not exist on this release, or a DKMS build that fails against this
# kernel, must not abort the run — a partial driver set is useful, a script that
# dies on the first missing package is not. The summary at the end is the real
# output: it says which chips are covered and which are not.
#
# Deliberately NOT idempotency-hostile: re-running is safe and is the intended
# way to pick up new drivers after a kernel upgrade.
#------------------------------------------------------------
set -uo pipefail        # NOT -e: see the best-effort note above

OUT_OF_TREE=0
[[ "${1:-}" == "--out-of-tree" ]] && OUT_OF_TREE=1

LOG=/var/log/install_wifi_drivers.log
exec > >(tee -a "$LOG") 2>&1
echo "=== install_wifi_drivers.sh $(date -Is) (out-of-tree=$OUT_OF_TREE) ==="

[[ $EUID -eq 0 ]] || { echo "FATAL: must run as root (sudo)"; exit 1; }

OK_LIST=(); FAIL_LIST=()
_try() {  # _try <label> <cmd...>
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then OK_LIST+=("$label"); echo "  ok   $label"
    else FAIL_LIST+=("$label"); echo "  MISS $label"; fi
}

#------------------------------------------------------------
# 1. Non-free firmware components.
#
# Debian 12 split firmware out of `non-free` into its own `non-free-firmware`
# component, and trixie ships the deb822 format (/etc/apt/sources.list.d/
# debian.sources) rather than the one-line sources.list. Handle BOTH: an image
# built from a netinst has the .sources file, an upgraded box may still have the
# old list. Without this every firmware-* package below is simply "not found".
#------------------------------------------------------------
echo "-- enabling contrib / non-free / non-free-firmware"
_src=/etc/apt/sources.list.d/debian.sources
if [[ -f "$_src" ]]; then
    if ! grep -q 'non-free-firmware' "$_src"; then
        cp -n "$_src" "$_src.bak-wifi"
        # Append the missing components to every Components: line.
        sed -i 's/^\(Components:.*\)$/\1 contrib non-free non-free-firmware/' "$_src"
        # Collapse any duplicates a re-run would introduce.
        sed -i 's/\(Components:[^\n]*\)/\1/' "$_src"
        echo "  updated $_src"
    else
        echo "  $_src already has non-free-firmware"
    fi
fi
if [[ -f /etc/apt/sources.list ]] && grep -qE '^deb ' /etc/apt/sources.list; then
    if ! grep -qE '^deb .*non-free-firmware' /etc/apt/sources.list; then
        cp -n /etc/apt/sources.list /etc/apt/sources.list.bak-wifi
        sed -i -E 's/^(deb .*debian\.org[^ ]* [a-z-]+ main.*)$/\1 contrib non-free non-free-firmware/' \
            /etc/apt/sources.list
        echo "  updated /etc/apt/sources.list"
    fi
fi
apt-get update -qq || echo "  WARNING: apt-get update failed — package list may be stale"

#------------------------------------------------------------
# 2. Firmware blobs. Covers the in-kernel drivers (mt76, ath9k/10k, iwlwifi,
#    brcmfmac, rtw88/rtw89, r8152 USB-ethernet) which load but do NOTHING
#    without their firmware — the classic "device appears, never associates".
#------------------------------------------------------------
echo "-- firmware packages"
for p in \
    firmware-linux-free firmware-linux-nonfree firmware-misc-nonfree \
    firmware-realtek firmware-atheros firmware-iwlwifi firmware-brcm80211 \
    firmware-libertas firmware-ti-connectivity firmware-zd1211 \
    firmware-mediatek firmware-ath9k-htc firmware-intel-sof \
    firmware-realtek-rtl8723cs-bt
do
    _try "$p" env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$p"
done

#------------------------------------------------------------
# 3. Build prerequisites for every DKMS driver below. Headers MUST match the
#    running kernel or every DKMS build fails with a confusing "kernel source
#    not found" — install both the exact and the meta package.
#------------------------------------------------------------
echo "-- build prerequisites"
# wireless-regdb + iw are what make 6 GHz usable at all: without a regulatory
# database the kernel falls back to world-roaming, and every 6E/Wi-Fi 7 channel
# is silently unavailable. The adapter associates fine on 2.4/5 GHz, so this
# looks like a hardware limitation rather than a missing package.
for p in dkms build-essential bc "linux-headers-$(uname -r)" linux-headers-amd64 \
         git iw wireless-tools wireless-regdb rfkill usbutils libelf-dev
do
    _try "$p" env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$p"
done

#------------------------------------------------------------
# 4. usb_modeswitch — REQUIRED, not optional, for this fleet.
#    Several Realtek dongles enumerate first as a CD-ROM holding a Windows
#    driver (0bda:1a2b is exactly that mode) and only become a WiFi NIC after
#    being switched. Without this the dongle looks present in lsusb and never
#    appears as a netdev — which reads like a driver problem and is not one.
#------------------------------------------------------------
echo "-- usb_modeswitch (CD-ROM-mode dongles, e.g. 0bda:1a2b)"
for p in usb-modeswitch usb-modeswitch-data; do
    _try "$p" env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$p"
done

#------------------------------------------------------------
# 5. Packaged DKMS drivers. These are the out-of-tree chips Debian happens to
#    package; anything not here needs section 6.
#------------------------------------------------------------
echo "-- packaged DKMS drivers"
for p in realtek-rtl88xxau-dkms rtl8821ce-dkms rtl8812au-dkms \
         rtl8189es-dkms rtl8723bu-dkms broadcom-sta-dkms
do
    _try "$p" env DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$p"
done

#------------------------------------------------------------
# 6. Out-of-tree DKMS builds (--out-of-tree). Needs network + several minutes.
#
#    These are the chips with NO usable mainline driver, mapped from the fleet's
#    configured dongle VID:PIDs. Each is cloned to /usr/src/<name> and registered
#    with dkms so it REBUILDS AUTOMATICALLY on a kernel upgrade — a plain `make
#    install` would silently stop working at the next kernel bump, which on a
#    fleet this size is a slow-motion outage.
#------------------------------------------------------------
if [[ $OUT_OF_TREE -eq 1 ]]; then
    echo "-- out-of-tree DKMS builds"
    _dkms_git() {  # _dkms_git <name> <version> <git-url>
        local name="$1" ver="$2" url="$3" dir="/usr/src/${1}-${2}"
        if dkms status 2>/dev/null | grep -q "^${name}"; then
            OK_LIST+=("$name (already installed)"); echo "  ok   $name (already installed)"; return
        fi
        rm -rf "$dir"
        if ! git clone --depth 1 "$url" "$dir" >/dev/null 2>&1; then
            FAIL_LIST+=("$name (clone failed)"); echo "  MISS $name (clone failed: $url)"; return
        fi
        # Most of these ship a dkms.conf; if not, the build is not DKMS-ready and
        # we skip rather than hand-rolling one that will rot.
        if [[ ! -f "$dir/dkms.conf" ]]; then
            FAIL_LIST+=("$name (no dkms.conf)"); echo "  MISS $name (no dkms.conf)"; rm -rf "$dir"; return
        fi
        if dkms add -m "$name" -v "$ver" >/dev/null 2>&1 \
           && dkms build -m "$name" -v "$ver" >/dev/null 2>&1 \
           && dkms install -m "$name" -v "$ver" >/dev/null 2>&1; then
            OK_LIST+=("$name (dkms)"); echo "  ok   $name (dkms built + installed)"
        else
            FAIL_LIST+=("$name (dkms build failed)")
            echo "  MISS $name — build failed; see /var/lib/dkms/$name/$ver/build/make.log"
        fi
    }
    # Chips with NO mainline driver on 6.12 — always build these.
    # RTL8812AU/8821AU  0bda:8812 2001:331e 2357:011e 0b05:1a62
    _dkms_git 8812au   20210629 https://github.com/morrownr/8812au-20210629.git
    # RTL8188EU(S)      0bda:8179 0846:9020   (staging r8188eu removed after 6.6)
    _dkms_git 8188eu   20210902 https://github.com/morrownr/8188eu-20210902.git
    # RTL8814AU — not in the current fleet, harmless if unused
    _dkms_git 8814au   20210629 https://github.com/morrownr/8814au-20210629.git
    # RTL8852AU/8832AU — Wi-Fi 6 over USB. No mainline USB driver exists for
    # these, so this is the only way to get Wi-Fi 6 on a Realtek dongle.
    # (MT7921AU is the other Wi-Fi 6 USB option and needs NOTHING — mt7921u has
    # been mainline since 5.16.)
    _dkms_git rtl8852au 1.15.0.1 https://github.com/morrownr/rtl8852au.git
    # RTL8192EU  0bda:818b — rtl8xxxu claims it but is unstable on many units
    _dkms_git rtl8192eu 1.0      https://github.com/Mange/rtl8192eu-linux-driver.git

    #--------------------------------------------------------
    # RTL8822BU / RTL8821CU are MAINLINE on this kernel (rtw88_8822bu since 6.1,
    # rtw88_8821cu since 6.2). Building the out-of-tree driver too means BOTH
    # claim the same USB ID and which one binds is a race — behaviour then
    # differs across reboots on identical VMs, which is exactly the kind of
    # fault this fleet cannot debug remotely.
    #
    # So: only build them if the mainline module is genuinely absent. Opt in with
    # LM_FORCE_RTW_OOT=1 if the mainline driver misbehaves on your dongles, and
    # blacklist the mainline module if you do.
    #--------------------------------------------------------
    _mainline_has() { modinfo "$1" >/dev/null 2>&1; }
    if [[ "${LM_FORCE_RTW_OOT:-0}" == "1" ]]; then
        echo "  LM_FORCE_RTW_OOT=1 — building rtw88-overlapping drivers anyway"
        echo "  REMEMBER to blacklist the mainline module, e.g.:"
        echo "    echo 'blacklist rtw88_8822bu' > /etc/modprobe.d/blacklist-rtw88-usb.conf"
        _dkms_git 88x2bu 20210702 https://github.com/morrownr/88x2bu-20210702.git
        _dkms_git 8821cu 20210916 https://github.com/morrownr/8821cu-20210916.git
    else
        for _m in rtw88_8822bu rtw88_8821cu; do
            if _mainline_has "$_m"; then
                OK_LIST+=("$_m (mainline — no DKMS needed)"); echo "  ok   $_m (mainline, skipping out-of-tree)"
            else
                echo "  note $_m not found in this kernel — building out-of-tree instead"
                case "$_m" in
                    rtw88_8822bu) _dkms_git 88x2bu 20210702 https://github.com/morrownr/88x2bu-20210702.git ;;
                    rtw88_8821cu) _dkms_git 8821cu 20210916 https://github.com/morrownr/8821cu-20210916.git ;;
                esac
            fi
        done
    fi
else
    echo "-- out-of-tree DKMS builds SKIPPED (pass --out-of-tree to build them)"
    echo "   Without these, these fleet dongles have no driver:"
    echo "     0bda:8812 0bda:818b 0bda:8179 2001:331e 2357:011e 0b05:1a62 0846:9020"
    echo "   (0bda:b812/b820/c811/c820 are covered by mainline rtw88 on kernel 6.12)"
    fi

#------------------------------------------------------------
# 7. Make sure the radio is not soft-blocked, and refresh module deps so a
#    freshly-installed driver is loadable without a reboot.
#------------------------------------------------------------
depmod -a >/dev/null 2>&1 || true
rfkill unblock all >/dev/null 2>&1 || true

#------------------------------------------------------------
# 8. Report. This is the point of the script — a bare exit code cannot tell you
#    whether the image can actually drive the dongles you own.
#------------------------------------------------------------
echo ""
echo "=== SUMMARY ==="
echo "installed/present : ${#OK_LIST[@]}"
# Guarded: on bash < 4.4 an empty array under `set -u` expands to an UNBOUND
# VARIABLE error, so the summary would die exactly when nothing installed —
# the one run where you most need to read it.
((${#OK_LIST[@]})) && printf '    %s\n' "${OK_LIST[@]}"
if ((${#FAIL_LIST[@]})); then
    echo "unavailable/failed: ${#FAIL_LIST[@]}"
    printf '    %s\n' "${FAIL_LIST[@]}"
    echo ""
    echo "NOTE: 'unavailable' is often correct — several of the packages probed"
    echo "above do not exist on every Debian release, and a chip covered by a"
    echo "mainline driver needs no DKMS package at all. Judge by section 9, not"
    echo "by this count."
fi

#------------------------------------------------------------
# 9. Coverage against the dongles ACTUALLY plugged in right now.
#------------------------------------------------------------
echo ""
echo "=== DONGLES PRESENT ON THIS HOST ==="
if command -v lsusb >/dev/null 2>&1; then
    lsusb | grep -iE "wireless|wlan|802\.11|realtek|ralink|mediatek|atheros|broadcom" \
        || echo "  (no obvious wifi dongle in lsusb — plug one in and re-check)"
else
    echo "  lsusb unavailable (usbutils not installed)"
fi
echo ""
echo "=== WIRELESS INTERFACES ==="
if command -v iw >/dev/null 2>&1; then
    iw dev 2>/dev/null | grep -E "Interface|type" || echo "  (none — driver missing, or dongle still in CD-ROM mode)"
else
    ip -br link 2>/dev/null | grep -iE "wl" || echo "  (no wl* interface)"
fi
echo ""
echo "Driver matrix + system requirements: cs/clients/linux/WIFI-DRIVERS.md (repo)"
echo "Full log: $LOG"
echo "If a dongle shows in lsusb but has no wl* interface, try:"
echo "  usb_modeswitch -v <vid> -p <pid> -J     # CD-ROM-mode Realtek"
echo "  dmesg | tail -40                        # driver bind errors"
echo "=== done $(date -Is) ==="
