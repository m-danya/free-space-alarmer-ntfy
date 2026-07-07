#!/usr/bin/env bash
set -euo pipefail

APP_NAME="free-space-alarmer-ntfy"
APP_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
CONFIG_FILE="${CONFIG_DIR}/config.json"
WRAPPER_BIN="/usr/local/bin/${APP_NAME}"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
TIMER_FILE="/etc/systemd/system/${APP_NAME}.timer"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="${SCRIPT_DIR}/free_space_alarmer_ntfy.py"

if [[ ! -f "${SOURCE_SCRIPT}" ]]; then
  echo "Cannot find ${SOURCE_SCRIPT}" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl is required to install the systemd timer." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it first: https://docs.astral.sh/uv/" >&2
  exit 1
fi

UV_BIN="$(command -v uv)"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

tmp_config="${tmp_dir}/config.json"
tmp_existing_config="${tmp_dir}/existing-config.json"
tmp_timer_interval="${tmp_dir}/timer-interval-hours"
tmp_wrapper="${tmp_dir}/${APP_NAME}"
tmp_service="${tmp_dir}/${APP_NAME}.service"
tmp_timer="${tmp_dir}/${APP_NAME}.timer"

existing_config_path="${CONFIG_FILE}"
if [[ -f "${CONFIG_FILE}" && ! -r "${CONFIG_FILE}" && "${#SUDO[@]}" -gt 0 ]]; then
  if "${SUDO[@]}" cat "${CONFIG_FILE}" >"${tmp_existing_config}"; then
    existing_config_path="${tmp_existing_config}"
  else
    echo "Could not read ${CONFIG_FILE}; using installer defaults." >&2
    existing_config_path=""
  fi
fi

"${UV_BIN}" run --script "${SOURCE_SCRIPT}" \
  --configure-install \
  --existing-config "${existing_config_path}" \
  --existing-config-label "${CONFIG_FILE}" \
  --output-config "${tmp_config}" \
  --output-timer-interval "${tmp_timer_interval}"

timer_interval_hours="$(<"${tmp_timer_interval}")"

cat >"${tmp_wrapper}" <<WRAPPER
#!/usr/bin/env bash
set -euo pipefail
exec "${UV_BIN}" run --script "${APP_DIR}/free_space_alarmer_ntfy.py" "\$@"
WRAPPER

cat >"${tmp_service}" <<SERVICE
[Unit]
Description=Check local disk free space and send configured notifications
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=${WRAPPER_BIN} --config ${CONFIG_FILE}
SERVICE

cat >"${tmp_timer}" <<TIMER
[Unit]
Description=Run free-space alarm every ${timer_interval_hours}h

[Timer]
OnBootSec=5min
OnUnitActiveSec=${timer_interval_hours}h
AccuracySec=5min
Persistent=true
Unit=${APP_NAME}.service

[Install]
WantedBy=timers.target
TIMER

"${SUDO[@]}" install -Dm755 "${SOURCE_SCRIPT}" "${APP_DIR}/free_space_alarmer_ntfy.py"
"${SUDO[@]}" install -Dm755 "${tmp_wrapper}" "${WRAPPER_BIN}"
"${SUDO[@]}" install -Dm600 "${tmp_config}" "${CONFIG_FILE}"
"${SUDO[@]}" install -Dm644 "${tmp_service}" "${SERVICE_FILE}"
"${SUDO[@]}" install -Dm644 "${tmp_timer}" "${TIMER_FILE}"

"${SUDO[@]}" systemctl daemon-reload
"${SUDO[@]}" systemctl enable --now "${APP_NAME}.timer"

echo "Installed ${APP_NAME}."
echo "Timer status: systemctl status ${APP_NAME}.timer"
echo "Send test messages for selected disks after blacklist: sudo ${APP_NAME} --config ${CONFIG_FILE} --test"
