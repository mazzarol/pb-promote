#!/bin/bash
# PB-Promote — one-shot deployment script
# Run as root on the BinaryLane VPS (103.249.238.138)
#
# Usage:
#   sudo bash deploy.sh
#
# This will:
#   1. Install system dependencies
#   2. Create directory structure
#   3. Set up Python virtualenv
#   4. Install systemd service
#   5. Start PB-Promote on http://127.0.0.1:8080
#
# After deployment, access via SSH tunnel:
#   ssh -L 8080:127.0.0.1:8080 user@103.249.238.138
#   Then open http://localhost:8080 in your browser.

set -euo pipefail

APP_ROOT="/opt/pb-promote"
VENV="$APP_ROOT/venv"
REPO_URL="${PB_PROMOTE_REPO:-https://github.com/priorityblinds/pb-promote.git}"

echo "=== PB-Promote Deployment ==="
echo "Target: $APP_ROOT"
echo ""

# --- 1. System dependencies ---
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git postgresql-client

# --- 2. Clone or update repo ---
echo "[2/6] Cloning/updating repository..."
if [ -d "$APP_ROOT/.git" ]; then
    cd "$APP_ROOT"
        git pull origin master
else
    if [ -d "$APP_ROOT" ]; then
        echo "  WARNING: $APP_ROOT exists but is not a git repo — backing up"
        mv "$APP_ROOT" "$APP_ROOT.bak.$(date +%Y%m%d%H%M%S)"
    fi
    git clone "$REPO_URL" "$APP_ROOT"
fi

# --- 3. Create directories ---
echo "[3/6] Creating directory structure..."
mkdir -p "$APP_ROOT/backups"
mkdir -p "$APP_ROOT/logs"
chown -R root:root "$APP_ROOT"

# --- 4. Python virtualenv ---
echo "[4/6] Setting up Python virtual environment..."
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$APP_ROOT/requirements.txt" -q

# --- 5. Odoo API key ---
if [ ! -f "$APP_ROOT/odoo_api_key.txt" ] && [ -n "${ODOO_API_KEY:-}" ]; then
    echo "$ODOO_API_KEY" > "$APP_ROOT/odoo_api_key.txt"
    chmod 600 "$APP_ROOT/odoo_api_key.txt"
    echo "  API key saved to $APP_ROOT/odoo_api_key.txt"
elif [ ! -f "$APP_ROOT/odoo_api_key.txt" ]; then
    echo "  WARNING: No ODOO_API_KEY set. Create $APP_ROOT/odoo_api_key.txt manually."
    echo "  Get an API key from Odoo: Profile → Account Security → New API Key"
fi

# --- 6. Systemd service ---
echo "[5/6] Installing systemd service..."
cp "$APP_ROOT/pb-promote.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable pb-promote

# --- Start ---
echo "[6/6] Starting PB-Promote..."
systemctl restart pb-promote
sleep 2

if systemctl is-active --quiet pb-promote; then
    echo ""
    echo "=== DEPLOYMENT SUCCESSFUL ==="
    echo "PB-Promote is running on http://127.0.0.1:8080"
    echo ""
    echo "Access via SSH tunnel:"
    echo "  ssh -L 8080:127.0.0.1:8080 root@103.249.238.138"
    echo "  Then open http://localhost:8080"
    echo ""
    echo "Or via Tailscale:"
    echo "  http://$(tailscale ip -4 2>/dev/null || echo '<tailscale-ip>'):8080"
    echo ""
    echo "View logs:"
    echo "  journalctl -u pb-promote -f"
    echo "  tail -f $APP_ROOT/logs/app.log"
else
    echo ""
    echo "=== DEPLOYMENT FAILED ==="
    echo "Check logs: journalctl -u pb-promote -n 50"
    systemctl status pb-promote --no-pager
    exit 1
fi
