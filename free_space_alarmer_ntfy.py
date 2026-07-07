#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///

from __future__ import annotations

import argparse
import json
import math
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = "/etc/free-space-alarmer-ntfy/config.json"
DEFAULT_THRESHOLD_FREE_PERCENT = 10.0
GIB = 1024**3

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


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from None

    required_fields = ("ntfy_base_url", "ntfy_topic")
    missing_fields = [field for field in required_fields if not str(config.get(field, "")).strip()]
    if missing_fields:
        raise SystemExit(f"Missing required config fields: {', '.join(missing_fields)}")

    config.setdefault("machine_name", socket.gethostname())
    config.setdefault("threshold_free_percent", DEFAULT_THRESHOLD_FREE_PERCENT)
    return config


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


def collect_usages() -> list[DiskUsage]:
    usages: list[DiskUsage] = []
    for mount in selected_mounts():
        usage = get_disk_usage(mount)
        if usage is not None:
            usages.append(usage)
    return usages


def print_mounts(usages: list[DiskUsage]) -> None:
    if not usages:
        print("No suitable local disks found.")
        return

    for usage in usages:
        print(
            f"{disk_label(usage.mount)}: "
            f"{format_number(usage.free_percent)}% free, "
            f"{format_number(usage.available_gb)} GB available"
        )


def run(config: dict[str, Any], *, test_mode: bool, dry_run: bool) -> int:
    threshold = float(config.get("threshold_free_percent", DEFAULT_THRESHOLD_FREE_PERCENT))
    machine_name = str(config.get("machine_name") or socket.gethostname())
    usages = collect_usages()

    if test_mode:
        messages = [format_message(usage, machine_name) for usage in usages]
    else:
        messages = [
            format_message(usage, machine_name)
            for usage in usages
            if usage.free_percent < threshold
        ]

    for message in messages:
        if dry_run:
            print(message)
            continue
        send_ntfy_message(message, config)
        print(message)

    if not messages and dry_run:
        print("No alerts to send.")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check real local disks and send ntfy alerts when free space is below threshold."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Config path, default: {DEFAULT_CONFIG_PATH}")
    parser.add_argument("--test", action="store_true", help="Send a message for every selected disk, ignoring threshold.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages instead of sending them to ntfy.")
    parser.add_argument("--list-disks", action="store_true", help="Print selected local disks and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_disks:
        print_mounts(collect_usages())
        return 0

    config = load_config(args.config)
    return run(config, test_mode=args.test, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
