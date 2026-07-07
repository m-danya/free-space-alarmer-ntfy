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

prompt_required() {
  local prompt="$1"
  local value=""
  while [[ -z "${value}" ]]; do
    read -r -p "${prompt}: " value
  done
  printf '%s' "${value}"
}

echo "ntfy base URL example: https://ntfy-base-server.ru"
ntfy_base_url="$(prompt_required "ntfy base URL")"
ntfy_base_url="${ntfy_base_url%/}"

while [[ ! "${ntfy_base_url}" =~ ^https?:// ]]; do
  echo "Please enter a full URL starting with http:// or https://"
  ntfy_base_url="$(prompt_required "ntfy base URL")"
  ntfy_base_url="${ntfy_base_url%/}"
done

ntfy_topic="$(prompt_required "ntfy topic")"
ntfy_topic="${ntfy_topic#/}"
ntfy_topic="${ntfy_topic%/}"

echo 'Optional bearer token. Example: curl -H "Authorization: Bearer 78c5506d0740a58.........." -d "<Текст сообщения>" https://ntfy-base-server.ru/<topic>'
read -r -p "ntfy bearer token [empty]: " ntfy_bearer_token

default_machine_name="$(hostname -f 2>/dev/null || hostname)"
read -r -p "Machine name [${default_machine_name}]: " machine_name
machine_name="${machine_name:-${default_machine_name}}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

tmp_config="${tmp_dir}/config.json"
tmp_wrapper="${tmp_dir}/${APP_NAME}"
tmp_service="${tmp_dir}/${APP_NAME}.service"
tmp_timer="${tmp_dir}/${APP_NAME}.timer"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=(python3)
else
  PYTHON_CMD=("${UV_BIN}" run python)
fi

"${PYTHON_CMD[@]}" - "${tmp_config}" "${ntfy_base_url}" "${ntfy_topic}" "${ntfy_bearer_token}" "${machine_name}" <<'PY'
import json
from pathlib import Path
import sys

config_path, ntfy_base_url, ntfy_topic, ntfy_bearer_token, machine_name = sys.argv[1:]
token = ntfy_bearer_token.strip()
config = {
    "ntfy_base_url": ntfy_base_url.strip(),
    "ntfy_topic": ntfy_topic.strip(),
    "ntfy_bearer_token": token or None,
    "machine_name": machine_name.strip(),
    "threshold_free_percent": 10.0,
}

Path(config_path).write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

cat >"${tmp_wrapper}" <<WRAPPER
#!/usr/bin/env bash
set -euo pipefail
exec "${UV_BIN}" run --script "${APP_DIR}/free_space_alarmer_ntfy.py" "\$@"
WRAPPER

cat >"${tmp_service}" <<SERVICE
[Unit]
Description=Check local disk free space and notify via ntfy
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=${WRAPPER_BIN} --config ${CONFIG_FILE}
SERVICE

cat >"${tmp_timer}" <<TIMER
[Unit]
Description=Run free-space ntfy alarm every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
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
echo "Send test messages for all selected disks: sudo ${APP_NAME} --config ${CONFIG_FILE} --test"
