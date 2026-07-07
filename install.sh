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

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=(python3)
else
  PYTHON_CMD=("${UV_BIN}" run python)
fi

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

DEFAULT_THRESHOLD_FREE_PERCENT="10"
DEFAULT_NOTIFY_NOT_BEFORE="10:00"
DEFAULT_NOTIFY_NOT_AFTER="20:00"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

tmp_config="${tmp_dir}/config.json"
tmp_candidates="${tmp_dir}/candidates.json"
tmp_blacklist="${tmp_dir}/blacklist.json"
tmp_existing_config="${tmp_dir}/existing-config.json"
tmp_wrapper="${tmp_dir}/${APP_NAME}"
tmp_service="${tmp_dir}/${APP_NAME}.service"
tmp_timer="${tmp_dir}/${APP_NAME}.timer"

printf '{}\n' >"${tmp_existing_config}"
existing_config_present=0

load_existing_config_defaults() {
  local reader=("${PYTHON_CMD[@]}")

  if [[ ! -r "${CONFIG_FILE}" && "${#SUDO[@]}" -gt 0 ]]; then
    reader=("${SUDO[@]}" "${PYTHON_CMD[@]}")
  fi

  "${reader[@]}" - "${CONFIG_FILE}" "${tmp_existing_config}" "${DEFAULT_THRESHOLD_FREE_PERCENT}" "${DEFAULT_NOTIFY_NOT_BEFORE}" "${DEFAULT_NOTIFY_NOT_AFTER}" <<'PY'
import json
import math
import re
import sys
from pathlib import Path

config_path, defaults_path, default_threshold, default_not_before, default_not_after = sys.argv[1:]
config = json.loads(Path(config_path).read_text(encoding="utf-8"))


def string_value(key):
    value = config.get(key)
    if value is None:
        return ""
    return str(value).strip()


def normalize_url(value):
    value = value.rstrip("/")
    if re.fullmatch(r"https?://.+", value):
        return value
    return ""


def normalize_topic(value):
    return value.strip("/")


def normalize_threshold(value):
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return default_threshold
    if not math.isfinite(threshold) or threshold < 0 or threshold > 100:
        return default_threshold
    return str(value)


def normalize_time(value, fallback):
    match = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", str(value).strip())
    if not match:
        return fallback
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


def blacklist_mount_points():
    blacklist = config.get("blacklist", {})
    if not isinstance(blacklist, dict):
        return []
    values = blacklist.get("mount_points", [])
    if not isinstance(values, list):
        return []

    mount_points = []
    for value in values:
        mount_point = str(value).strip()
        if mount_point and mount_point not in mount_points:
            mount_points.append(mount_point)
    return mount_points


defaults = {
    "ntfy_base_url": normalize_url(string_value("ntfy_base_url")),
    "ntfy_topic": normalize_topic(string_value("ntfy_topic")),
    "ntfy_bearer_token": string_value("ntfy_bearer_token"),
    "machine_name": string_value("machine_name"),
    "threshold_free_percent": normalize_threshold(
        config.get("threshold_free_percent", default_threshold)
    ),
    "notify_not_before": normalize_time(
        config.get("notify_not_before", default_not_before),
        default_not_before,
    ),
    "notify_not_after": normalize_time(
        config.get("notify_not_after", default_not_after),
        default_not_after,
    ),
    "blacklist_mount_points": blacklist_mount_points(),
}

Path(defaults_path).write_text(
    json.dumps(defaults, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

existing_default() {
  local key="$1"
  "${PYTHON_CMD[@]}" - "${tmp_existing_config}" "${key}" <<'PY'
import json
import sys
from pathlib import Path

defaults_path, key = sys.argv[1:]
value = json.loads(Path(defaults_path).read_text(encoding="utf-8")).get(key, "")
if isinstance(value, list):
    print(" ".join(str(item) for item in value))
elif value is not None:
    print(str(value))
PY
}

if [[ -f "${CONFIG_FILE}" ]]; then
  if load_existing_config_defaults; then
    existing_config_present=1
    echo "Loaded defaults from ${CONFIG_FILE}."
  else
    echo "Could not load defaults from ${CONFIG_FILE}; using installer defaults." >&2
    printf '{}\n' >"${tmp_existing_config}"
  fi
fi

prompt_required() {
  local prompt="$1"
  local default="${2:-}"
  local value=""
  while [[ -z "${value}" ]]; do
    if [[ -n "${default}" ]]; then
      read -r -p "${prompt} [${default}]: " value
      value="${value:-${default}}"
    else
      read -r -p "${prompt}: " value
    fi
  done
  printf '%s' "${value}"
}

prompt_optional_secret() {
  local prompt="$1"
  local default="$2"
  local value=""

  if [[ -n "${default}" ]]; then
    read -r -p "${prompt} [keep existing, '-' to clear]: " value
    if [[ -z "${value}" ]]; then
      printf '%s' "${default}"
    elif [[ "${value}" == "-" ]]; then
      printf ''
    else
      printf '%s' "${value}"
    fi
  else
    read -r -p "${prompt} [empty]: " value
    printf '%s' "${value}"
  fi
}

prompt_time() {
  local prompt="$1"
  local default="$2"
  local value=""
  local hours=""
  local minutes=""

  while true; do
    read -r -p "${prompt} [${default}]: " value
    value="${value:-${default}}"
    if [[ "${value}" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]]; then
      hours="${BASH_REMATCH[1]}"
      minutes="${BASH_REMATCH[2]}"
      printf '%02d:%02d' "$((10#${hours}))" "$((10#${minutes}))"
      return
    fi
    echo "Please enter time as HH:MM, from 00:00 to 23:59." >&2
  done
}

prompt_threshold() {
  local prompt="$1"
  local default="$2"
  local value=""

  while true; do
    read -r -p "${prompt} [${default}]: " value
    value="${value:-${default}}"
    if "${PYTHON_CMD[@]}" - "${value}" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except ValueError:
    raise SystemExit(1)

if not math.isfinite(value) or value < 0 or value > 100:
    raise SystemExit(1)
PY
    then
      printf '%s' "${value}"
      return
    fi
    echo "Please enter a number from 0 to 100." >&2
  done
}

echo "ntfy base URL example: https://ntfy-base-server.ru"
default_ntfy_base_url="$(existing_default "ntfy_base_url")"
ntfy_base_url="$(prompt_required "ntfy base URL" "${default_ntfy_base_url}")"
ntfy_base_url="${ntfy_base_url%/}"

while [[ ! "${ntfy_base_url}" =~ ^https?:// ]]; do
  echo "Please enter a full URL starting with http:// or https://"
  ntfy_base_url="$(prompt_required "ntfy base URL")"
  ntfy_base_url="${ntfy_base_url%/}"
done

default_ntfy_topic="$(existing_default "ntfy_topic")"
ntfy_topic="$(prompt_required "ntfy topic" "${default_ntfy_topic}")"
ntfy_topic="${ntfy_topic#/}"
ntfy_topic="${ntfy_topic%/}"

echo 'Optional bearer token. Example: curl -H "Authorization: Bearer 78c5506d0740a58.........." -d "<Текст сообщения>" https://ntfy-base-server.ru/<topic>'
default_ntfy_bearer_token="$(existing_default "ntfy_bearer_token")"
ntfy_bearer_token="$(prompt_optional_secret "ntfy bearer token" "${default_ntfy_bearer_token}")"

host_machine_name="$(hostname -f 2>/dev/null || hostname)"
default_machine_name="$(existing_default "machine_name")"
default_machine_name="${default_machine_name:-${host_machine_name}}"
read -r -p "Machine name [${default_machine_name}]: " machine_name
machine_name="${machine_name:-${default_machine_name}}"

default_threshold_free_percent="$(existing_default "threshold_free_percent")"
default_notify_not_before="$(existing_default "notify_not_before")"
default_notify_not_after="$(existing_default "notify_not_after")"

threshold_free_percent="$(prompt_threshold "Alert when free space is below percent" "${default_threshold_free_percent:-${DEFAULT_THRESHOLD_FREE_PERCENT}}")"
notify_not_before="$(prompt_time "Do not notify before local time" "${default_notify_not_before:-${DEFAULT_NOTIFY_NOT_BEFORE}}")"
notify_not_after="$(prompt_time "Do not notify after local time" "${default_notify_not_after:-${DEFAULT_NOTIFY_NOT_AFTER}}")"

"${PYTHON_CMD[@]}" - "${SOURCE_SCRIPT}" "${tmp_candidates}" "${machine_name}" <<'PY'
import importlib.util
import json
from pathlib import Path
import sys

source_script, candidates_path, machine_name = sys.argv[1:]
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("free_space_alarmer_ntfy", source_script)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

candidates = []
for index, usage in enumerate(module.collect_usages(), start=1):
    mount_point = usage.mount.mount_point
    candidates.append(
        {
            "number": index,
            "source": usage.mount.source,
            "mount_point": mount_point,
            "message": module.format_message(usage, machine_name),
            "default_blacklist": "/boot" in mount_point,
        }
    )

Path(candidates_path).write_text(
    json.dumps(candidates, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

candidate_count="$("${PYTHON_CMD[@]}" - "${tmp_candidates}" <<'PY'
import json
import sys
from pathlib import Path

print(len(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))))
PY
)"

echo
if [[ "${candidate_count}" -eq 0 ]]; then
  echo "No suitable local disks found for test messages."
  if [[ "${existing_config_present}" -eq 1 ]]; then
    "${PYTHON_CMD[@]}" - "${tmp_existing_config}" "${tmp_blacklist}" <<'PY'
import json
from pathlib import Path
import sys

defaults_path, blacklist_path = sys.argv[1:]
defaults = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
mount_points = defaults.get("blacklist_mount_points", [])
Path(blacklist_path).write_text(
    json.dumps(mount_points, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
  else
    printf '[]\n' >"${tmp_blacklist}"
  fi
else
  echo "Current test messages before blacklist:"
  "${PYTHON_CMD[@]}" - "${tmp_candidates}" <<'PY'
import json
import sys
from pathlib import Path

for candidate in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
    print(f"{candidate['number']}. {candidate['message']}")
PY

  if [[ "${existing_config_present}" -eq 1 ]]; then
    default_blacklist_numbers="$("${PYTHON_CMD[@]}" - "${tmp_candidates}" "${tmp_existing_config}" <<'PY'
import json
import sys
from pathlib import Path

candidates_path, defaults_path = sys.argv[1:]
candidates = json.loads(Path(candidates_path).read_text(encoding="utf-8"))
defaults = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
mount_points = set(defaults.get("blacklist_mount_points", []))
print(" ".join(str(candidate["number"]) for candidate in candidates if candidate["mount_point"] in mount_points))
PY
    )"
    existing_blacklist_mount_points="$(existing_default "blacklist_mount_points")"
    if [[ -n "${default_blacklist_numbers}" ]]; then
      default_blacklist_label="${default_blacklist_numbers}"
    elif [[ -n "${existing_blacklist_mount_points}" ]]; then
      default_blacklist_label="keep existing"
    else
      default_blacklist_label="empty"
    fi
  else
    default_blacklist_numbers="$("${PYTHON_CMD[@]}" - "${tmp_candidates}" <<'PY'
import json
import sys
from pathlib import Path

candidates = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(" ".join(str(candidate["number"]) for candidate in candidates if candidate["default_blacklist"]))
PY
    )"
    default_blacklist_label="${default_blacklist_numbers:-empty}"
  fi

  blacklist_defaulted=0
  read -r -p "Blacklist numbers [${default_blacklist_label}]: " blacklist_numbers
  if [[ -z "${blacklist_numbers}" ]]; then
    blacklist_numbers="${default_blacklist_numbers}"
    blacklist_defaulted=1
  fi

  "${PYTHON_CMD[@]}" - "${tmp_candidates}" "${tmp_blacklist}" "${blacklist_numbers}" "${tmp_existing_config}" "${blacklist_defaulted}" <<'PY'
import json
from pathlib import Path
import sys

candidates_path, blacklist_path, raw_numbers, defaults_path, defaulted = sys.argv[1:]
candidates = json.loads(Path(candidates_path).read_text(encoding="utf-8"))
candidates_by_number = {candidate["number"]: candidate for candidate in candidates}
mount_points = []

if defaulted == "1":
    defaults = json.loads(Path(defaults_path).read_text(encoding="utf-8"))
    for value in defaults.get("blacklist_mount_points", []):
        mount_point = str(value).strip()
        if mount_point and mount_point not in mount_points:
            mount_points.append(mount_point)

for raw_number in raw_numbers.split():
    if not raw_number.isdigit():
        raise SystemExit(f"Invalid blacklist number: {raw_number}")
    number = int(raw_number)
    if number not in candidates_by_number:
        raise SystemExit(f"Blacklist number out of range: {number}")
    mount_point = candidates_by_number[number]["mount_point"]
    if mount_point not in mount_points:
        mount_points.append(mount_point)

Path(blacklist_path).write_text(
    json.dumps(mount_points, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

if mount_points:
    print("Blacklisted mount points: " + ", ".join(mount_points))
else:
    print("Blacklisted mount points: none")
PY
fi

"${PYTHON_CMD[@]}" - "${tmp_config}" "${ntfy_base_url}" "${ntfy_topic}" "${ntfy_bearer_token}" "${machine_name}" "${threshold_free_percent}" "${notify_not_before}" "${notify_not_after}" "${tmp_blacklist}" <<'PY'
import json
from pathlib import Path
import sys

(
    config_path,
    ntfy_base_url,
    ntfy_topic,
    ntfy_bearer_token,
    machine_name,
    threshold_free_percent,
    notify_not_before,
    notify_not_after,
    blacklist_path,
) = sys.argv[1:]
token = ntfy_bearer_token.strip()
config = {
    "ntfy_base_url": ntfy_base_url.strip(),
    "ntfy_topic": ntfy_topic.strip(),
    "ntfy_bearer_token": token or None,
    "machine_name": machine_name.strip(),
    "threshold_free_percent": float(threshold_free_percent),
    "notify_not_before": notify_not_before.strip(),
    "notify_not_after": notify_not_after.strip(),
    "blacklist": {
        "mount_points": json.loads(Path(blacklist_path).read_text(encoding="utf-8")),
    },
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
echo "Send test messages for selected disks after blacklist: sudo ${APP_NAME} --config ${CONFIG_FILE} --test"
