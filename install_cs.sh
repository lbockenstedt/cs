#!/bin/bash
set -euo pipefail

# ============================================================
# Lab Manager — Client Simulator (CS) Spoke Installer
#
# Deploys the LM client simulator spoke.
# Safe to re-run (updates code, preserves credentials).
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/lbockenstedt/cs/main/install_cs.sh \
#     | sudo bash -s -- --hub ws://HUB_IP:8765
# ============================================================

HUB_URL="ws://localhost:8765"
SPOKE_ID="cs-spoke-1"
SPOKE_SECRET=""
HUB_SECRET=""
ADMIN_TOKEN=""
SVC_USER="svc_lm"
LM_DIR="/opt/lm"

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub)         HUB_URL="$2";      shift ;;
        --id|--name)   SPOKE_ID="$2";     shift ;;
        --secret)      SPOKE_SECRET="$2"; shift ;;
        --hub-secret)  HUB_SECRET="$2";   shift ;;
        --admin-token) ;; # deprecated
        --all-prereqs) ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
    shift
done

[ "$(id -u)" -eq 0 ] || { echo "❌ Must be run as root."; exit 1; }

GRN='\033[0;32m'; YLW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}✅  $*${NC}"; }
warn() { echo -e "${YLW}⚠️   $*${NC}"; }
step() { echo -e "\n${GRN}━━  $*  ━━${NC}"; }

step "Lab Manager — Generic Agent Installer"

# ── System packages ───────────────────────────────────────────────────────────
step "Installing system packages"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    python3 python3-venv python3-pip git curl jq
ok "Packages ready"

# ── Service user ──────────────────────────────────────────────────────────────
if ! id "$SVC_USER" &>/dev/null; then
    useradd -r -s /bin/false -M "$SVC_USER"
    ok "Created service user $SVC_USER"
fi

# ── Spoke secret (preserve on re-run) ────────────────────────────────────────
EXISTING_SECRET=""
[ -f "$LM_DIR/cs/.env" ] && \
    EXISTING_SECRET=$(grep "^SPOKE_SECRET=" "$LM_DIR/cs/.env" | cut -d= -f2-)

if [ -z "$SPOKE_SECRET" ]; then
    if [ -n "$EXISTING_SECRET" ]; then
        SPOKE_SECRET="$EXISTING_SECRET"
        ok "Reusing existing spoke secret"
    else
        warn "No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
    fi
fi

# ── Clone / update ────────────────────────────────────────────────────────────
step "Installing Generic Agent"
mkdir -p "$LM_DIR"
if [ -d "$LM_DIR/cs/.git" ]; then
    echo "   Updating existing install"
    git -C "$LM_DIR/cs" pull --rebase --autostash origin main -q
else
    echo "   Cloning CS / Generic Agent repo"
    git clone -q https://github.com/lbockenstedt/cs.git "$LM_DIR/cs"
fi

# ── Python venv ───────────────────────────────────────────────────────────────
rm -rf "$LM_DIR/cs/venv"
python3 -m venv "$LM_DIR/cs/venv"
"$LM_DIR/cs/venv/bin/pip" install --upgrade pip -q
[ -f "$LM_DIR/cs/requirements.txt" ] && \
    "$LM_DIR/cs/venv/bin/pip" install -r "$LM_DIR/cs/requirements.txt" -q
ok "Dependencies installed"

# ── .env ──────────────────────────────────────────────────────────────────────
cat > "$LM_DIR/cs/.env" <<DOTENV
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=${HUB_SECRET:-}
DOTENV
chmod 600 "$LM_DIR/cs/.env"

# ── systemd unit ──────────────────────────────────────────────────────────────

cat > /etc/systemd/system/lm-cs.service <<SYSD
[Unit]
Description=Lab Manager Spoke - Generic Agent
After=network.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$LM_DIR/cs
EnvironmentFile=$LM_DIR/cs/.env
Environment="PYTHONPATH=$LM_DIR:$LM_DIR/core/src:$LM_DIR/cs/src"
ExecStart=$LM_DIR/cs/venv/bin/python3 -m src.control_plane \
    --id $SPOKE_ID \
    --secret $SPOKE_SECRET \
    --hub-secret "${HUB_SECRET:-}" \
    --hub $HUB_URL \

StandardOutput=append:/var/log/lm/lm-cs.log
StandardError=append:/var/log/lm/lm-cs.log
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
SYSD

systemctl daemon-reload
systemctl enable lm-cs
systemctl restart lm-cs
ok "Generic Agent service started"

chown -R "$SVC_USER:$SVC_USER" "$LM_DIR/cs" 2>/dev/null || true

LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "this-host")
echo ""
echo "════════════════════════════════════════════"
ok "Generic Agent installation complete!"
echo "════════════════════════════════════════════"
echo "  LM Hub:       $HUB_URL"
echo "  Spoke ID:     $SPOKE_ID"
echo "  Version:      $(cat $LM_DIR/cs/VERSION 2>/dev/null || echo unknown)"
echo "  Status:       sudo systemctl status lm-cs"
echo ""
