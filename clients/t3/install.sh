#!/bin/bash
# install.sh — T3 wireless simulation installer
# Run as root on a fresh headless Debian/Ubuntu system.
# Creates /usr/scripts/, installs all T3 files, generates the
# /etc/udev/rules.d/90-Wireless.rules virtual interface file,
# and optionally enables the systemd service for headless autostart.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/usr/scripts"
UDEV_RULES="/etc/udev/rules.d/90-Wireless.rules"
SERVICE_DEST="/etc/systemd/system/t3-simulation.service"

# ── Root check ────────────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root (sudo ./install.sh)"
    exit 1
fi

echo "=============================="
echo " T3 Simulation Installer"
echo "=============================="

# ── Detect WiFi PHY name (default phy0) ──────────────────────────────────────
PHY=$(ls /sys/class/ieee80211/ 2>/dev/null | head -1)
PHY="${PHY:-phy0}"
echo "Detected WiFi PHY: $PHY"

# ── Generate MAC host seed (octet 5) from hostname hash ───────────────────────
# Hash the full hostname so the same hostname always produces the same MAC
# pattern across reinstalls. md5sum gives 32 hex chars — we take the first 2.
# If hostname has no trailing number, we still hash but warn the operator.
RAW_NUM=$(hostname | grep -oE '[0-9]+$')
HOST_OCT=$(printf "%s" "$(hostname)" | md5sum | cut -c1-2)

if [ -z "$RAW_NUM" ]; then
    echo ""
    echo "  *** WARNING ***"
    echo "  Hostname '$(hostname)' does not end in a number."
    echo "  MAC octet 5 has been derived from a hash of the hostname: $HOST_OCT"
    echo "  This value is deterministic — re-running on this host will produce"
    echo "  the same MACs. For a predictable/readable value, rename this host"
    echo "  to end in a number (e.g. client-sim-01) and re-run the installer."
    echo ""
fi
echo "Hostname: $(hostname)  →  MAC seed (hash): 07:${HOST_OCT}  (octet 6 = interface number)"

# ── Create install directory ──────────────────────────────────────────────────
echo "Creating $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# ── Copy scripts and config ───────────────────────────────────────────────────
echo "Installing scripts to $INSTALL_DIR"
cp "$SCRIPT_DIR/wireless.sh"           "$INSTALL_DIR/wireless.sh"
cp "$SCRIPT_DIR/update_script.sh"      "$INSTALL_DIR/update_script.sh"
cp "$SCRIPT_DIR/gen_macs.sh"           "$INSTALL_DIR/gen_macs.sh"
cp "$SCRIPT_DIR/ini-parser.sh"         "$INSTALL_DIR/ini-parser.sh"

# Copy simulation.conf only if not already present (preserve local settings)
if [ ! -f "$INSTALL_DIR/simulation.conf" ]; then
    cp "$SCRIPT_DIR/../simulation.conf" "$INSTALL_DIR/simulation.conf"
    echo "  Installed simulation.conf (edit [t3] section to match your environment)"
else
    echo "  simulation.conf already exists — skipping (preserving local settings)"
fi

# Copy mac_config.json only if not already present (API server owns it after install)
if [ ! -f "$INSTALL_DIR/mac_config.json" ]; then
    cp "$SCRIPT_DIR/mac_config.json"   "$INSTALL_DIR/mac_config.json"
    echo "  Installed mac_config.json (default 25-interface OUI map)"
else
    echo "  mac_config.json already exists — skipping (API server manages this file)"
fi

chmod 755 "$INSTALL_DIR"/*.sh
chmod -x  "$INSTALL_DIR/simulation.conf" \
          "$INSTALL_DIR/mac_config.json" 2>/dev/null || true

# ── Generate udev virtual interface rules ────────────────────────────────────
# Each vwlan gets a unique MAC. The OUI prefix is chosen to match the
# simulated device type; last 3 octets are 07:91:NN (NN = interface number).
echo "Generating $UDEV_RULES  (PHY=$PHY, MAC seed=07:${HOST_OCT})"

cat > "$UDEV_RULES" << EOF
ACTION=="add", SUBSYSTEM=="ieee80211", KERNEL=="$PHY", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan1  type station addr 00:0f:e5:07:${HOST_OCT}:01", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan2  type station addr cc:6a:10:07:${HOST_OCT}:02", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan3  type station addr 90:ac:3f:07:${HOST_OCT}:03", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan4  type station addr cc:69:fa:07:${HOST_OCT}:04", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan5  type station addr 28:cd:c1:07:${HOST_OCT}:05", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan6  type station addr f0:f6:c1:07:${HOST_OCT}:06", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan7  type station addr 5c:60:ba:07:${HOST_OCT}:07", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan8  type station addr 64:16:7f:07:${HOST_OCT}:08", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan9  type station addr ac:cc:8e:07:${HOST_OCT}:09", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan10 type station addr 00:1b:54:07:${HOST_OCT}:10", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan11 type station addr 2e:0b:57:07:${HOST_OCT}:11", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan12 type station addr 9c:76:13:07:${HOST_OCT}:12", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan13 type station addr 48:a2:e6:07:${HOST_OCT}:13", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan14 type station addr 00:04:a5:07:${HOST_OCT}:14", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan15 type station addr 00:12:5f:07:${HOST_OCT}:15", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan16 type station addr b8:80:4f:07:${HOST_OCT}:16", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan17 type station addr 2c:26:17:07:${HOST_OCT}:17", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan18 type station addr 4c:fc:aa:07:${HOST_OCT}:18", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan19 type station addr 00:10:7f:07:${HOST_OCT}:19", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan20 type station addr c0:56:e3:07:${HOST_OCT}:20", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan21 type station addr 3c:15:c2:07:${HOST_OCT}:21", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan22 type station addr 40:b8:37:07:${HOST_OCT}:22", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan23 type station addr fc:65:de:07:${HOST_OCT}:23", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan24 type station addr 44:61:32:07:${HOST_OCT}:24", \\
	RUN+="/usr/sbin/iw phy %k interface add vwlan25 type station addr 80:5e:c0:07:${HOST_OCT}:25"
EOF

chmod -x "$UDEV_RULES"
echo "  Written: $UDEV_RULES"
echo "  Interfaces: vwlan1-25  |  MAC pattern: OUI:OUI:OUI:07:${HOST_OCT}:NN"

# ── Reload udev rules ─────────────────────────────────────────────────────────
echo "Reloading udev rules"
udevadm control --reload-rules
udevadm trigger

# ── Install systemd service ───────────────────────────────────────────────────
echo "Installing systemd service"
cp "$SCRIPT_DIR/t3-simulation.service" "$SERVICE_DEST"
systemctl daemon-reload
systemctl enable t3-simulation
echo "  Service enabled: t3-simulation"

# ── Install dependencies check ────────────────────────────────────────────────
echo "Checking required packages"
missing=()
for pkg in iw dhcpcd5 wpa-supplicant smbclient wget curl dnsutils; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        missing+=("$pkg")
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    echo "  Installing missing packages: ${missing[*]}"
    apt-get install -y "${missing[@]}"
else
    echo "  All required packages present"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=============================="
echo " Installation complete"
echo "=============================="
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/simulation.conf — update the [t3] section"
echo "     with your SSID, passwords, and server addresses"
echo "  2. Reboot (udev will create vwlan1-25 automatically on boot)"
echo "     OR manually trigger: udevadm trigger --subsystem-match=ieee80211"
echo "  3. Service starts automatically on boot via systemd"
echo "     To start now: systemctl start t3-simulation"
echo ""
echo "  PHY used in udev rules: $PHY"
echo "  MAC octet 5 (from hostname): $HOST_OCT"
echo "  If your adapter uses a different PHY, re-run or edit $UDEV_RULES"
