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
for arg in "$@"; do
  [[ "$arg" == "--debug" || "$arg" == "-d" ]] && DEBUG=1
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
IS_PI=false
if grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
  IS_PI=true
fi
# Also catch Pi via cpuinfo (older firmware / no device-tree)
if ! $IS_PI && grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
  IS_PI=true
fi
# Also catch Pi VMs and Raspberry Pi Desktop for x86/x64 — these may not
# have Pi hardware signatures but do have /etc/rpi-issue (all Pi OS variants)
# or identify as raspbian/raspberry-pi-os in /etc/os-release
if ! $IS_PI && [[ -f /etc/rpi-issue ]]; then
  IS_PI=true
fi
if ! $IS_PI && grep -qiE "raspbian|raspberry pi os" /etc/os-release 2>/dev/null; then
  IS_PI=true
fi

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
if $IS_PI; then
  printf "${COL_DIM}  Platform: Raspberry Pi (raspberrypi-kernel-headers)${COL_RESET}\n"
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
if ! $IS_PI; then
  info "Adding Raspberry Pi apt repository for PIXEL desktop packages"
  curl -fsSL https://archive.raspberrypi.org/debian/raspberrypi.gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/raspberrypi-archive-keyring.gpg \
    >>"$LOG" 2>&1
  echo "deb [signed-by=/usr/share/keyrings/raspberrypi-archive-keyring.gpg] \
http://archive.raspberrypi.org/debian/ bookworm main" \
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

# Kernel headers package name differs between Debian x86 and Raspberry Pi OS
if $IS_PI; then
  KERNEL_HEADERS="raspberrypi-kernel-headers"
else
  KERNEL_HEADERS="linux-headers-$(uname -r)"
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

# Pi already has a desktop environment (PIXEL/LXDE) — don't replace it
# On Debian x86/x64 VMs install PIXEL (from Pi repo) + lightdm/xorg
if ! $IS_PI; then
  PACKAGES+=("raspberrypi-ui-mods" "raspberrypi-artwork" "lightdm" "lxde-core" "xorg")
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
  mkdir -p /etc/lightdm/lightdm.conf.d
  cat >/etc/lightdm/lightdm.conf.d/50-autologin.conf <<LIGHTDM_EOF
[Seat:*]
autologin-user=$SIM_USER
autologin-user-timeout=0
autologin-session=LXDE-pi
user-session=LXDE-pi
greeter-session=lightdm-greeter
LIGHTDM_EOF
  ok "LightDM autologin → $SIM_USER (session: LXDE-pi)"
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

CLIENT_SIM_REPO="https://github.com/solutions-hpe/client-sim.git"
CLIENT_SIM_DIR="$HOME/client-sim"

info "Cloning solutions-hpe/client-sim"
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

    # ── Shell scripts ────────────────────────────────────────────────────────
    info "Copying shell scripts to /usr/local/scripts"
    if compgen -G "*.sh" &>/dev/null; then
      cp *.sh /usr/local/scripts/ >>"$LOG" 2>&1
      ok "Shell scripts copied"
    else
      warn "No .sh files found in $LINUX_DIR"
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

WIFI_SRC="/usr/src/wifi-drivers"
mkdir -p "$WIFI_SRC"
cd "$WIFI_SRC"

# ── Reboot suppression shim ──────────────────────────────────────────────────
SUPPRESS="$(mktemp -d)"
for c in reboot shutdown poweroff halt; do
  printf '#!/bin/sh\necho "[SUPPRESSED] %s called — ignored during driver install"\nexit 0\n' "$c" \
    >"$SUPPRESS/$c"
  chmod +x "$SUPPRESS/$c"
done
cat >"$SUPPRESS/systemctl" <<'SHIM'
#!/bin/sh
case "${1:-}" in
  reboot|shutdown|poweroff|halt)
    echo "[SUPPRESSED] systemctl $* ignored during driver install"
    exit 0 ;;
  *) exec /bin/systemctl "$@" ;;
esac
SHIM
chmod +x "$SUPPRESS/systemctl"
OLD_PATH="$PATH"
export PATH="$SUPPRESS:$PATH"
# ────────────────────────────────────────────────────────────────────────────

# Format: "dir-name|type|repo-url|dkms-module|pinned-tag|modprobe-module"
# Types:
#   morrownr  — uses install-driver.sh NoPrompt
#   aircrack  — uses install-driver.sh (with stdin echo)
#   lwfinger  — bare Makefile; source copied to /usr/src then registered with DKMS
#   dkms-only — bare Makefile + dkms.conf; DKMS-managed, no install-driver.sh
# modprobe-module: use "-" if no explicit modprobe needed after install
#
# Bug fixes applied:
#   - rtl8812au (aircrack-ng) removed — duplicate of 8812au-20210820 (same chipset, conflict)
#   - rtw89 changed from type morrownr→dkms-only (repo has no install-driver.sh)
#   - rtw89 skipped at runtime if kernel ≥ 5.16 (driver is in-tree on modern kernels)
DRIVERS=(
  "8821au-20210708|morrownr|https://github.com/morrownr/8821au-20210708.git|8821au|HEAD|-"
  "8821cu-20210916|morrownr|https://github.com/morrownr/8821cu-20210916.git|8821cu|HEAD|-"
  "8814au|morrownr|https://github.com/morrownr/8814au.git|8814au|HEAD|-"
  "8812au-20210820|morrownr|https://github.com/morrownr/8812au-20210820.git|8812au|HEAD|-"
  "rtl8852bu-20240418|morrownr|https://github.com/morrownr/rtl8852bu-20240418.git|8852bu|HEAD|-"
  "rtl8852cu-20240510|morrownr|https://github.com/morrownr/rtl8852cu-20240510.git|8852cu|HEAD|-"
  "88x2bu-20210702|morrownr|https://github.com/morrownr/88x2bu-20210702.git|88x2bu|HEAD|-"
  "rtw89|dkms-only|https://github.com/morrownr/rtw89.git|rtw89|HEAD|-"
  "rtl8188eu|lwfinger|https://github.com/lwfinger/rtl8188eu.git|8188eu|HEAD|-"
  "rtl8723au|lwfinger|https://github.com/lwfinger/rtl8723au.git|8723au|HEAD|8723au"
  "rtl8852au|lwfinger|https://github.com/lwfinger/rtl8852au.git|8852au|HEAD|-"
)

TOTAL_DRIVERS="${#DRIVERS[@]}"
DRIVER_NUM=0

for entry in "${DRIVERS[@]}"; do
  SAVED_IFS="$IFS"
  IFS='|' read -r NAME TYPE REPO MOD PIN MODPROBE <<<"$entry"
  IFS="$SAVED_IFS"

  DRIVER_NUM=$(( DRIVER_NUM + 1 ))

  phase_step "$DRIVER_NUM" "$TOTAL_DRIVERS"
  info "Driver $DRIVER_NUM/$TOTAL_DRIVERS: $NAME"

  rm -rf "$NAME"
  CLONE_ARGS=(--depth=1)
  [[ "$PIN" != "HEAD" ]] && CLONE_ARGS+=(--branch "$PIN")

  info "Cloning $NAME [$DRIVER_NUM/$TOTAL_DRIVERS]"
  if git clone "${CLONE_ARGS[@]}" "$REPO" "$NAME" >>"$LOG" 2>&1; then
    cd "$NAME"

  INSTALL_OK=true
    case "$TYPE" in
      morrownr)
        if [[ -x ./install-driver.sh ]]; then
          info "Building $NAME (morrownr)"
          ./install-driver.sh NoPrompt >>"$LOG" 2>&1 || INSTALL_OK=false
        else
          warn "$NAME: install-driver.sh not found or not executable"
          INSTALL_OK=false
        fi
        ;;

      aircrack)
        if [[ -x ./install-driver.sh ]]; then
          info "Building $NAME (aircrack-ng)"
          echo "" | ./install-driver.sh >>"$LOG" 2>&1 || INSTALL_OK=false
        else
          warn "$NAME: install-driver.sh not found or not executable"
          INSTALL_OK=false
        fi
        ;;

      dkms-only)
        # Repos that have dkms.conf + Makefile but no install-driver.sh (e.g. morrownr/rtw89).
        # Also skips rtw89 entirely on kernels >= 5.16 where it is already in-tree.
        if [[ "$MOD" == "rtw89" ]]; then
          KVER_MAJOR=$(uname -r | cut -d. -f1)
          KVER_MINOR=$(uname -r | cut -d. -f2)
          if (( KVER_MAJOR > 5 || ( KVER_MAJOR == 5 && KVER_MINOR >= 16 ) )); then
            info "Skipping $NAME — rtw89 is built-in to kernel $(uname -r) (>= 5.16)"
            echo "$NAME:SKIPPED_IN_TREE" >>"$DRIVER_STATE"
            cd "$WIFI_SRC"; continue
          fi
        fi

        DKMS_VER="0.0"
        [[ -f dkms.conf ]] && DKMS_VER="$(grep 'PACKAGE_VERSION=' dkms.conf | cut -d'"' -f2 || echo "0.0")"

        SRC_DEST="/usr/src/${MOD}-${DKMS_VER}"
        info "Installing $NAME via DKMS ($MOD/$DKMS_VER)"

        # Copy source into /usr/src where dkms expects it
        rm -rf "$SRC_DEST"
        cp -r "$(pwd)" "$SRC_DEST"

        dkms add    -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 || true
        dkms build  -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 \
          && dkms install -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 \
          || { INSTALL_OK=false; }

        if [[ "$MODPROBE" != "-" && -n "$MODPROBE" ]]; then
          info "Loading module: $MODPROBE"
          modprobe "$MODPROBE" >>"$LOG" 2>&1 \
            || warn "modprobe $MODPROBE failed (may need reboot)"
          ok "Module $MODPROBE loaded"
        fi
        ;;

      lwfinger)
        # Build only (no make install) — DKMS manages the module lifecycle.
        # Source must be copied to /usr/src/MOD-VER/ before dkms add.
        info "Building $NAME (lwfinger)"
        if make all >>"$LOG" 2>&1; then

          DKMS_VER="0.0"
          [[ -f dkms.conf ]] && DKMS_VER="$(grep 'PACKAGE_VERSION=' dkms.conf | cut -d'"' -f2 || echo "0.0")"

          SRC_DEST="/usr/src/${MOD}-${DKMS_VER}"
          info "Registering $NAME with DKMS ($MOD/$DKMS_VER)"

          # Copy source into /usr/src where dkms expects it, then register
          rm -rf "$SRC_DEST"
          cp -r "$(pwd)" "$SRC_DEST"

          dkms add    -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 || true
          dkms build  -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 \
            && dkms install -m "$MOD" -v "$DKMS_VER" >>"$LOG" 2>&1 \
            || { warn "$NAME: dkms build/install failed"; INSTALL_OK=false; }

          if $INSTALL_OK && [[ "$MODPROBE" != "-" && -n "$MODPROBE" ]]; then
            info "Loading module: $MODPROBE"
            modprobe "$MODPROBE" >>"$LOG" 2>&1 \
              || warn "modprobe $MODPROBE failed (may need reboot)"
            ok "Module $MODPROBE loaded"
          fi
        else
          INSTALL_OK=false
        fi
        ;;
    esac

    cd "$WIFI_SRC"

    if $INSTALL_OK; then
      echo "$NAME:INSTALLED" >>"$DRIVER_STATE"
      ok "✓ $NAME installed [$DRIVER_NUM/$TOTAL_DRIVERS]"
    else
      echo "$NAME:FAILED" >>"$DRIVER_STATE"
      warn "✗ $NAME build/install failed [$DRIVER_NUM/$TOTAL_DRIVERS]"
    fi
  else
    echo "$NAME:CLONE_FAILED" >>"$DRIVER_STATE"
    warn "✗ Failed to clone $NAME [$DRIVER_NUM/$TOTAL_DRIVERS]"
  fi
done

info "Running depmod -a"
depmod -a >>"$LOG" 2>&1

export PATH="$OLD_PATH"
rm -rf "$SUPPRESS"
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