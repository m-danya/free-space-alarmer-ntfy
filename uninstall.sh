#!/usr/bin/env bash
set -euo pipefail

APP_NAME="free-space-alarmer-ntfy"
APP_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
WRAPPER_BIN="/usr/local/bin/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

"${SUDO[@]}" systemctl disable --now "${APP_NAME}.timer" >/dev/null 2>&1 || true
"${SUDO[@]}" systemctl stop "${APP_NAME}.service" >/dev/null 2>&1 || true

"${SUDO[@]}" rm -f "${SERVICE_FILE}" "${TIMER_FILE}" "${WRAPPER_BIN}"
"${SUDO[@]}" rm -rf "${APP_DIR}" "${CONFIG_DIR}"
"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl reset-failed "${APP_NAME}.service" "${APP_NAME}.timer" >/dev/null 2>&1 || true

echo "Uninstalled ${APP_NAME}."
