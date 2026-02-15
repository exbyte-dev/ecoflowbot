#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────
# setup-service.sh — install / update the EcoFlow Discord Bot
#                     as a systemd service.
#
# Usage:
#   sudo ./setup-service.sh          # first-time install or update
#
# What it does (idempotent — safe to re-run):
#   1. Creates a Python venv if it doesn't exist
#   2. Installs / updates pip + requirements.txt
#   3. Writes (or overwrites) the systemd unit file
#   4. Reloads systemd, enables on boot, and (re)starts the service
# ────────────────────────────────────────────────────────────────
set -euo pipefail

SERVICE_NAME="ecoflowbot"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ── Colours ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Must be root ───────────────────────────────────────────────
[[ $EUID -eq 0 ]] || fail "This script must be run with sudo."

# ── Resolve paths ──────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_USER="${SUDO_USER:-${LOGNAME:-$(whoami)}}"
PYTHON="python3"
VENV_DIR="${PROJECT_DIR}/venv"
REQ_FILE="${PROJECT_DIR}/requirements.txt"

info "Project directory : ${PROJECT_DIR}"
info "Service user      : ${BOT_USER}"

# ── Sanity checks ──────────────────────────────────────────────
[[ -f "${PROJECT_DIR}/bot.py" ]]  || fail "bot.py not found in ${PROJECT_DIR}"
[[ -f "${REQ_FILE}" ]]            || fail "requirements.txt not found"
[[ -f "${PROJECT_DIR}/.env" ]]    || fail ".env not found — copy .env.example and fill in your credentials first"
command -v ${PYTHON} &>/dev/null  || fail "python3 is not installed"

# ── Virtual environment ───────────────────────────────────────
if [[ ! -d "${VENV_DIR}" ]]; then
    info "Creating virtual environment…"
    sudo -u "${BOT_USER}" ${PYTHON} -m venv "${VENV_DIR}"
    ok "Virtual environment created"
else
    ok "Virtual environment exists"
fi

# ── Install / update dependencies ─────────────────────────────
info "Installing / updating dependencies…"
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip --quiet
sudo -u "${BOT_USER}" "${VENV_DIR}/bin/pip" install --upgrade -r "${REQ_FILE}" --quiet
ok "Dependencies up to date"

# ── Write systemd unit file ───────────────────────────────────
info "Writing ${UNIT_FILE}…"
cat > "${UNIT_FILE}" <<EOF
[Unit]
Description=EcoFlow Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python bot.py
Restart=on-failure
RestartSec=10
EnvironmentFile=${PROJECT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF
ok "Unit file written"

# ── Enable and (re)start ──────────────────────────────────────
info "Reloading systemd…"
systemctl daemon-reload

info "Enabling ${SERVICE_NAME} to start on boot…"
systemctl enable "${SERVICE_NAME}" --quiet

info "Restarting ${SERVICE_NAME}…"
systemctl restart "${SERVICE_NAME}"
ok "Service restarted"

echo ""
echo -e "${GREEN}──────────────────────────────────────────────────────${NC}"
echo -e "${GREEN}  ${SERVICE_NAME} is installed and running.${NC}"
echo -e "${GREEN}  It will start automatically on boot.${NC}"
echo -e "${GREEN}──────────────────────────────────────────────────────${NC}"
echo ""

# ── Show status ────────────────────────────────────────────────
systemctl status "${SERVICE_NAME}" --no-pager || true
