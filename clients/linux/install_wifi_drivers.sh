#!/usr/bin/env bash
###############################################################################
# install_wifi_drivers.sh — USB WiFi driver package for the sim-client image
#
# Pulls every out-of-tree USB WiFi driver LIVE from GitHub and builds it, plus
# the firmware / mode-switch / regulatory layer the DKMS drivers do not cover.
#
# Extracted from installers/install.sh so the driver set is maintained in ONE
# place and can be re-run on a live client when a new dongle appears, without
# re-running the whole client installer. install.sh calls this at the end.
#
#   sudo bash install_wifi_drivers.sh              # firmware + all USB drivers
#   sudo bash install_wifi_drivers.sh --firmware-only
#   sudo bash install_wifi_drivers.sh --drivers-only
#   sudo bash install_wifi_drivers.sh --repair          # un-wedge broken DKMS/apt
#
# SCOPE: USB only. These clients are VMs fed by USB passthrough and never see a
# PCIe/M.2 adapter, so no PCIe-only driver is built here.
#
# TARGETS Debian 12 (bookworm, 6.1) AND 13 (trixie, 6.12). Nothing here is
# version-pinned: the mainline-vs-out-of-tree decision is made by probing
# `modinfo` on the RUNNING kernel, so on bookworm this builds 8821cu (rtw88's
# USB support for it did not land until 6.2) and on trixie it skips it. Both
# apt sources formats are handled — bookworm's one-line sources.list and
# trixie's deb822 .sources.
#
# Callers may pre-set LOG and DRIVER_STATE to fold this into their own logging
# (install.sh does); otherwise sane defaults are used.
###############################################################################
set -uo pipefail        # NOT -e: every step is best-effort and reported

export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

DO_FIRMWARE=1; DO_DRIVERS=1; DO_REPAIR=0
for a in "$@"; do
  case "$a" in
    --firmware-only) DO_DRIVERS=0 ;;
    --drivers-only)  DO_FIRMWARE=0 ;;
    --repair)        DO_REPAIR=1 ;;
  esac
done

LOG="${LOG:-/var/log/install_wifi_drivers.log}"
DRIVER_STATE="${DRIVER_STATE:-/var/lib/lm-client/wlan-drivers.state}"
mkdir -p "$(dirname "$DRIVER_STATE")" 2>/dev/null || DRIVER_STATE=/tmp/wlan-drivers.state
: >"$DRIVER_STATE" 2>/dev/null || DRIVER_STATE=/tmp/wlan-drivers.state
touch "$LOG" 2>/dev/null || LOG=/tmp/install_wifi_drivers.log

# Use the caller's helpers when invoked from install.sh; otherwise define ours.
command -v info >/dev/null 2>&1 || info() { echo "  $*" | tee -a "$LOG"; }
command -v ok   >/dev/null 2>&1 || ok()   { echo "  ok   $*" | tee -a "$LOG"; }
command -v warn >/dev/null 2>&1 || warn() { echo "  WARN $*" | tee -a "$LOG"; }

[[ $EUID -eq 0 ]] || { echo "FATAL: must run as root"; exit 1; }
echo "=== install_wifi_drivers.sh $(date -Is) ===" >>"$LOG"

###############################################################################
# 0. REPAIR (--repair)
#
# Un-wedge a box where a broken DKMS module is failing the linux-headers
# postinst. Symptom, and it names the wrong package:
#   "errors were encountered while processing: linux-headers-6.1.0-51-amd64"
# apt is then stuck for EVERY subsequent operation until the module is removed.
###############################################################################
if [[ $DO_REPAIR -eq 1 ]]; then
  info "Repair: looking for DKMS modules that fail to build"
  _removed=0
  while read -r _line; do
    _m="$(echo "$_line" | awk -F'[,/:]' '{print $1}' | tr -d ' ')"
    _v="$(echo "$_line" | awk -F'[,/:]' '{print $2}' | tr -d ' ')"
    [[ -z "$_m" || -z "$_v" ]] && continue
    if echo "$_line" | grep -qiE "broken|failed|error"; then
      warn "removing broken DKMS module $_m/$_v"
      dkms remove -m "$_m" -v "$_v" --all >>"$LOG" 2>&1 || true
      rm -rf "/usr/src/${_m}-${_v}"
      _removed=$(( _removed + 1 ))
    fi
  done < <(dkms status 2>/dev/null)
  info "Repair: removed $_removed broken module(s); running apt-get -f install"
  DEBIAN_FRONTEND=noninteractive apt-get -f install -y >>"$LOG" 2>&1 \
    && ok "apt dependency state repaired" \
    || warn "apt-get -f install still failing — see $LOG"
  info "Re-run without --repair to reinstall drivers."
  exit 0
fi

###############################################################################
# 1. FIRMWARE + MODE-SWITCH + REGULATORY
#
# This layer is what the DKMS drivers do NOT give you, and its absence is the
# hardest class of failure to diagnose because nothing errors:
#   * MediaTek dongles in this fleet (0846:9041 MT7612U, 0e8d:c616, 2357:0105
#     MT7610U) use MAINLINE mt76 drivers — the module loads and the device is
#     dead without firmware-* installed.
#   * 0bda:1a2b is not a NIC at all until usb_modeswitch flips it out of
#     CD-ROM mode. It shows in lsusb with no wl* interface and no dmesg error.
#   * Without wireless-regdb the kernel world-roams and every 6 GHz channel is
#     silently unavailable while 2.4/5 GHz work fine.
###############################################################################
if [[ $DO_FIRMWARE -eq 1 ]]; then
  info "Enabling contrib / non-free / non-free-firmware"
  # Debian 12+ moved firmware into its own non-free-firmware component, and
  # trixie ships deb822 (.sources). Handle both or every firmware-* below is
  # simply "not found".
  _src=/etc/apt/sources.list.d/debian.sources
  if [[ -f "$_src" ]] && ! grep -q 'non-free-firmware' "$_src"; then
    cp -n "$_src" "$_src.bak-wifi" 2>/dev/null
    sed -i 's/^\(Components:.*\)$/\1 contrib non-free non-free-firmware/' "$_src"
    info "updated $_src"
  fi
  if [[ -f /etc/apt/sources.list ]] && grep -qE '^deb ' /etc/apt/sources.list \
     && ! grep -qE '^deb .*non-free-firmware' /etc/apt/sources.list; then
    cp -n /etc/apt/sources.list /etc/apt/sources.list.bak-wifi 2>/dev/null
    sed -i -E 's/^(deb .*debian\.org[^ ]* [a-z-]+ main.*)$/\1 contrib non-free non-free-firmware/' \
      /etc/apt/sources.list
    info "updated /etc/apt/sources.list"
  fi
  apt-get update -qq >>"$LOG" 2>&1 || warn "apt-get update failed — package list may be stale"

  info "Installing firmware + support packages"
  for p in \
      firmware-linux-free firmware-linux-nonfree firmware-misc-nonfree \
      firmware-realtek firmware-atheros firmware-iwlwifi firmware-brcm80211 \
      firmware-mediatek firmware-ath9k-htc firmware-libertas firmware-zd1211 \
      firmware-ti-connectivity \
      usb-modeswitch usb-modeswitch-data wireless-regdb \
      iw wireless-tools rfkill usbutils
  do
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$p" >>"$LOG" 2>&1; then
      echo "$p:INSTALLED" >>"$DRIVER_STATE"; ok "$p"
    else
      echo "$p:UNAVAILABLE" >>"$DRIVER_STATE"; info "skip $p (not on this release)"
    fi
  done

  # ── AIC8800 fake-CD-ROM mode switch ────────────────────────────────────────
  # AIC8800 dongles boot as USB MASS STORAGE and only become a NIC after being
  # flipped. usb-modeswitch-data does not carry the a69c IDs, and the upstream
  # aic8800 installer only ships rules for the 1111:1111 clones — so on these
  # units the flip never happens and the adapter shows up in lsusb forever
  # while never producing a wlan interface. That is not an obvious driver
  # symptom, which is why it is handled here rather than left to the driver.
  info "Installing usb_modeswitch rule for AIC8800 (a69c:5721/5723)"
  mkdir -p /etc/udev/rules.d
  cat >/etc/udev/rules.d/40-aic8800-modeswitch.rules <<'AICRULES'
# Managed by install_wifi_drivers.sh — do not edit manually
# Flip AIC8800 dongles out of their fake CD-ROM (mass storage) identity so the
# Wi-Fi function appears. Post-flip they re-enumerate as a69c:8d80.
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="a69c", ATTR{idProduct}=="5721", RUN+="/usr/sbin/usb_modeswitch -v a69c -p 5721 -KQ"
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="a69c", ATTR{idProduct}=="5723", RUN+="/usr/sbin/usb_modeswitch -v a69c -p 5723 -KQ"
AICRULES
  chmod 644 /etc/udev/rules.d/40-aic8800-modeswitch.rules
  udevadm control --reload-rules >>"$LOG" 2>&1 || true
  # Flip anything already plugged in — the rule alone only fires on a NEW add
  # event, so without this the dongles sitting in the box stay mass-storage
  # until physically re-seated.
  for _pid in 5721 5723; do
    if lsusb -d "a69c:$_pid" >/dev/null 2>&1; then
      usb_modeswitch -v a69c -p "$_pid" -KQ >>"$LOG" 2>&1 \
        && ok "AIC8800 a69c:$_pid flipped to Wi-Fi mode" \
        || info "AIC8800 a69c:$_pid flip returned non-zero (may already be flipped)"
    fi
  done
  echo "aic8800-modeswitch:INSTALLED" >>"$DRIVER_STATE"
fi

[[ $DO_DRIVERS -eq 0 ]] && { info "--firmware-only: skipping driver builds"; exit 0; }

###############################################################################
# 2. BUILD PREREQUISITES
#
# Headers MUST match the RUNNING kernel or every build fails with a misleading
# "kernel source not found". linux-headers-amd64 is separate and load-bearing:
# without it DKMS silently skips the rebuild on a kernel upgrade and the driver
# disappears at the next boot — a slow-motion fleet outage.
###############################################################################
if grep -qiE "raspberry|BCM2" /proc/cpuinfo 2>/dev/null; then
  KERNEL_HEADERS="raspberrypi-kernel-headers"
else
  KERNEL_HEADERS="linux-headers-$(uname -r)"
fi
info "Installing build prerequisites ($KERNEL_HEADERS)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  dkms build-essential bc libelf-dev git linux-headers-amd64 \
  >>"$LOG" 2>&1 || warn "some build prerequisites failed"
DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$KERNEL_HEADERS" \
  >>"$LOG" 2>&1 || warn "$KERNEL_HEADERS failed to install"

# Headers for the RUNNING kernel are a hard gate: without them EVERY DKMS build
# below fails identically with "kernel source not found", producing a wall of
# failures whose real cause is one missing package. Say so once and stop, rather
# than emitting 13 misleading build errors.
if [[ ! -d "/lib/modules/$(uname -r)/build" ]]; then
  warn "No kernel headers for $(uname -r) (/lib/modules/$(uname -r)/build missing)."
  warn "Every DKMS driver build would fail. Install headers, then re-run:"
  warn "  apt-get install -y $KERNEL_HEADERS   # or reboot into the kernel the headers match"
  echo "kernel-headers:MISSING" >>"$DRIVER_STATE"
  exit 1
fi

###############################################################################
# 3. OUT-OF-TREE USB DRIVERS — cloned LIVE from GitHub and built
#
# Format: "dir-name|type|repo-url|dkms-module|pinned-tag|modprobe-module"
# Types:
#   morrownr  — repo ships install-driver.sh; run it NoPrompt
#   lwfinger  — bare Makefile; build, copy to /usr/src, register with DKMS
#   dkms-only — dkms.conf + Makefile, no install-driver.sh
# modprobe-module: "-" when no explicit modprobe is needed.
###############################################################################
WIFI_SRC="/usr/src/wifi-drivers"
mkdir -p "$WIFI_SRC"; cd "$WIFI_SRC" || exit 1

# systemctl shim: several install-driver.sh scripts call systemctl, which is
# meaningless (and noisy) mid-install. Suppressed for the duration only.
SUPPRESS="$(mktemp -d)"
printf '#!/bin/sh\necho "[SUPPRESSED] systemctl $* ignored during driver install"\nexit 0\n' \
  > "$SUPPRESS/systemctl"
chmod +x "$SUPPRESS/systemctl"
OLD_PATH="$PATH"; export PATH="$SUPPRESS:$PATH"

DRIVERS=(
  # ── chips in the fleet's usb_config today ─────────────────────────────────
  # RTL8812AU  0bda:8812 2001:331e 2357:012e
  "8812au-20210820|morrownr|https://github.com/morrownr/8812au-20210820.git|8812au|HEAD|-"
  # RTL8811AU/8821AU  2357:011e (TP-Link Archer T2U Nano)
  # Mainline rtw88 gained these only in 6.13, so on trixie's 6.12 the
  # out-of-tree build is the ONLY thing that binds them.
  "8821au-20210708|morrownr|https://github.com/morrownr/8821au-20210708.git|8821au|HEAD|-"
  # RTL8811CU/8821CU  0bda:c811 0bda:c820   (mainline rtw88 on 6.12 — see skip below)
  "8821cu-20210916|morrownr|https://github.com/morrownr/8821cu-20210916.git|8821cu|HEAD|-"
  # RTL8812BU/8822BU  0bda:b812 0bda:b820 2357:012d   (mainline rtw88 on 6.12)
  "88x2bu-20210702|morrownr|https://github.com/morrownr/88x2bu-20210702.git|88x2bu|HEAD|-"
  # RTL8188EUS  0bda:8179 0846:9020
  "rtl8188eu|lwfinger|https://github.com/lwfinger/rtl8188eu.git|8188eu|HEAD|-"
  # RTL8192EU  0bda:818b — IS in the fleet but had NO out-of-tree driver in the
  # installer. Mainline rtl8xxxu claims it and is unstable on many units.
  "rtl8192eu|dkms-only|https://github.com/Mange/rtl8192eu-linux-driver.git|rtl8192eu|HEAD|-"

  # ── not in the fleet yet; cheap parts you plausibly buy next ──────────────
  "8814au|morrownr|https://github.com/morrownr/8814au.git|8814au|HEAD|-"
  "rtl8723au|lwfinger|https://github.com/lwfinger/rtl8723au.git|8723au|HEAD|8723au"
  "rtl8723du|lwfinger|https://github.com/lwfinger/rtl8723du.git|8723du|HEAD|-"
  # RTL8710BU/RTL8188GU (0bda:b711) intentionally has NO entry: morrownr/rtl8710bu
  # no longer exists upstream (it was failing CLONE_FAILED on every run) and
  # mainline rtl8xxxu has claimed this chip since 6.4, so trixie's 6.12 handles
  # it in-tree. Adding an out-of-tree build back would just race the in-tree one.

  # ── Wi-Fi 6 / 6E / Wi-Fi 7 over USB ──────────────────────────────────────
  # ONE repo now covers the whole Realtek rtw89 USB family, replacing the three
  # per-chip entries that used to live here. Two of those (rtl8852bu-20240418,
  # rtl8852cu-20240510) NO LONGER EXIST upstream — morrownr consolidated them —
  # so they had been failing CLONE_FAILED on every run, which is precisely why
  # 0b05:1a62 and 0bda:c832 came up with no wlan interface. lwfinger/rtl8852au
  # is dropped too: it claims the same USB IDs as this driver and two drivers
  # racing for one device is the non-deterministic fault this fleet cannot debug
  # remotely.
  #
  # Covers RTL8831BU 8851BU 8832AU 8852AU 8832BU 8852BU 8832CU 8852CU
  #        8912AU 8922AU. In the fleet today:
  #   0b05:1a62 8852BU (ASUS USB-AX55 Nano)   0bda:c832 8852CU
  #   35bc:0108 8832BU (Archer TX20U Nano)    2357:0141 8832AU (Archer TX20UH)
  #   0bda:8912 8912AU/8922AU (Wi-Fi 7)
  # Its dkms.conf declares BUILD_EXCLUSIVE_KERNEL ^(6.[6-9]|6.1[0-9]|7.) so
  # trixie's 6.12 qualifies, and its modules carry a _git suffix so they cannot
  # collide with the in-tree rtw89 names when mainline catches up.
  "rtw89|dkms-only|https://github.com/morrownr/rtw89.git|rtw89|HEAD|-"

  # ── AIC8800D80  a69c:8d80 ────────────────────────────────────────────────
  # No mainline support at ANY kernel version. Uses the repo's own installer
  # because DKMS alone is not enough here: without its firmware blobs the module
  # loads and the radio is deaf, and the dongle does not present as a NIC at all
  # until usb_modeswitch flips it out of the fake CD-ROM identity it boots into
  # (that is what a69c:5721 / a69c:5723 are — the SAME dongles pre-flip, not
  # separate models). The installer ships the firmware, udev rules and
  # usb_modeswitch config together.
  "aic8800|script-yes|https://github.com/kilam994/aic8800d80-linux-driver.git|aic8800|HEAD|-"
)

TOTAL="${#DRIVERS[@]}"; N=0
for entry in "${DRIVERS[@]}"; do
  SAVED_IFS="$IFS"; IFS='|' read -r NAME TYPE REPO MOD PIN MODPROBE <<<"$entry"; IFS="$SAVED_IFS"
  N=$(( N + 1 ))
  info "Driver $N/$TOTAL: $NAME"

  # Skip a chip the running kernel already handles. Building an out-of-tree
  # driver alongside a mainline one means BOTH claim the USB ID and which binds
  # is a race — identical VMs then behave differently across reboots, which is
  # exactly the fault this fleet cannot debug remotely.
  case "$MOD" in
    88x2bu) modinfo rtw88_8822bu >/dev/null 2>&1 && {
              info "skip $NAME — rtw88_8822bu is mainline on $(uname -r)"
              echo "$NAME:SKIPPED_IN_TREE" >>"$DRIVER_STATE"; continue; } ;;
    8821cu) modinfo rtw88_8821cu >/dev/null 2>&1 && {
              info "skip $NAME — rtw88_8821cu is mainline on $(uname -r)"
              echo "$NAME:SKIPPED_IN_TREE" >>"$DRIVER_STATE"; continue; } ;;
  esac

  rm -rf "$NAME"
  CLONE_ARGS=(--depth=1)
  [[ "$PIN" != "HEAD" ]] && CLONE_ARGS+=(--branch "$PIN")
  if ! git clone "${CLONE_ARGS[@]}" "$REPO" "$NAME" >>"$LOG" 2>&1; then
    echo "$NAME:CLONE_FAILED" >>"$DRIVER_STATE"; warn "✗ clone failed: $NAME"; continue
  fi
  cd "$NAME" || continue
  INSTALL_OK=true

  case "$TYPE" in
    morrownr)
      if [[ -x ./install-driver.sh ]]; then
        ./install-driver.sh NoPrompt >>"$LOG" 2>&1 || INSTALL_OK=false
      else
        warn "$NAME: install-driver.sh missing"; INSTALL_OK=false
      fi
      ;;
    script-yes)
      # Repo ships its own installer that does more than a module build —
      # firmware, udev rules and usb_modeswitch config — so running DKMS by hand
      # would produce a driver that loads and still cannot see a network.
      # --yes keeps it non-interactive (it also auto-assumes yes when stdin is
      # not a TTY, but be explicit rather than relying on how we are invoked).
      if [[ -x ./install.sh ]]; then
        ./install.sh --yes >>"$LOG" 2>&1 || INSTALL_OK=false
      elif [[ -f ./install.sh ]]; then
        bash ./install.sh --yes >>"$LOG" 2>&1 || INSTALL_OK=false
      else
        warn "$NAME: install.sh missing"; INSTALL_OK=false
      fi
      ;;
    lwfinger|dkms-only)
      # lwfinger repos build first; dkms-only go straight to DKMS.
      if [[ "$TYPE" == "lwfinger" ]]; then
        make all >>"$LOG" 2>&1 || INSTALL_OK=false
      fi
      if $INSTALL_OK; then
        DKMS_VER="0.0"
        [[ -f dkms.conf ]] && DKMS_VER="$(grep 'PACKAGE_VERSION=' dkms.conf | cut -d'"' -f2 || echo 0.0)"
        SRC_DEST="/usr/src/${MOD}-${DKMS_VER}"
        rm -rf "$SRC_DEST"; cp -r "$(pwd)" "$SRC_DEST"
        dkms add -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 || true
        if dkms build -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 \
           && dkms install -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1; then
          :
        else
          INSTALL_OK=false
          # UNREGISTER a module that failed to build. Leaving it registered
          # poisons EVERY future kernel/header operation: the linux-headers
          # postinst rebuilds all registered DKMS modules, one failure aborts
          # the postinst, and dpkg then reports
          #   "errors were encountered while processing: linux-headers-<ver>"
          # — which points at the kernel package and gives no hint that a wifi
          # driver is the cause. Observed in the field on 6.1.0-51-amd64.
          warn "$NAME: build failed — unregistering from DKMS so it cannot break kernel upgrades"
          dkms remove -m "$MOD" -v "$DKMS_VER" --all >>"$LOG" 2>&1 || true
          rm -rf "$SRC_DEST"
        fi
      fi
      ;;
  esac

  if $INSTALL_OK && [[ "$MODPROBE" != "-" && -n "$MODPROBE" ]]; then
    modprobe "$MODPROBE" >>"$LOG" 2>&1 || warn "modprobe $MODPROBE failed (may need reboot)"
  fi

  cd "$WIFI_SRC"
  if $INSTALL_OK; then
    echo "$NAME:INSTALLED" >>"$DRIVER_STATE"; ok "✓ $NAME [$N/$TOTAL]"
  else
    echo "$NAME:FAILED" >>"$DRIVER_STATE"; warn "✗ $NAME build failed [$N/$TOTAL]"
  fi
done

export PATH="$OLD_PATH"; rm -rf "$SUPPRESS"
depmod -a >>"$LOG" 2>&1 || true
rfkill unblock all >/dev/null 2>&1 || true

###############################################################################
# 4. REPORT — the point of the script. An exit code cannot tell you whether the
#    image can actually drive the dongles you own.
###############################################################################
echo ""
echo "=== WiFi driver summary ==="
if [[ -s "$DRIVER_STATE" ]]; then
  printf '  %-34s %s\n' "ITEM" "RESULT"
  while IFS=: read -r k v; do
    [[ -n "${k:-}" ]] && printf '  %-34s %s\n' "$k" "${v:-?}"
  done < "$DRIVER_STATE"
fi
echo ""
echo "=== dongles present ==="
lsusb 2>/dev/null | grep -iE "wireless|wlan|802\.11|realtek|ralink|mediatek|atheros" \
  || echo "  (none detected — plug one in and re-check)"
echo "=== wireless interfaces ==="
iw dev 2>/dev/null | grep -E "Interface" || echo "  (none — driver missing, or dongle still in CD-ROM mode)"
echo ""
echo "State: $DRIVER_STATE   Log: $LOG"
echo "Driver matrix: cs/clients/linux/WIFI-DRIVERS.md (repo)"
echo "In lsusb but no wl* and no dmesg error => CD-ROM mode:"
echo "  usb_modeswitch -v <vid> -p <pid> -J"
