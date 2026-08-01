#!/usr/bin/env bash
###############################################################################
# Client Simulator Installer v0.09
###############################################################################

set -euo pipefail
export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"

###############################################################################
# Debug flag  (sudo bash install.sh --debug)
###############################################################################
DEBUG=0
PIXEL_DESKTOP=0
for arg in "$@"; do
  [[ "$arg" == "--debug" || "$arg" == "-d" ]] && DEBUG=1
  # Opt in to the Pi PIXEL desktop on plain Debian. OFF by default: it pulls
  # +rpt packages that block every future release upgrade.
  [[ "$arg" == "--pixel-desktop" ]] && PIXEL_DESKTOP=1
done

###############################################################################
# Root check
###############################################################################
if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: This script must be run as root (e.g. sudo $0)" >&2
  exit 1
fi

###############################################################################
# Non-interactive guarantees
###############################################################################
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1          # belt-and-suspenders for needrestart
export GIT_TERMINAL_PROMPT=0
export UCF_FORCE_CONFFOLD=1           # stop ucf (rsyslog/others) from prompting
export APT_LISTCHANGES_FRONTEND=none  # suppress apt-listchanges pager

VERSION="0.17"
INSTALL_START=$(date +%s)
WARN_COUNT=0
ERR_COUNT=0
PHASE_START=0

###############################################################################
# Platform detection
###############################################################################
# HARDWARE and OS are detected SEPARATELY, because they drive different
# decisions and conflating them is a real bug:
#
#   * which kernel-headers package exists depends on the KERNEL (only actual Pi
#     hardware runs an rpi kernel with raspberrypi-kernel-headers);
#   * whether Pi repos/desktop apply depends on the OS.
#
# "Raspberry Pi Desktop for PC" is the case that breaks a single flag: it is Pi
# OS branding on x86 hardware with a STOCK Debian kernel. The old single IS_PI
# saw /etc/rpi-issue, chose raspberrypi-kernel-headers, and that package does
# not exist for an x86 kernel — so headers never installed and every DKMS driver
# build failed with "kernel source not found". Observed in the field as
#   "errors were encountered while processing: linux-headers-6.1.0-51-amd64"
IS_PI_HW=false        # real Raspberry Pi hardware (=> rpi kernel)
IS_PI_OS=false        # Raspberry Pi OS / Raspberry Pi Desktop userland

if grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
  IS_PI_HW=true
fi
# Older firmware with no device-tree still names the board in cpuinfo.
if ! $IS_PI_HW && grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
  IS_PI_HW=true
fi
# /etc/rpi-issue is present on EVERY Pi OS variant, including the x86 desktop.
if [[ -f /etc/rpi-issue ]]; then
  IS_PI_OS=true
fi
if ! $IS_PI_OS && grep -qiE "raspbian|raspberry pi os" /etc/os-release 2>/dev/null; then
  IS_PI_OS=true
fi
# Pi-repo packages carry a +rpt version suffix. Their presence means Pi repos are
# (or were) configured even when the branding files are gone.
if ! $IS_PI_OS && dpkg -l 2>/dev/null | grep -q '+rpt'; then
  IS_PI_OS=true
fi

# Backwards compatibility for any later reference: IS_PI now means HARDWARE.
IS_PI=$IS_PI_HW

###############################################################################
# Logging
###############################################################################
STATE_DIR="/var/lib/client-sim"
LOG="/var/log/client-sim_install.log"
DRIVER_STATE="$STATE_DIR/wlan-drivers.state"

mkdir -p "$STATE_DIR"
: >"$LOG"
: >"$DRIVER_STATE"
chmod 644 "$LOG" "$DRIVER_STATE"

ts()   { date "+%H:%M:%S"; }
info() { echo "[$(ts)] INFO: $*" | tee -a "$LOG"; }
ok()   { echo "[$(ts)] OK:   $*" | tee -a "$LOG"; }
warn() { WARN_COUNT=$(( WARN_COUNT + 1 )); echo "[$(ts)] WARN: $*" | tee -a "$LOG"; }
err()  { ERR_COUNT=$(( ERR_COUNT + 1 ));  echo "[$(ts)] ERR:  $*" | tee -a "$LOG" >&2; }

###############################################################################
# PHASE TRACKING
###############################################################################
PHASE_NAMES=(
  "User Provisioning"
  "Package Install"
  "GNOME Configuration"
  "Scripts Directory"
  "Client-Sim Repo"
  "SMB Config Sync"
  "rsyslog Config"
  "WLAN Drivers"
  "Health Check"
)

CURRENT_PHASE=0

COL_RESET="\033[0m"
COL_GREEN="\033[0;32m"
COL_RED="\033[0;31m"
COL_CYAN="\033[0;36m"
COL_YELLOW="\033[1;33m"
COL_BOLD="\033[1m"
COL_DIM="\033[2m"

begin_phase() {
  PHASE_START=$(date +%s)
  local name="${PHASE_NAMES[$CURRENT_PHASE]:-Unknown}"
  printf "\n${COL_BOLD}──── Phase $((CURRENT_PHASE+1))/${#PHASE_NAMES[@]}: %s ────${COL_RESET}\n" "$name" | tee -a "$LOG"
}

end_phase() {
  local elapsed=$(( $(date +%s) - PHASE_START ))
  local name="${PHASE_NAMES[$CURRENT_PHASE]:-Unknown}"
  printf "${COL_GREEN}✓${COL_RESET} %s complete ${COL_DIM}(%ds)${COL_RESET}\n" "$name" "$elapsed" | tee -a "$LOG"
  CURRENT_PHASE=$(( CURRENT_PHASE + 1 ))
}

phase_step() { :; }   # no-op — progress bar removed

trap 'echo; printf "${COL_RED}Installation cancelled by user.${COL_RESET}\n"; exit 130' INT

###############################################################################
# Startup banner
###############################################################################
{
echo
echo "============================================================"
echo " Client Simulator Installer v${VERSION}"
echo " Started at: $(date)"
[[ "$DEBUG" -eq 1 ]] && echo " *** DEBUG MODE — apt output shown on screen ***"
echo "============================================================"
echo
} | tee -a "$LOG"


# ── Pre-flight summary ───────────────────────────────────────────────────────
printf "${COL_DIM}  Log    : %s${COL_RESET}\n" "$LOG"
if $IS_PI_HW; then
  printf "${COL_DIM}  Platform: Raspberry Pi hardware (raspberrypi-kernel-headers)${COL_RESET}\n"
elif $IS_PI_OS; then
  printf "${COL_DIM}  Platform: Raspberry Pi OS on non-Pi hardware — using stock Debian headers${COL_RESET}\n"
else
  printf "${COL_DIM}  Platform: Debian x86/VM (linux-headers-$(uname -r))${COL_RESET}\n"
fi
printf "${COL_DIM}  Press Ctrl+C at any time to abort.${COL_RESET}\n"
echo

###############################################################################
# HELPER: retry wrapper
###############################################################################
retry() {
  local attempts=3 delay=5 cmd=("$@")
  for ((i=1; i<=attempts; i++)); do
    "${cmd[@]}" && return 0
    warn "Command failed (attempt $i/$attempts): ${cmd[*]}"
    sleep "$delay"
  done
  return 1
}

###############################################################################
# HELPER: apt_run — silent normally, live output in --debug mode
#         timeout baked in: APT_TIMEOUT seconds (default 300)
# Usage: apt_run [apt-get args...]
###############################################################################
APT_TIMEOUT=300
apt_run() {
  if [[ "$DEBUG" -eq 1 ]]; then
    printf "\n${COL_DIM}  [DEBUG] apt-get %s${COL_RESET}\n" "$*"
    timeout "$APT_TIMEOUT" apt-get "$@" 2>&1 | tee -a "$LOG"
    local rc=${PIPESTATUS[0]}
    return $rc
  else
    timeout "$APT_TIMEOUT" apt-get "$@" >>"$LOG" 2>&1
  fi
}

###############################################################################
# PHASE 1 — USER PROVISIONING + SCOPED SUDO
###############################################################################
begin_phase
SIM_USER="sim-user"

info "Checking user '$SIM_USER'"
if ! id "$SIM_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$SIM_USER" >>"$LOG" 2>&1
  ok "Created user '$SIM_USER'"
else
  ok "User '$SIM_USER' already exists"
fi

info "Configuring sudoers"

# sim-user: scoped passwordless sudo for simulation operations
cat >/etc/sudoers.d/99-simuser-nopasswd <<EOF
# Managed by client-sim-install.sh — do not edit manually
$SIM_USER ALL=(ALL) NOPASSWD: /usr/bin/apt-get, /usr/sbin/dpkg, /bin/systemctl, /sbin/depmod, /usr/sbin/dkms
EOF
chmod 0440 /etc/sudoers.d/99-simuser-nopasswd

if ! visudo -cf /etc/sudoers.d/99-simuser-nopasswd >>"$LOG" 2>&1; then
  err "sudoers fragment failed validation — removing"
  rm -f /etc/sudoers.d/99-simuser-nopasswd
  exit 1
fi
ok "Scoped passwordless sudo configured for '$SIM_USER'"

# user: full passwordless sudo — required for driver builds (morrownr install-driver.sh calls sudo internally)
if id "user" &>/dev/null; then
  cat >/etc/sudoers.d/99-user-nopasswd <<EOF
# Managed by client-sim-install.sh — do not edit manually
user ALL=(ALL) NOPASSWD: ALL
EOF
  chmod 0440 /etc/sudoers.d/99-user-nopasswd
  if ! visudo -cf /etc/sudoers.d/99-user-nopasswd >>"$LOG" 2>&1; then
    err "sudoers fragment for 'user' failed validation — removing"
    rm -f /etc/sudoers.d/99-user-nopasswd
    exit 1
  fi
  ok "Full passwordless sudo configured for 'user'"
else
  warn "User 'user' does not exist — skipping its sudoers entry"
fi

# ── SMB credentials template ─────────────────────────────────────────────────
info "Checking SMB credentials file"
SMB_CREDS_DIR="/etc/client-sim"
SMB_CREDS="$SMB_CREDS_DIR/smb-credentials"
mkdir -p "$SMB_CREDS_DIR"
if [[ ! -f "$SMB_CREDS" ]]; then
  cat >"$SMB_CREDS" <<'CREDS'
# client-sim SMB credentials — edit before running installer
# username=your_username
# password=your_password
# domain=your_domain
CREDS
  chmod 600 "$SMB_CREDS"
  warn "SMB credentials template created at $SMB_CREDS — edit it to enable SMB sync"
else
  chmod 600 "$SMB_CREDS"
  ok "SMB credentials file already exists"
fi
end_phase

###############################################################################
# PHASE 2 — PACKAGE INSTALL  (update + upgrade + install)
###############################################################################
begin_phase

info "Updating package lists"
retry apt_run update --quiet=2
ok "Package lists updated"

# ── Raspberry Pi apt repo (non-Pi Debian only) ───────────────────────────────
# Adds the Pi Foundation's repo so we can install the PIXEL desktop theme
# (raspberrypi-ui-mods, raspberrypi-artwork) on plain Debian x86/x64 VMs,
# giving them the same look as Pi OS hardware.
# OPT-IN ONLY (--pixel-desktop). Adding the Pi repo to plain Debian is what
# installs +rpt packages, and those have NO upgrade path: Raspberry Pi Desktop
# for PC was never released past bookworm, so a bullseye->bookworm->trixie
# upgrade dies on each +rpt package in turn (dphys-swapfile, raspberrypi-ui-mods,
# ...). An image that will be release-upgraded must not have this repo.
if [[ "${PIXEL_DESKTOP:-0}" == "1" ]] && ! $IS_PI_OS; then
  info "Adding Raspberry Pi apt repository for PIXEL desktop packages (--pixel-desktop)"
  curl -fsSL https://archive.raspberrypi.org/debian/raspberrypi.gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    >>"$LOG" 2>&1
  # Use the RUNNING release codename. Hard-coding bookworm pulled bookworm
  # packages onto a bullseye or trixie system — a silent cross-release mix.
  _rel="$(. /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-bookworm}")"
  echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] \
http://archive.raspberrypi.org/debian/ ${_rel} main" \
    > /etc/apt/sources.list.d/raspberrypi.list
  # Lower priority so Pi repo never overrides standard Debian packages
  cat >/etc/apt/preferences.d/raspberrypi <<'PINEOF'
Package: *
Pin: origin archive.raspberrypi.org
Pin-Priority: 100
PINEOF
  retry apt_run update --quiet=2
  ok "Raspberry Pi apt repository added"
fi

# Pre-seed debconf answers for packages known to prompt interactively.
# samba-common ignores DEBIAN_FRONTEND without do_debconf=false.
info "Pre-seeding debconf answers"
{
  # samba-common: do_debconf=false prevents ALL interactive questions
  echo "samba-common samba-common/do_debconf boolean false"
  echo "samba-common samba-common/workgroup string WORKGROUP"
  echo "samba-common samba-common/dhcp boolean false"
  echo "samba-common samba-common/smb.conf.update.template boolean false"
  echo "samba-common samba-common/smb.conf.upgrade boolean false"
  # rsyslog
  echo "rsyslog rsyslog/enable_all boolean false"
  # display manager — only pre-seed on non-Pi; Pi OS manages its own DM
  echo "lightdm shared/default-x-display-manager select lightdm"
  echo "gdm3 shared/default-x-display-manager select lightdm"
} | debconf-set-selections >>"$LOG" 2>&1
ok "debconf answers pre-seeded"

info "Upgrading existing packages"
retry apt_run upgrade -y --quiet \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold"
ok "System packages upgraded"

# Kernel headers: chosen by HARDWARE (which kernel is running), not by OS
# branding. raspberrypi-kernel-headers exists only for the rpi kernel — on
# "Raspberry Pi Desktop for PC" the branding says Pi while the kernel is stock
# Debian, and asking for the Pi package there fails, taking every DKMS driver
# build down with it.
if $IS_PI_HW; then
  KERNEL_HEADERS="raspberrypi-kernel-headers"
else
  KERNEL_HEADERS="linux-headers-$(uname -r)"
fi
# Verify rather than assume: if the chosen package is not installable, fall back
# to the other. Getting this wrong produces a wall of identical "kernel source
# not found" build failures whose real cause is one wrong package name.
if ! apt-cache show "$KERNEL_HEADERS" >/dev/null 2>&1; then
  if $IS_PI_HW; then
    warn "raspberrypi-kernel-headers unavailable — falling back to linux-headers-$(uname -r)"
    KERNEL_HEADERS="linux-headers-$(uname -r)"
  else
    warn "linux-headers-$(uname -r) unavailable — falling back to linux-headers-amd64"
    KERNEL_HEADERS="linux-headers-amd64"
  fi
fi

PACKAGES=(
  # Safe — no network impact
  "gnupg"
  "curl"
  "gnome-terminal"
  "wget"
  "git"
  "build-essential"
  "$KERNEL_HEADERS"
  "dkms"
  "rsyslog"
  "firefox-esr"
  "cpulimit"
  "iperf3"
  "net-tools"
  "dnsutils"
  "smbclient"
  "qemu-guest-agent"
  # Network-disruptive — install last so connection stays up for all prior downloads
  "rfkill"
  "network-manager"
  "network-manager-gnome"
)

# Pi hardware already has a desktop environment (PIXEL/LXDE) — don't replace it.
# On plain Debian x86/x64 VMs install the SAME machinery Pi OS uses — lightdm +
# LXDE (Openbox WM) + xorg — all from Debian main, on both amd64 and arm64.
#
# Deliberately NOT installed: raspberrypi-ui-mods / raspberrypi-artwork. Those are
# theme-only (+rpt, from the Pi Foundation repo) and they are what makes a Debian
# box un-upgradeable — every release upgrade then dies package-by-package on the
# +rpt set (dphys-swapfile, raspberrypi-ui-mods, ...). Dropping them costs the Pi
# LOOK and nothing else: the window manager, display manager, autologin and window
# positioning are all identical, so there is ONE package set to test across plain
# Debian and real Pi hardware instead of two.
if ! $IS_PI; then
  PACKAGES+=("lightdm" "lxde-core" "xorg")
  # Escape hatch: --pixel-desktop (which also added the Pi repo above) layers the
  # Pi look back on top. Same machinery either way — this only adds the theme, and
  # it re-introduces the +rpt upgrade problem, so it stays opt-in.
  if [[ "${PIXEL_DESKTOP:-0}" == "1" ]]; then
    PACKAGES+=("raspberrypi-ui-mods" "raspberrypi-artwork")
  fi
fi
TOTAL_PKGS="${#PACKAGES[@]}"
INSTALLED_COUNT=0

for pkg in "${PACKAGES[@]}"; do
  INSTALLED_COUNT=$(( INSTALLED_COUNT + 1 ))
  phase_step "$INSTALLED_COUNT" "$TOTAL_PKGS"
  info "Installing ($INSTALLED_COUNT/$TOTAL_PKGS): $pkg"
  if apt_run install -y --quiet \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      "$pkg"; then
    ok "Installed: $pkg"
  else
    warn "Failed: $pkg — retrying"
    APT_TIMEOUT=180
    apt_run install -y --quiet \
      -o Dpkg::Options::="--force-confdef" \
      -o Dpkg::Options::="--force-confold" \
      "$pkg" \
      && ok "Installed: $pkg" \
      || warn "Could not install: $pkg (non-fatal, continuing)"
    APT_TIMEOUT=300
  fi
done
info "Running autoremove"
apt_run autoremove -y --quiet=2
ok "Core dependencies installed"

# ── jq — JSON processor used by agent.sh and sys_mon.sh ─────────────────────
# Pulled out of main loop: can hang on some OS versions. Short timeout + non-fatal.
info "Installing jq"
APT_TIMEOUT=60
apt_run install -y --quiet jq \
  && ok "Installed jq" \
  || warn "Could not install jq — agent.sh JSON parsing will fall back to python3"
APT_TIMEOUT=300

# ── python3-websockets — only extra Python dep; python3 is pre-installed ────
# All other python3 usage (JSON parsing) replaced with jq.
# websockets is needed solely for the async WebSocket loop in agent.sh.
info "Installing python3 websockets library"
APT_TIMEOUT=60
apt_run install -y --quiet python3-websockets \
  && ok "Installed python3-websockets" \
  || warn "Could not install python3-websockets — agent.sh hub connection will be unavailable"
APT_TIMEOUT=300

info "Disabling NetworkManager MAC randomization"
# WHY: NM's defaults randomize the wifi MAC (scan-rand-mac-address + a
# stable-random cloned-mac on new profiles) — "Privacy"/"Random MAC". That
# breaks two things on a sim client: (1) the AP/ClearPass/NetBox see an
# inconsistent MAC per scan/associate so device identity & dhcp_fail telemetry
# are unreliable, and (2) dhcp_fail's spoof is driven through the profile's
# 802-11-wireless.cloned-mac-address (see simulation.sh connect_wifi) — a
# random default there would race the spoof. Pin both legs to PERMANENT: the
# device uses its real factory MAC unless dhcp_fail explicitly pins a spoof.
# connect_wifi always sets cloned-mac on the profile (spoof OR real), so this
# default only governs the bootstrap window before the first associate.
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/99-client-sim-no-mac-random.conf <<'NM_EOF'
# Managed by client-sim-install.sh — do not edit manually
# Disable MAC randomization on sim clients (deterministic device identity).
[device]
wifi.scan-rand-mac-address=no

[connection]
wifi.cloned-mac-address=permanent
ethernet.cloned-mac-address=permanent
NM_EOF
chmod 644 /etc/NetworkManager/conf.d/99-client-sim-no-mac-random.conf

info "Restarting network stack"
systemctl restart NetworkManager 2>/dev/null || true
sleep 3
systemctl restart networking 2>/dev/null || true
sleep 3
ok "Network stack restarted"
end_phase

###############################################################################
# PHASE 3 — DISPLAY MANAGER & POWER MANAGEMENT
###############################################################################
begin_phase

# ── LightDM autologin ────────────────────────────────────────────────────────
# Write the autologin config AFTER lightdm is installed (Phase 2).
# Without this block LightDM always shows the greeter — autologin never fires.
# On Pi hardware/VMs, Pi OS manages its own display manager — skip this.
if ! $IS_PI; then
  info "Configuring LightDM autologin for $SIM_USER"

  # Pick the session by what is actually installed rather than hard-coding it.
  # `LXDE-pi` is provided by raspberrypi-ui-mods; plain Debian's lxde-core
  # provides `LXDE`. Naming a session that has no .desktop file does NOT fail
  # loudly — LightDM just falls back to the greeter, so autologin quietly stops
  # working and the three autostart windows never appear. Probing keeps Pi OS
  # boxes on the exact session they already used while letting straight Debian
  # work with no second code path.
  LXSESSION="LXDE"
  [[ -f /usr/share/xsessions/LXDE-pi.desktop ]] && LXSESSION="LXDE-pi"

  mkdir -p /etc/lightdm/lightdm.conf.d
  cat >/etc/lightdm/lightdm.conf.d/50-autologin.conf <<LIGHTDM_EOF
[Seat:*]
autologin-user=$SIM_USER
autologin-user-timeout=0
autologin-session=$LXSESSION
user-session=$LXSESSION
greeter-session=lightdm-greeter
LIGHTDM_EOF
  ok "LightDM autologin → $SIM_USER (session: $LXSESSION)"
else
  ok "Pi platform — keeping native Pi OS desktop and display manager"
fi

if [[ -n "${DISPLAY:-}" && -n "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
  info "Applying screen power settings to current session"
  xset s noblank || true
  xset -dpms     || true
  xset s off     || true
  ok "Screen power management disabled"
else
  info "No active graphical session — power settings will apply on next login"
fi

if command -v raspi-config &>/dev/null; then
  info "Configuring Raspberry Pi locale and Wi-Fi region"
  raspi-config nonint do_change_locale en_US.UTF-8
  raspi-config nonint do_wifi_country US
  ok "Raspberry Pi locale and Wi-Fi region configured"
fi

end_phase

###############################################################################
# PHASE 4 — /usr/local/scripts setup
###############################################################################
begin_phase

info "Preparing /usr/local/scripts"
mkdir -p /usr/local/scripts
chmod a+rwx /usr/local/scripts

touch /usr/local/scripts/sim.log
chmod a+rw /usr/local/scripts/sim.log

# Write installer version to sim.log (from original script)
echo "Installer Version $VERSION" | tee /usr/local/scripts/sim.log >>"$LOG"

ok "/usr/local/scripts prepared — version $VERSION written to sim.log"
end_phase

###############################################################################
# PHASE 5 — CLIENT-SIM GITHUB REPO CLONE + FILE DEPLOYMENT
###############################################################################
begin_phase

CLIENT_SIM_REPO="https://github.com/lbockenstedt/cs.git"
CLIENT_SIM_DIR="$HOME/client-sim"

info "Cloning lbockenstedt/cs"
rm -rf "$CLIENT_SIM_DIR"
if retry git clone --depth=1 "$CLIENT_SIM_REPO" "$CLIENT_SIM_DIR" >>"$LOG" 2>&1; then
  ok "client-sim repo cloned"

  LINUX_DIR="$CLIENT_SIM_DIR/linux"
  CONFIGS_DIR="$CLIENT_SIM_DIR/configs"

  if [[ -d "$LINUX_DIR" ]]; then
    cd "$LINUX_DIR"

    # ── .desktop autostart files ─────────────────────────────────────────────
    info "Installing .desktop autostart files"
    if compgen -G "*.desktop" &>/dev/null; then
      cp *.desktop /etc/xdg/autostart/ >>"$LOG" 2>&1
      ok ".desktop autostart files installed"
    else
      warn "No .desktop files found in $LINUX_DIR"
    fi

    # ── PATH drop-in so 'bash update.sh' works from any login shell ──────────
    # /usr/local/scripts on PATH for every login shell (SSH, terminal, console).
    # startup.sh re-applies this best-effort at boot (self-heal for already-
    # deployed clients); this is the canonical install-time drop.
    info "Adding /usr/local/scripts to PATH (profile.d)"
    mkdir -p /etc/profile.d
    cat >/etc/profile.d/client-sim-path.sh <<'PATH_EOF'
# Managed by client-sim-install.sh — do not edit manually
# Put the sim scripts on PATH for every login shell.
case ":$PATH:" in
  *:/usr/local/scripts:*) ;;
  *) export PATH="$PATH:/usr/local/scripts" ;;
esac
PATH_EOF
    chmod 644 /etc/profile.d/client-sim-path.sh
    ok "/usr/local/scripts on PATH for login shells"

    # ── Shell scripts ────────────────────────────────────────────────────────
    info "Copying shell scripts to /usr/local/scripts"
    if compgen -G "*.sh" &>/dev/null; then
      cp *.sh /usr/local/scripts/ >>"$LOG" 2>&1
      ok "Shell scripts copied"
    else
      warn "No .sh files found in $LINUX_DIR"
    fi

    # ── Python helpers (invoked by the .sh scripts, e.g. dhcp_fire.py) ────────
    info "Copying Python helpers to /usr/local/scripts"
    if compgen -G "*.py" &>/dev/null; then
      cp *.py /usr/local/scripts/ >>"$LOG" 2>&1
      ok "Python helpers copied"
    else
      warn "No .py files found in $LINUX_DIR"
    fi

    # ── Flat text files ──────────────────────────────────────────────────────
    info "Copying text files to /usr/local/scripts"
    if compgen -G "*.txt" &>/dev/null; then
      cp *.txt /usr/local/scripts/ >>"$LOG" 2>&1
      ok "Text files copied"
    else
      warn "No .txt files found in $LINUX_DIR"
    fi

    # ── systemd units ────────────────────────────────────────────────────────
    info "Installing client-sim systemd units"
    unit_files=()
    if compgen -G "*.service" &>/dev/null; then
      unit_files+=( *.service )
    fi
    if compgen -G "*.timer" &>/dev/null; then
      unit_files+=( *.timer )
    fi
    if (( ${#unit_files[@]} > 0 )); then
      cp "${unit_files[@]}" /etc/systemd/system/ >>"$LOG" 2>&1
      chmod 644 /etc/systemd/system/client-sim-agent.service \
                /etc/systemd/system/client-sim-watchdog.service \
                /etc/systemd/system/client-sim-watchdog.timer >>"$LOG" 2>&1 || true
      ok "Systemd units installed"
    else
      warn "No client-sim systemd units found in $LINUX_DIR"
    fi

    # ── VERSION file ─────────────────────────────────────────────────────────
    if [[ -f "VERSION" ]]; then
      cp VERSION /usr/local/scripts/VERSION >>"$LOG" 2>&1
      ok "VERSION $(cat VERSION | tr -d '[:space:]') written to /usr/local/scripts"
    else
      warn "VERSION file not found in linux/ — update.sh will always sync"
    fi

    # ── simulation.conf (conditional — don't overwrite existing) ─────────────
    info "Checking simulation.conf"
    if [[ -f /usr/local/scripts/simulation.conf ]]; then
      ok "simulation.conf already exists — not overwriting"
    else
      if [[ -f "$CONFIGS_DIR/simulation.conf" ]]; then
        cp "$CONFIGS_DIR/simulation.conf" /usr/local/scripts/simulation.conf >>"$LOG" 2>&1
        ok "simulation.conf copied from configs directory"
      elif [[ -f "$LINUX_DIR/simulation.conf" ]]; then
        cp "$LINUX_DIR/simulation.conf" /usr/local/scripts/simulation.conf >>"$LOG" 2>&1
        ok "simulation.conf copied from linux directory"
      else
        warn "simulation.conf not found in repo — skipping"
      fi
    fi

    # ── user-overrides.conf ──────────────────────────────────────────────────
    if [[ -f "$CONFIGS_DIR/user-overrides.conf" ]]; then
      cp "$CONFIGS_DIR/user-overrides.conf" /usr/local/scripts/user-overrides.conf >>"$LOG" 2>&1
      ok "user-overrides.conf copied"
    else
      warn "user-overrides.conf not found in configs/ — skipping"
    fi

    # ── rsyslog config from repo ─────────────────────────────────────────────
    info "Checking for rsyslog config in repo"
    if [[ -f "$LINUX_DIR/10-rsyslog.conf" ]]; then
      info "Found 10-rsyslog.conf in repo — will apply in rsyslog phase"
      REPO_RSYSLOG_CONF="$LINUX_DIR/10-rsyslog.conf"
    else
      warn "No 10-rsyslog.conf in repo linux directory"
      REPO_RSYSLOG_CONF=""
    fi

    # ── Final permissions ────────────────────────────────────────────────────
    info "Setting permissions on /usr/local/scripts"
    find /usr/local/scripts -type d                -exec chmod a+rwx {} \; >>"$LOG" 2>&1
    find /usr/local/scripts -type f -name "*.sh"   -exec chmod a+rx  {} \; >>"$LOG" 2>&1
    find /usr/local/scripts -type f ! -name "*.sh" -exec chmod a+rw  {} \; >>"$LOG" 2>&1
    ok "Permissions set on /usr/local/scripts"

    if [[ -f /etc/systemd/system/client-sim-agent.service ]]; then
      info "Enabling client-sim agent service"
      systemctl daemon-reload >>"$LOG" 2>&1 || true
      existing_agent_pid=$(cat /tmp/client-sim-ws-agent.pid 2>/dev/null || true)
      if [[ "$existing_agent_pid" =~ ^[0-9]+$ ]] && \
         ps -o args= -p "$existing_agent_pid" 2>/dev/null | grep -q '/usr/local/scripts/agent.sh'; then
        kill "$existing_agent_pid" 2>/dev/null || true
      fi
      systemctl enable client-sim-agent.service >>"$LOG" 2>&1 || warn "Failed to enable client-sim-agent.service"
      systemctl restart client-sim-agent.service >>"$LOG" 2>&1 || warn "Failed to restart client-sim-agent.service"
      if [[ -f /etc/systemd/system/client-sim-watchdog.timer ]]; then
        systemctl enable --now client-sim-watchdog.timer >>"$LOG" 2>&1 || warn "Failed to enable client-sim-watchdog.timer"
      fi
      ok "Client-sim agent service configured"
    fi

    cd "$HOME"
  else
    warn "linux/ directory not found in client-sim repo — skipping file deployment"
    REPO_RSYSLOG_CONF=""
  fi
else
  warn "Failed to clone client-sim repo — skipping file deployment"
  REPO_RSYSLOG_CONF=""
fi

end_phase

###############################################################################
# PHASE 6 — SMB CONFIG SYNC  (authenticated + checksum validated)
###############################################################################
begin_phase

SMB_SHARE="//nas/scripts"
SMB_REMOTE_DIR="/SIM/CONFIG"

# Check credentials exist and have been filled in (not just the template)
if [[ ! -f "$SMB_CREDS" ]]; then
  warn "SMB credentials file not found at $SMB_CREDS — skipping SMB sync"
elif grep -qE '^\s*#|^[[:space:]]*$' "$SMB_CREDS" && ! grep -qE '^username=' "$SMB_CREDS"; then
  warn "SMB credentials file is still a template — edit $SMB_CREDS to enable SMB sync"
else
  chmod 600 "$SMB_CREDS"
  info "Syncing config files from SMB share"
  if smbclient "$SMB_SHARE" --authentication-file="$SMB_CREDS" -c \
      "lcd /usr/local/scripts; cd $SMB_REMOTE_DIR; prompt off; mget *.conf" \
      >>"$LOG" 2>&1; then

    MANIFEST="/usr/local/scripts/checksums.sha256"
    if [[ -f "$MANIFEST" ]]; then
      info "Verifying SMB file checksums"
      if ! (cd /usr/local/scripts && sha256sum -c "$MANIFEST" >>"$LOG" 2>&1); then
        err "Checksum verification FAILED — aborting"
        exit 1
      fi
      ok "SMB file checksums verified"
    else
      warn "No checksums.sha256 manifest found — skipping integrity check"
    fi
    ok "SMB config sync complete"
  else
    warn "SMB config sync failed — continuing without remote config"
  fi
fi

end_phase

###############################################################################
# PHASE 7 — RSYSLOG CUSTOM CONFIG
# Priority: repo file > SMB-synced file > skip
###############################################################################
begin_phase

RSYSLOG_SOURCE=""
if [[ -n "${REPO_RSYSLOG_CONF:-}" && -f "$REPO_RSYSLOG_CONF" ]]; then
  RSYSLOG_SOURCE="$REPO_RSYSLOG_CONF"
  info "Using rsyslog config from GitHub repo"
elif [[ -f /usr/local/scripts/10-rsyslog.conf ]]; then
  RSYSLOG_SOURCE="/usr/local/scripts/10-rsyslog.conf"
  info "Using rsyslog config from /usr/local/scripts (SMB)"
fi

if [[ -n "$RSYSLOG_SOURCE" ]]; then
  info "Installing rsyslog config"
  mkdir -p /etc/rsyslog.d
  cp "$RSYSLOG_SOURCE" /etc/rsyslog.d/10-rsyslog.conf
  # Validate the full rsyslog config (including the new drop-in) not just the snippet
  if rsyslogd -N1 >>"$LOG" 2>&1; then
    systemctl restart rsyslog || true
    systemctl enable  rsyslog || true
    ok "rsyslog configured from $RSYSLOG_SOURCE"
  else
    warn "rsyslog config validation failed — reverting"
    rm -f /etc/rsyslog.d/10-rsyslog.conf
  fi
else
  warn "No rsyslog config source found — skipping"
fi

end_phase

###############################################################################
# PHASE 9 — WLAN DRIVERS INSTALL
###############################################################################
begin_phase

# The driver set lives in clients/linux/install_wifi_drivers.sh so it is
# maintained in ONE place and can be re-run on a live client when a new dongle
# shows up, without re-running this whole installer. It uses the same
# clone-live-from-GitHub + morrownr/lwfinger/dkms-only build model this phase
# used to carry inline, and additionally installs the firmware / usb_modeswitch
# / wireless-regdb layer that was missing here entirely:
#
#   * MediaTek dongles in the fleet (0846:9041, 0e8d:c616, 2357:0105) run on
#     MAINLINE mt76 drivers — the module loads and the device is dead without
#     firmware-mediatek / firmware-misc-nonfree.
#   * 0bda:1a2b is not a NIC until usb_modeswitch flips it out of CD-ROM mode.
#   * RTL8192EU (0bda:818b) is in the fleet and had no out-of-tree driver here.
#
# LOG and DRIVER_STATE are exported so its output folds into this installer's
# log and the PHASE 10 health summary keeps working unchanged.
WIFI_DRIVER_PKG=""
for _c in "$(dirname "$0")/../clients/linux/install_wifi_drivers.sh" \
          /usr/local/scripts/install_wifi_drivers.sh \
          "$(dirname "$0")/install_wifi_drivers.sh"; do
    [[ -f "$_c" ]] && { WIFI_DRIVER_PKG="$_c"; break; }
done

if [[ -n "$WIFI_DRIVER_PKG" ]]; then
    info "WLAN drivers: $WIFI_DRIVER_PKG"
    LOG="$LOG" DRIVER_STATE="$DRIVER_STATE" bash "$WIFI_DRIVER_PKG" \
        || warn "driver package returned non-zero — see $LOG"
else
    warn "install_wifi_drivers.sh not found — NO wifi drivers or firmware installed."
    warn "Fetch it with: curl -fsSL https://raw.githubusercontent.com/lbockenstedt/cs/main/clients/linux/install_wifi_drivers.sh -o /usr/local/scripts/install_wifi_drivers.sh"
fi
ok "WLAN driver installation complete"
end_phase

###############################################################################
# PHASE 10 — FINAL HEALTH SUMMARY
###############################################################################
begin_phase

echo ""
{
# Helper functions for colored health check rows (output goes to tee below)
_hc_ok()   { printf "  \033[0;32m✓\033[0m  %-24s \033[0;32mOK\033[0m\n"       "$1"; }
_hc_warn()  { printf "  \033[1;33m✗\033[0m  %-24s \033[1;33m%s\033[0m\n"      "$1" "$2"; }
_hc_fail()  { printf "  \033[0;31m✗\033[0m  %-24s \033[0;31m%s\033[0m\n"      "$1" "$2"; }
_hc_drv_ok(){ printf "  \033[0;32m✓\033[0m  %-35s \033[0;32mINSTALLED\033[0m\n" "$1"; }
_hc_drv_fail(){ printf "  \033[0;31m✗\033[0m  %-35s \033[0;31m%s\033[0m\n"    "$1" "$2"; }

echo "================ HEALTH CHECK ================"
id "$SIM_USER" &>/dev/null \
  && _hc_ok   "User ($SIM_USER)" \
  || _hc_fail "User ($SIM_USER)" "MISSING"
[[ -f /etc/sudoers.d/99-simuser-nopasswd ]] \
  && _hc_ok   "Scoped sudoers" \
  || _hc_fail "Scoped sudoers" "MISSING"
systemctl is-active --quiet lightdm \
  && _hc_ok   "LightDM" \
  || _hc_warn "LightDM" "NOT ACTIVE"
[[ -f /etc/lightdm/lightdm.conf.d/50-autologin.conf ]] \
  && grep -q "autologin-user=$SIM_USER" /etc/lightdm/lightdm.conf.d/50-autologin.conf \
  && _hc_ok   "LightDM autologin" \
  || _hc_fail "LightDM autologin" "NOT CONFIGURED"
systemctl is-active --quiet NetworkManager \
  && _hc_ok   "NetworkManager" \
  || _hc_fail "NetworkManager" "NOT ACTIVE"
lsmod | grep -qE '^(88|rtw|rtl)' \
  && _hc_ok   "WLAN modules" \
  || _hc_warn "WLAN modules" "NOT LOADED (reboot may be needed)"
[[ -f /usr/local/scripts/simulation.conf ]] \
  && _hc_ok   "simulation.conf" \
  || _hc_fail "simulation.conf" "MISSING"
[[ -f /etc/rsyslog.d/10-rsyslog.conf ]] \
  && _hc_ok   "rsyslog config" \
  || _hc_warn "rsyslog config" "NOT INSTALLED"
systemctl is-active --quiet client-sim-agent.service \
  && _hc_ok   "Client agent" \
  || _hc_warn "Client agent" "NOT ACTIVE"
systemctl is-active --quiet client-sim-watchdog.timer \
  && _hc_ok   "Agent watchdog timer" \
  || _hc_warn "Agent watchdog timer" "NOT ACTIVE"

echo ""
echo "  ---- Driver State ----"
while IFS=: read -r drv status; do
  case "$status" in
    INSTALLED)        _hc_drv_ok   "$drv" ;;
    SKIPPED_IN_TREE)  _hc_warn     "$drv (in-tree)" "SKIPPED — already in kernel" ;;
    FAILED)           _hc_drv_fail "$drv" "FAILED" ;;
    CLONE_FAILED)     _hc_drv_fail "$drv" "CLONE FAILED" ;;
    *)                printf "  ?  %-35s %s\n" "$drv" "$status" ;;
  esac
done < "$DRIVER_STATE"

echo "============================================="
} | tee -a "$LOG"

end_phase

###############################################################################
# END
###############################################################################
TOTAL_ELAPSED=$(( $(date +%s) - INSTALL_START ))
ELAPSED_MIN=$(( TOTAL_ELAPSED / 60 ))
ELAPSED_SEC=$(( TOTAL_ELAPSED % 60 ))

{
echo "============================================================"
printf " Installation complete — reboot recommended\n"
printf " Total time : %dm %02ds\n" "$ELAPSED_MIN" "$ELAPSED_SEC"
if [[ "$WARN_COUNT" -gt 0 ]]; then
  printf " Warnings   : %d  (see %s)\n" "$WARN_COUNT" "$LOG"
else
  printf " Warnings   : 0\n"
fi
if [[ "$ERR_COUNT" -gt 0 ]]; then
  printf " Errors     : %d  (see %s)\n" "$ERR_COUNT" "$LOG"
else
  printf " Errors     : 0\n"
fi
printf " Full log   : %s\n" "$LOG"
printf " Driver state: %s\n" "$DRIVER_STATE"
printf " Sim log    : /usr/local/scripts/sim.log\n"
echo "============================================================"
} | tee -a "$LOG" | while IFS= read -r line; do
  if   echo "$line" | grep -q "Warnings" && [[ "$WARN_COUNT" -gt 0 ]]; then
    printf "${COL_YELLOW}%s${COL_RESET}\n" "$line"
  elif echo "$line" | grep -q "Errors"   && [[ "$ERR_COUNT"  -gt 0 ]]; then
    printf "${COL_RED}%s${COL_RESET}\n" "$line"
  elif echo "$line" | grep -q "complete"; then
    printf "${COL_GREEN}%s${COL_RESET}\n" "$line"
  else
    echo "$line"
  fi
done