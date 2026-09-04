#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/recipe_budget_service}"
CONFIG_FILE="/etc/recipe-budget-ddns.env"
SERVICE_USER="${SUDO_USER:-${USER:?Current user is unknown}}"

PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

if [ ! -f "$CONFIG_FILE" ]; then
    sudo install -m 600 "$PROJECT_DIR/ops/reg-ru-ddns.env.example" "$CONFIG_FILE"
    echo "Created $CONFIG_FILE. Fill in REG.RU credentials, then run this installer again."
    exit 1
fi

SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT
sed \
    -e "s|__SERVICE_USER__|$SERVICE_USER|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/ops/reg-ru-ddns.service" > "$SERVICE_TMP"
sudo install -m 644 "$SERVICE_TMP" /etc/systemd/system/reg-ru-ddns.service
sudo install -m 644 "$PROJECT_DIR/ops/reg-ru-ddns.timer" /etc/systemd/system/reg-ru-ddns.timer
sudo systemctl daemon-reload
sudo systemctl enable --now reg-ru-ddns.timer
sudo systemctl start reg-ru-ddns.service
sudo systemctl status reg-ru-ddns.service --no-pager
