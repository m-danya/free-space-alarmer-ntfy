#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/etc/free-space-alarmer-ntfy/config.json"
DEFAULT_THRESHOLD_FREE_PERCENT = 10.0
DEFAULT_THRESHOLD_FREE_PERCENT_TEXT = "10"
DEFAULT_NOTIFY_NOT_BEFORE = "10:00"
DEFAULT_NOTIFY_NOT_AFTER = "20:00"
DEFAULT_TIMER_INTERVAL_HOURS = 1
DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_SSH_COMMAND_TIMEOUT_SECONDS = 60
GIB = 1024**3
TIME_VALUE_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

PSEUDO_FS_TYPES = {
    "autofs",
    "bdev",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nsfs",
    "overlay",
    "pipefs",
    "proc",
    "pstore",
    "ramfs",
    "rootfs",
    "rpc_pipefs",
    "securityfs",
    "selinuxfs",
    "squashfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}

NETWORK_FS_TYPES = {
    "9p",
    "afs",
    "ceph",
    "cifs",
    "davfs",
    "fuse.ceph",
    "fuse.curlftpfs",
    "fuse.davfs",
    "fuse.glusterfs",
    "fuse.rclone",
    "fuse.s3fs",
    "fuse.sshfs",
    "glusterfs",
    "ncpfs",
    "nfs",
    "nfs4",
    "smb2",
    "smb3",
    "sshfs",
    "virtiofs",
}

LOCAL_FS_TYPES = {
    "apfs",
    "bcachefs",
    "btrfs",
    "exfat",
    "ext2",
    "ext3",
    "ext4",
    "f2fs",
    "fat",
    "fuseblk",
    "hfs",
    "hfsplus",
    "jfs",
    "msdos",
    "nilfs2",
    "ntfs",
    "ntfs3",
    "reiserfs",
    "ufs",
    "vfat",
    "xfs",
    "zfs",
}

EXCLUDED_SOURCE_PREFIXES = (
    "/dev/fd",
    "/dev/loop",
    "/dev/ram",
    "/dev/zram",
)

EXCLUDED_MOUNT_POINTS = {
    "/dev",
    "/proc",
    "/run",
    "/sys",
    "/tmp",
    "/var/tmp",
}


@dataclass(frozen=True)
class MountEntry:
    device_id: str
    root: str
    mount_point: str
    fs_type: str
    source: str
    options: frozenset[str]


@dataclass(frozen=True)
class DiskUsage:
    mount: MountEntry
    total_bytes: int
    available_bytes: int

    @property
    def free_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return (self.available_bytes / self.total_bytes) * 100

    @property
    def available_gb(self) -> float:
        return self.available_bytes / GIB


@dataclass(frozen=True)
class TargetDiskUsage:
    machine_name: str
    usage: DiskUsage


def decode_mountinfo_field(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(path: Path = Path("/proc/self/mountinfo")) -> list[MountEntry]:
    entries: list[MountEntry] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            left, right = line.split(" - ", 1)
            left_fields = left.split()
            right_fields = right.split()
            if len(left_fields) < 6 or len(right_fields) < 3:
                raise ValueError("not enough fields")
        except ValueError as exc:
            print(f"Skipping malformed mountinfo line {line_number}: {exc}", file=sys.stderr)
            continue

        entries.append(
            MountEntry(
                device_id=left_fields[2],
                root=decode_mountinfo_field(left_fields[3]),
                mount_point=decode_mountinfo_field(left_fields[4]),
                options=frozenset(left_fields[5].split(",")),
                fs_type=decode_mountinfo_field(right_fields[0]),
                source=decode_mountinfo_field(right_fields[1]),
            )
        )
    return entries


def is_excluded_mount_point(mount_point: str) -> bool:
    if mount_point in EXCLUDED_MOUNT_POINTS:
        return True
    return any(mount_point.startswith(f"{prefix}/") for prefix in EXCLUDED_MOUNT_POINTS)


def is_network_source(source: str) -> bool:
    if source.startswith("//"):
        return True
    return bool(re.match(r"^[A-Za-z0-9_.-]+:", source))


def is_local_real_mount(entry: MountEntry) -> bool:
    fs_type = entry.fs_type.lower()
    source = entry.source

    if fs_type in PSEUDO_FS_TYPES or fs_type in NETWORK_FS_TYPES:
        return False
    if fs_type.startswith("fuse.") and fs_type != "fuseblk":
        return False
    if source in {"none", "tmpfs", "devtmpfs", "udev", "systemd-1"}:
        return False
    if is_network_source(source):
        return False
    if source.startswith(EXCLUDED_SOURCE_PREFIXES):
        return False
    if is_excluded_mount_point(entry.mount_point):
        return False
    if "ro" in entry.options:
        return False

    if source.startswith("/dev/"):
        return True
    return fs_type in LOCAL_FS_TYPES


def mount_priority(entry: MountEntry) -> tuple[int, int, str]:
    if entry.mount_point == "/":
        return (0, 0, entry.mount_point)
    important_prefixes = ("/home", "/var", "/srv", "/opt", "/space", "/mnt", "/media")
    if entry.mount_point == "/boot":
        return (1, 0, entry.mount_point)
    if entry.mount_point == "/boot/efi":
        return (2, 0, entry.mount_point)
    if any(entry.mount_point == prefix or entry.mount_point.startswith(f"{prefix}/") for prefix in important_prefixes):
        return (3, entry.mount_point.count("/"), entry.mount_point)
    return (4, entry.mount_point.count("/"), entry.mount_point)


def selected_mounts() -> list[MountEntry]:
    selected = [entry for entry in parse_mountinfo() if is_local_real_mount(entry)]
    selected.sort(key=mount_priority)

    deduplicated: dict[str, MountEntry] = {}
    for entry in selected:
        key = entry.device_id if entry.device_id != "0:0" else f"{entry.fs_type}:{entry.source}"
        deduplicated.setdefault(key, entry)

    return sorted(deduplicated.values(), key=mount_priority)


def get_disk_usage(mount: MountEntry) -> DiskUsage | None:
    try:
        stat = os.statvfs(mount.mount_point)
    except OSError as exc:
        print(f"Cannot stat {mount.mount_point}: {exc}", file=sys.stderr)
        return None

    fragment_size = stat.f_frsize or stat.f_bsize
    total_bytes = stat.f_blocks * fragment_size
    available_bytes = stat.f_bavail * fragment_size
    if total_bytes <= 0:
        return None
    return DiskUsage(mount=mount, total_bytes=total_bytes, available_bytes=available_bytes)


def collect_all_usages() -> list[DiskUsage]:
    usages: list[DiskUsage] = []
    for mount in selected_mounts():
        usage = get_disk_usage(mount)
        if usage is not None:
            usages.append(usage)
    return usages


def mount_to_json_value(mount: MountEntry) -> dict[str, Any]:
    return {
        "device_id": mount.device_id,
        "root": mount.root,
        "mount_point": mount.mount_point,
        "fs_type": mount.fs_type,
        "source": mount.source,
        "options": sorted(mount.options),
    }


def usage_to_json_value(usage: DiskUsage) -> dict[str, Any]:
    return {
        "mount": mount_to_json_value(usage.mount),
        "total_bytes": usage.total_bytes,
        "available_bytes": usage.available_bytes,
    }


def mount_from_json_value(value: dict[str, Any]) -> MountEntry:
    options = value.get("options", [])
    if not isinstance(options, list):
        options = []
    return MountEntry(
        device_id=str(value.get("device_id", "")),
        root=str(value.get("root", "")),
        mount_point=str(value.get("mount_point", "")),
        fs_type=str(value.get("fs_type", "")),
        source=str(value.get("source", "")),
        options=frozenset(str(option) for option in options),
    )


def usage_from_json_value(value: dict[str, Any]) -> DiskUsage:
    mount = value.get("mount", {})
    if not isinstance(mount, dict):
        mount = {}
    return DiskUsage(
        mount=mount_from_json_value(mount),
        total_bytes=int(value.get("total_bytes", 0)),
        available_bytes=int(value.get("available_bytes", 0)),
    )


def print_probe_json() -> int:
    json.dump(
        [usage_to_json_value(usage) for usage in collect_all_usages()],
        sys.stdout,
        ensure_ascii=False,
    )
    sys.stdout.write("\n")
    return 0


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}") from None
    except OSError as exc:
        raise SystemExit(f"Cannot read config file {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from None

    config.setdefault("machine_name", socket.gethostname())
    ntfy_base_url = normalize_http_url(config.get("ntfy_base_url"), "ntfy_base_url", strip_trailing_slash=True)
    ntfy_topic = normalize_ntfy_topic(config.get("ntfy_topic"))
    if ntfy_base_url and not ntfy_topic:
        raise SystemExit("Invalid config: ntfy_topic is required when ntfy_base_url is set")

    config["ntfy_base_url"] = ntfy_base_url or None
    config["ntfy_topic"] = ntfy_topic or None
    config["ntfy_bearer_token"] = string_config_value(config.get("ntfy_bearer_token")) or None
    config["mattermost_webhook_url"] = normalize_http_url(
        config.get("mattermost_webhook_url"),
        "mattermost_webhook_url",
    ) or None
    config["threshold_free_percent"] = parse_threshold_free_percent(
        config.get("threshold_free_percent", DEFAULT_THRESHOLD_FREE_PERCENT)
    )
    config["notify_not_before"] = normalize_time_value(
        config.get("notify_not_before", DEFAULT_NOTIFY_NOT_BEFORE),
        "notify_not_before",
    )
    config["notify_not_after"] = normalize_time_value(
        config.get("notify_not_after", DEFAULT_NOTIFY_NOT_AFTER),
        "notify_not_after",
    )
    config["ssh"] = normalize_ssh_settings(config.get("ssh"))
    config.setdefault("blacklist", {"mount_points": []})
    return config


def string_config_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_http_url(value: Any, field_name: str, *, strip_trailing_slash: bool = False) -> str:
    text = string_config_value(value)
    if not text or text == "-":
        return ""

    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"Invalid config: {field_name} must be a full http:// or https:// URL")

    if strip_trailing_slash:
        return text.rstrip("/")
    return text


def normalize_ntfy_topic(value: Any) -> str:
    return string_config_value(value).strip("/")


def parse_threshold_free_percent(value: Any) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        raise SystemExit("Invalid config: threshold_free_percent must be a number") from None

    if not math.isfinite(threshold) or threshold < 0 or threshold > 100:
        raise SystemExit("Invalid config: threshold_free_percent must be between 0 and 100")
    return threshold


def parse_time_value(value: Any, field_name: str) -> int:
    text = str(value).strip()
    match = TIME_VALUE_RE.fullmatch(text)
    if not match:
        raise SystemExit(f"Invalid config: {field_name} must be time in HH:MM format")
    return (int(match.group(1)) * 60) + int(match.group(2))


def normalize_time_value(value: Any, field_name: str) -> str:
    minutes = parse_time_value(value, field_name)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_positive_int_config(value: Any, field_name: str, default: int) -> int:
    text = string_config_value(value)
    if not text:
        return default
    if re.fullmatch(r"[1-9][0-9]*", text):
        return int(text)
    raise SystemExit(f"Invalid config: {field_name} must be a positive integer")


def split_ssh_config_line(line: str) -> list[str]:
    try:
        tokens = shlex.split(line, comments=True, posix=True)
    except ValueError:
        tokens = line.split()

    if not tokens:
        return []

    key, separator, value = tokens[0].partition("=")
    if separator:
        return [key, value, *tokens[1:]] if value else [key, *tokens[1:]]
    return tokens


def expand_ssh_path(value: str, home_dir: Path) -> Path:
    expanded = os.path.expandvars(value)
    if expanded == "~":
        return home_dir
    if expanded.startswith("~/"):
        return home_dir / expanded[2:]
    return Path(expanded).expanduser()


def resolve_ssh_include(pattern: str, base_dir: Path, home_dir: Path) -> list[Path]:
    expanded = os.path.expandvars(pattern)
    include_path = expand_ssh_path(expanded, home_dir)

    if not include_path.is_absolute():
        include_path = base_dir / include_path

    return [Path(match) for match in sorted(glob.glob(str(include_path)))]


def is_concrete_ssh_host(pattern: str) -> bool:
    if not pattern or pattern.startswith("!") or pattern.startswith("-"):
        return False
    return not any(char in pattern for char in "*?[]")


def read_ssh_config_hosts(path: Path, visited: set[Path], home_dir: Path) -> list[str]:
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path

    if resolved_path in visited or not path.is_file():
        return []
    visited.add(resolved_path)

    hosts: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        tokens = split_ssh_config_line(line)
        if not tokens:
            continue

        keyword = tokens[0].lower()
        values = tokens[1:]
        if keyword == "include":
            for include_pattern in values:
                for include_path in resolve_ssh_include(include_pattern, path.parent, home_dir):
                    hosts.extend(read_ssh_config_hosts(include_path, visited, home_dir))
        elif keyword == "host":
            hosts.extend(pattern for pattern in values if is_concrete_ssh_host(pattern))

    return hosts


def ssh_config_hosts(config_file: str | None, home_dir: str | None = None) -> list[str]:
    ssh_home = Path(home_dir).expanduser() if home_dir else Path.home()
    config_path = expand_ssh_path(config_file, ssh_home) if config_file else ssh_home / ".ssh/config"
    hosts: list[str] = []
    for host in read_ssh_config_hosts(config_path, set(), ssh_home):
        if host not in hosts:
            hosts.append(host)
    return hosts


def normalize_ssh_settings(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SystemExit("Invalid config: ssh must be an object")

    config_file = string_config_value(value.get("config_file")) or str(Path.home() / ".ssh/config")
    enabled = bool(value.get("enabled", False))
    return {
        "enabled": enabled,
        "config_file": config_file,
        "connect_timeout_seconds": parse_positive_int_config(
            value.get("connect_timeout_seconds"),
            "ssh.connect_timeout_seconds",
            DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
        ),
        "command_timeout_seconds": parse_positive_int_config(
            value.get("command_timeout_seconds"),
            "ssh.command_timeout_seconds",
            DEFAULT_SSH_COMMAND_TIMEOUT_SECONDS,
        ),
        "hosts": ssh_config_hosts(config_file) if enabled else [],
    }


def blacklisted_mount_points(config: dict[str, Any] | None) -> set[str]:
    if not config:
        return set()

    blacklist = config.get("blacklist", {})
    if isinstance(blacklist, dict):
        values = blacklist.get("mount_points", [])
    else:
        values = []

    if not isinstance(values, list):
        raise SystemExit("Invalid config: blacklist.mount_points must be a list")

    return {str(value) for value in values if str(value).strip()}


def is_blacklisted(mount: MountEntry, config: dict[str, Any] | None) -> bool:
    return mount.mount_point in blacklisted_mount_points(config)


def format_number(value: float, digits: int = 1) -> str:
    rounded = round(value, digits)
    if math.isclose(rounded, round(rounded)):
        return str(int(round(rounded)))
    return f"{rounded:.{digits}f}"


def disk_label(mount: MountEntry) -> str:
    return f"{mount.source} ({mount.mount_point})"


def format_message(usage: DiskUsage, machine_name: str) -> str:
    return (
        f"🚨 На диске {disk_label(usage.mount)} на машине {machine_name} осталось "
        f"{format_number(usage.free_percent)}% свободного места "
        f"({format_number(usage.available_gb)} GB)"
    )


def ntfy_url(base_url: str, topic: str) -> str:
    normalized_base = base_url.rstrip("/")
    normalized_topic = urllib.parse.quote(topic.strip("/"), safe="")
    return f"{normalized_base}/{normalized_topic}"


def send_ntfy_message(message: str, config: dict[str, Any]) -> None:
    request = urllib.request.Request(
        ntfy_url(str(config["ntfy_base_url"]), str(config["ntfy_topic"])),
        data=message.encode("utf-8"),
        method="POST",
    )
    token = str(config.get("ntfy_bearer_token") or "").strip()
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ntfy returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach ntfy server: {exc.reason}") from exc


def send_mattermost_message(message: str, config: dict[str, Any]) -> None:
    request = urllib.request.Request(
        str(config["mattermost_webhook_url"]),
        data=json.dumps({"text": message}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mattermost returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach Mattermost webhook: {exc.reason}") from exc


def has_notification_channel(config: dict[str, Any]) -> bool:
    return bool(
        (config.get("ntfy_base_url") and config.get("ntfy_topic"))
        or config.get("mattermost_webhook_url")
    )


def send_notification_message(message: str, config: dict[str, Any]) -> None:
    errors = []

    if config.get("ntfy_base_url") and config.get("ntfy_topic"):
        try:
            send_ntfy_message(message, config)
        except RuntimeError as exc:
            errors.append(str(exc))

    if config.get("mattermost_webhook_url"):
        try:
            send_mattermost_message(message, config)
        except RuntimeError as exc:
            errors.append(str(exc))

    if errors:
        raise RuntimeError("Notification send failed: " + "; ".join(errors))


def filter_usages(usages: list[DiskUsage], config: dict[str, Any] | None = None) -> list[DiskUsage]:
    return [usage for usage in usages if not is_blacklisted(usage.mount, config)]


def collect_usages(config: dict[str, Any] | None = None) -> list[DiskUsage]:
    return filter_usages(collect_all_usages(), config)


def script_source_bytes() -> bytes:
    try:
        return Path(__file__).read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Cannot read script source for SSH probe: {exc}") from exc


def ssh_command_for_host(host: str, ssh_config: dict[str, Any]) -> list[str]:
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ssh_config['connect_timeout_seconds']}",
    ]
    config_file = string_config_value(ssh_config.get("config_file"))
    if config_file:
        command.extend(["-F", config_file])
    command.extend([host, "python3", "-", "--probe-json"])
    return command


def collect_ssh_usages(host: str, config: dict[str, Any], source: bytes) -> list[DiskUsage]:
    ssh_config = config["ssh"]
    try:
        process = subprocess.run(
            ssh_command_for_host(host, ssh_config),
            input=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=int(ssh_config["command_timeout_seconds"]),
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"Cannot collect SSH disk usages from {host}: command timed out.", file=sys.stderr)
        return []
    except OSError as exc:
        print(f"Cannot collect SSH disk usages from {host}: {exc}", file=sys.stderr)
        return []

    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        details = stderr or stdout.strip() or "no output"
        print(f"Cannot collect SSH disk usages from {host}: ssh exited {process.returncode}: {details}", file=sys.stderr)
        return []

    if stderr:
        print(f"SSH disk probe stderr from {host}: {stderr}", file=sys.stderr)

    try:
        values = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"Cannot collect SSH disk usages from {host}: invalid probe JSON: {exc}", file=sys.stderr)
        return []

    if not isinstance(values, list):
        print(f"Cannot collect SSH disk usages from {host}: probe JSON is not a list.", file=sys.stderr)
        return []

    usages: list[DiskUsage] = []
    for value in values:
        if isinstance(value, dict):
            usages.append(usage_from_json_value(value))
    return filter_usages(usages, config)


def ssh_hosts_from_config(config: dict[str, Any]) -> list[str]:
    ssh_config = config.get("ssh", {})
    if not isinstance(ssh_config, dict) or not ssh_config.get("enabled"):
        return []

    hosts = ssh_config.get("hosts", [])
    if not isinstance(hosts, list):
        return []
    return [str(host) for host in hosts if str(host).strip()]


def collect_target_usages(config: dict[str, Any]) -> list[TargetDiskUsage]:
    machine_name = str(config.get("machine_name") or socket.gethostname())
    target_usages = [TargetDiskUsage(machine_name, usage) for usage in collect_usages(config)]
    ssh_hosts = ssh_hosts_from_config(config)
    if not ssh_hosts:
        return target_usages

    if shutil.which("ssh") is None:
        print("Cannot collect SSH disk usages: ssh command not found.", file=sys.stderr)
        return target_usages

    try:
        source = script_source_bytes()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return target_usages

    for host in ssh_hosts:
        target_usages.extend(TargetDiskUsage(host, usage) for usage in collect_ssh_usages(host, config, source))
    return target_usages


def print_mounts(target_usages: list[TargetDiskUsage]) -> None:
    if not target_usages:
        print("No suitable disks found.")
        return

    for target_usage in target_usages:
        usage = target_usage.usage
        print(
            f"{target_usage.machine_name}: {disk_label(usage.mount)}: "
            f"{format_number(usage.free_percent)}% free, "
            f"{format_number(usage.available_gb)} GB available"
        )


def is_within_notification_window(config: dict[str, Any]) -> bool:
    not_before = parse_time_value(config.get("notify_not_before", DEFAULT_NOTIFY_NOT_BEFORE), "notify_not_before")
    not_after = parse_time_value(config.get("notify_not_after", DEFAULT_NOTIFY_NOT_AFTER), "notify_not_after")
    now = datetime.now()
    current_minutes = (now.hour * 60) + now.minute

    if not_before <= not_after:
        return not_before <= current_minutes <= not_after
    return current_minutes >= not_before or current_minutes <= not_after


def run(config: dict[str, Any], *, test_mode: bool, dry_run: bool) -> int:
    threshold = float(config.get("threshold_free_percent", DEFAULT_THRESHOLD_FREE_PERCENT))

    if not test_mode and not is_within_notification_window(config):
        if dry_run:
            print(
                "Outside notification window "
                f"{config.get('notify_not_before', DEFAULT_NOTIFY_NOT_BEFORE)}-"
                f"{config.get('notify_not_after', DEFAULT_NOTIFY_NOT_AFTER)}; no alerts to send."
            )
        return 0

    target_usages = collect_target_usages(config)

    if test_mode:
        messages = [format_message(target_usage.usage, target_usage.machine_name) for target_usage in target_usages]
    else:
        messages = [
            format_message(target_usage.usage, target_usage.machine_name)
            for target_usage in target_usages
            if target_usage.usage.free_percent < threshold
        ]

    if messages and not dry_run and not has_notification_channel(config):
        print("No notification channels configured; no messages sent.", file=sys.stderr)
        for message in messages:
            print(message)
        return 0

    for message in messages:
        if dry_run:
            print(message)
            continue
        send_notification_message(message, config)
        print(message)

    if not messages and dry_run:
        print("No alerts to send.")

    return 0


# Installer helpers keep install.sh as shell orchestration instead of embedding Python snippets.
def installer_url_default(value: Any, *, strip_trailing_slash: bool = False) -> str:
    text = string_config_value(value)
    if strip_trailing_slash:
        text = text.rstrip("/")
    if re.fullmatch(r"https?://.+", text):
        return text
    return ""


def installer_threshold_default(value: Any) -> str:
    text = string_config_value(value)
    try:
        parse_threshold_free_percent(text)
    except SystemExit:
        return DEFAULT_THRESHOLD_FREE_PERCENT_TEXT
    return text


def installer_time_default(value: Any, fallback: str) -> str:
    try:
        return normalize_time_value(value, "installer default")
    except SystemExit:
        return fallback


def installer_timer_interval_default(value: Any) -> str:
    text = string_config_value(value)
    if re.fullmatch(r"[1-9][0-9]*", text):
        return text
    return str(DEFAULT_TIMER_INTERVAL_HOURS)


def installer_blacklist_mount_points(config: dict[str, Any]) -> list[str]:
    blacklist = config.get("blacklist", {})
    if not isinstance(blacklist, dict):
        return []

    values = blacklist.get("mount_points", [])
    if not isinstance(values, list):
        return []

    mount_points: list[str] = []
    for value in values:
        mount_point = string_config_value(value)
        if mount_point and mount_point not in mount_points:
            mount_points.append(mount_point)
    return mount_points


def installer_default_values() -> dict[str, Any]:
    return {
        "ntfy_base_url": "",
        "ntfy_topic": "",
        "ntfy_bearer_token": "",
        "mattermost_webhook_url": "",
        "machine_name": "",
        "threshold_free_percent": DEFAULT_THRESHOLD_FREE_PERCENT_TEXT,
        "notify_not_before": DEFAULT_NOTIFY_NOT_BEFORE,
        "notify_not_after": DEFAULT_NOTIFY_NOT_AFTER,
        "timer_interval_hours": str(DEFAULT_TIMER_INTERVAL_HOURS),
        "blacklist_mount_points": [],
        "ssh_enabled": False,
        "ssh_config_file": str(Path.home() / ".ssh/config"),
    }


def load_installer_defaults(config_path: str, label: str) -> tuple[dict[str, Any], bool]:
    defaults = installer_default_values()
    if not config_path:
        return defaults, False

    path = Path(config_path)
    if not path.is_file():
        return defaults, False

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("top-level JSON value is not an object")
    except (OSError, ValueError, json.JSONDecodeError):
        print(f"Could not load defaults from {label}; using installer defaults.", file=sys.stderr)
        return defaults, False

    ssh_settings = normalize_ssh_settings(config.get("ssh"))
    defaults.update(
        {
            "ntfy_base_url": installer_url_default(config.get("ntfy_base_url"), strip_trailing_slash=True),
            "ntfy_topic": normalize_ntfy_topic(config.get("ntfy_topic")),
            "ntfy_bearer_token": string_config_value(config.get("ntfy_bearer_token")),
            "mattermost_webhook_url": installer_url_default(config.get("mattermost_webhook_url")),
            "machine_name": string_config_value(config.get("machine_name")),
            "threshold_free_percent": installer_threshold_default(
                config.get("threshold_free_percent", DEFAULT_THRESHOLD_FREE_PERCENT_TEXT)
            ),
            "notify_not_before": installer_time_default(
                config.get("notify_not_before", DEFAULT_NOTIFY_NOT_BEFORE),
                DEFAULT_NOTIFY_NOT_BEFORE,
            ),
            "notify_not_after": installer_time_default(
                config.get("notify_not_after", DEFAULT_NOTIFY_NOT_AFTER),
                DEFAULT_NOTIFY_NOT_AFTER,
            ),
            "timer_interval_hours": installer_timer_interval_default(
                config.get("timer_interval_hours", DEFAULT_TIMER_INTERVAL_HOURS)
            ),
            "blacklist_mount_points": installer_blacklist_mount_points(config),
            "ssh_enabled": bool(ssh_settings["enabled"]),
            "ssh_config_file": string_config_value(ssh_settings["config_file"]),
        }
    )
    print(f"Loaded defaults from {label}.")
    return defaults, True


def prompt_required(prompt: str, default: str = "") -> str:
    while True:
        if default:
            value = input(f"{prompt} [{default}]: ") or default
        else:
            value = input(f"{prompt}: ")
        if value:
            return value


def prompt_optional_secret(prompt: str, default: str) -> str:
    if default:
        value = input(f"{prompt} [keep existing, '-' to clear]: ")
        if not value:
            return default
        if value == "-":
            return ""
        return value

    return input(f"{prompt} [empty]: ")


def prompt_url_or_disabled(prompt: str, default: str = "") -> str:
    while True:
        if default:
            value = input(f"{prompt} [{default}; '-' to disable]: ") or default
        else:
            value = input(f"{prompt} ('-' to disable): ")

        if value == "-":
            return ""
        if re.fullmatch(r"https?://.+", value):
            return value
        print("Please enter a full URL starting with http:// or https://, or '-' to disable.", file=sys.stderr)


def prompt_time(prompt: str, default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]: ") or default
        try:
            return normalize_time_value(value, prompt)
        except SystemExit:
            print("Please enter time as HH:MM, from 00:00 to 23:59.", file=sys.stderr)


def prompt_threshold(prompt: str, default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]: ") or default
        try:
            parse_threshold_free_percent(value)
        except SystemExit:
            print("Please enter a number from 0 to 100.", file=sys.stderr)
            continue
        return value


def prompt_positive_integer(prompt: str, default: str) -> str:
    while True:
        value = input(f"{prompt} [{default}]: ") or default
        if re.fullmatch(r"[1-9][0-9]*", value):
            return value
        print("Please enter a positive integer.", file=sys.stderr)


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "д", "да"}:
            return True
        if value in {"n", "no", "н", "нет"}:
            return False
        print("Please answer yes or no.", file=sys.stderr)


def prompt_ssh_enabled(config_file: str, home_dir: str | None, default: bool) -> bool:
    hosts = ssh_config_hosts(config_file, home_dir)
    if not hosts:
        print(f"No SSH hosts found in {config_file}.")
        return False

    print("SSH hosts from your config:")
    for host in hosts:
        print(f"- {host}")
    return prompt_yes_no("Check disks on SSH hosts from your config?", default)


def is_default_blacklist_mount_point(mount_point: str) -> bool:
    return (
        mount_point == "/boot"
        or mount_point.startswith("/boot/")
        or mount_point == "/dump"
        or mount_point.startswith("/dump/")
    )


def installer_disk_candidates(target_usages: list[TargetDiskUsage]) -> list[dict[str, Any]]:
    candidates = []
    for index, target_usage in enumerate(target_usages, start=1):
        usage = target_usage.usage
        mount_point = usage.mount.mount_point
        candidates.append(
            {
                "number": index,
                "mount_point": mount_point,
                "message": format_message(usage, target_usage.machine_name),
                "default_blacklist": is_default_blacklist_mount_point(mount_point),
            }
        )
    return candidates


def installer_target_usages(machine_name: str, ssh_enabled: bool, ssh_config_file: str) -> list[TargetDiskUsage]:
    config = {
        "machine_name": machine_name,
        "ssh": normalize_ssh_settings(
            {
                "enabled": ssh_enabled,
                "config_file": ssh_config_file,
                "connect_timeout_seconds": DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
                "command_timeout_seconds": DEFAULT_SSH_COMMAND_TIMEOUT_SECONDS,
            }
        ),
        "blacklist": {
            "mount_points": [],
        },
    }
    return collect_target_usages(config)


def append_unique_mount_point(mount_points: list[str], mount_point: str) -> None:
    if mount_point and mount_point not in mount_points:
        mount_points.append(mount_point)


def select_installer_blacklist(
    candidates: list[dict[str, Any]],
    defaults: dict[str, Any],
    existing_config_present: bool,
) -> list[str]:
    if not candidates:
        print("No suitable disks found for test messages.")
        if existing_config_present:
            return list(defaults["blacklist_mount_points"])
        return []

    print("Current test messages before blacklist:")
    for candidate in candidates:
        print(f"{candidate['number']}. {candidate['message']}")

    if existing_config_present:
        existing_mount_points = defaults["blacklist_mount_points"]
        default_numbers = [
            str(candidate["number"])
            for candidate in candidates
            if candidate["mount_point"] in existing_mount_points
        ]
        if default_numbers:
            default_label = " ".join(default_numbers)
        elif existing_mount_points:
            default_label = "keep existing"
        else:
            default_label = "empty"
    else:
        existing_mount_points = []
        default_numbers = [
            str(candidate["number"])
            for candidate in candidates
            if candidate["default_blacklist"]
        ]
        default_label = " ".join(default_numbers) if default_numbers else "empty"

    candidates_by_number = {candidate["number"]: candidate for candidate in candidates}
    while True:
        raw_numbers = input(f"Blacklist numbers [{default_label}]: ")
        defaulted = not raw_numbers
        if defaulted:
            raw_numbers = " ".join(default_numbers)

        mount_points: list[str] = []
        if defaulted:
            for mount_point in existing_mount_points:
                append_unique_mount_point(mount_points, mount_point)

        try:
            for raw_number in raw_numbers.split():
                if not raw_number.isdigit():
                    raise ValueError(f"Invalid blacklist number: {raw_number}")
                number = int(raw_number)
                candidate = candidates_by_number.get(number)
                if candidate is None:
                    raise ValueError(f"Blacklist number out of range: {number}")
                append_unique_mount_point(mount_points, str(candidate["mount_point"]))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            continue

        if mount_points:
            print("Blacklisted mount points: " + ", ".join(mount_points))
        else:
            print("Blacklisted mount points: none")
        return mount_points


def configure_install(args: argparse.Namespace) -> int:
    if not args.output_config or not args.output_timer_interval:
        raise SystemExit("--output-config and --output-timer-interval are required with --configure-install")

    existing_config_path = args.existing_config if args.existing_config is not None else args.config
    existing_config_label = args.existing_config_label or existing_config_path
    defaults, existing_config_present = load_installer_defaults(existing_config_path, existing_config_label)

    print("Notification channels are optional. Enter '-' instead of the URL to disable ntfy or Mattermost.")
    print("ntfy base URL example: https://ntfy-base-server.ru")
    ntfy_base_url = prompt_url_or_disabled("ntfy base URL", defaults["ntfy_base_url"]).rstrip("/")

    ntfy_topic = ""
    ntfy_bearer_token = ""
    if ntfy_base_url:
        ntfy_topic = prompt_required("ntfy topic", defaults["ntfy_topic"]).strip("/")
        print(
            'Optional bearer token. Example: curl -H "Authorization: Bearer '
            '78c5506d0740a58.........." -d "<Текст сообщения>" https://ntfy-base-server.ru/<topic>'
        )
        ntfy_bearer_token = prompt_optional_secret("ntfy bearer token", defaults["ntfy_bearer_token"])
    else:
        print("ntfy disabled.")

    mattermost_default = defaults["mattermost_webhook_url"] or "-"
    mattermost_webhook_url = prompt_url_or_disabled("Mattermost incoming webhook URL (optional)", mattermost_default)
    if not mattermost_webhook_url:
        print("Mattermost disabled.")

    if not ntfy_base_url and not mattermost_webhook_url:
        print("No notification channels enabled; generated alerts will be logged but not sent.")

    host_machine_name = socket.getfqdn() or socket.gethostname()
    default_machine_name = defaults["machine_name"] or host_machine_name
    if default_machine_name:
        machine_name = input(f"Machine name [{default_machine_name}]: ") or default_machine_name
    else:
        machine_name = input("Machine name: ")

    threshold_free_percent = prompt_threshold(
        "Alert when free space is below percent",
        defaults["threshold_free_percent"],
    )
    notify_not_before = prompt_time("Do not notify before local time", defaults["notify_not_before"])
    notify_not_after = prompt_time("Do not notify after local time", defaults["notify_not_after"])
    timer_interval_hours = prompt_positive_integer("Run check every N hours", defaults["timer_interval_hours"])
    ssh_config_file = args.ssh_config or defaults["ssh_config_file"]
    ssh_enabled = prompt_ssh_enabled(ssh_config_file, args.ssh_home, bool(defaults["ssh_enabled"]))

    print()
    if ssh_enabled:
        print("Collecting disk list from local and SSH hosts...")
    blacklist_mount_points = select_installer_blacklist(
        installer_disk_candidates(installer_target_usages(machine_name, ssh_enabled, ssh_config_file)),
        defaults,
        existing_config_present,
    )

    token = ntfy_bearer_token.strip()
    config = {
        "ntfy_base_url": ntfy_base_url.strip() or None,
        "ntfy_topic": ntfy_topic.strip() or None,
        "ntfy_bearer_token": token or None,
        "mattermost_webhook_url": mattermost_webhook_url.strip() or None,
        "machine_name": machine_name.strip(),
        "threshold_free_percent": parse_threshold_free_percent(threshold_free_percent),
        "notify_not_before": notify_not_before.strip(),
        "notify_not_after": notify_not_after.strip(),
        "timer_interval_hours": int(timer_interval_hours),
        "ssh": {
            "enabled": ssh_enabled,
            "config_file": ssh_config_file,
            "connect_timeout_seconds": DEFAULT_SSH_CONNECT_TIMEOUT_SECONDS,
            "command_timeout_seconds": DEFAULT_SSH_COMMAND_TIMEOUT_SECONDS,
        },
        "blacklist": {
            "mount_points": blacklist_mount_points,
        },
    }

    Path(args.output_config).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    Path(args.output_timer_interval).write_text(f"{timer_interval_hours}\n", encoding="utf-8")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check real local disks and send alerts when free space is below threshold."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Config path, default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--test", action="store_true", help="Send a message for every selected disk, ignoring threshold.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages instead of sending notifications.")
    parser.add_argument("--list-disks", action="store_true", help="Print selected disks and exit.")
    parser.add_argument("--probe-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--configure-install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--existing-config", help=argparse.SUPPRESS)
    parser.add_argument("--existing-config-label", help=argparse.SUPPRESS)
    parser.add_argument("--ssh-config", help=argparse.SUPPRESS)
    parser.add_argument("--ssh-home", help=argparse.SUPPRESS)
    parser.add_argument("--output-config", help=argparse.SUPPRESS)
    parser.add_argument("--output-timer-interval", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.probe_json:
        return print_probe_json()

    if args.configure_install:
        return configure_install(args)

    if args.list_disks:
        config = load_config(args.config) if Path(args.config).exists() else {"machine_name": socket.gethostname()}
        print_mounts(collect_target_usages(config))
        return 0

    config = load_config(args.config)
    return run(config, test_mode=args.test, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
