"""Configuration loading and validation, and per-scrape device revalidation.

Devices are an explicit allowlist; nothing under /dev is ever scanned.

Each configured path is resolved once at startup and the resulting /dev/sgN
is *pinned*: commands always run against that pinned path, never against
the configured symlink. Before every collection the configured path is
re-resolved and the pinned node is re-checked (see check_device). These
checks are point-in-time: they cannot stop the kernel from re-assigning
/dev/sgN to a different device between the check and sg_logs' open().
"""

import json
import os
import re
import stat
from dataclasses import dataclass, field

# fullmatch() everywhere: re's "$" also matches before a trailing newline.
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
# Only SCSI generic nodes are accepted. Rewinding st nodes (/dev/stN) rewind
# the tape on close, and st/nst nodes are exclusive-open, which would collide
# with backup software. Symlinks are allowed as long as they resolve to
# /dev/sgN.
SG_RE = re.compile(r"/dev/sg([0-9]{1,4})")
# Printable, no whitespace, no control characters.
SAFE_PATH_RE = re.compile(r"/[\x21-\x7e]{1,4095}")
HOST_RE = re.compile(r"[A-Za-z0-9.:\-]{1,253}")
KNOWN_COLLECTORS = ("sg_logs",)
# Keys from the withdrawn tapeinfo collector. Rejected so nobody believes it
# is still running.
REMOVED_KEYS = ("tapeinfo_path",)
DEFAULT_COLLECTORS = ("sg_logs",)
MAX_DEVICES = 16
# Linux Documentation/admin-guide/devices.txt: char major 21 = SCSI generic.
SCSI_GENERIC_MAJOR = 21
# SCSI peripheral device type 1 = sequential-access (tape).
SCSI_TYPE_TAPE = "1"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Device:
    name: str
    path: str        # as configured (may be a symlink)
    resolved: str    # /dev/sgN pinned at startup; the only path ever executed
    collectors: tuple = DEFAULT_COLLECTORS


@dataclass(frozen=True)
class Config:
    listen_address: str = "127.0.0.1"
    port: int = 9842
    command_timeout_seconds: float = 10.0
    max_output_bytes: int = 65536
    cache_seconds: float = 30.0
    sg_logs_path: str = "/usr/bin/sg_logs"
    devices: tuple = field(default_factory=tuple)


def _safe_abs_path(val, what):
    if not isinstance(val, str) or not SAFE_PATH_RE.fullmatch(val):
        raise ConfigError(f"{what} must be an absolute path without whitespace or control characters")
    if ".." in val.split("/"):
        raise ConfigError(f"{what} contains forbidden components")
    return val


def resolve_device_path(path, resolver=os.path.realpath):
    """Validate a configured device path and return the pinned /dev/sgN."""
    _safe_abs_path(path, "device path")
    if not path.startswith("/dev/"):
        raise ConfigError("device path must be an absolute /dev/ path")
    resolved = resolver(path)
    if not isinstance(resolved, str) or not SG_RE.fullmatch(resolved):
        raise ConfigError(
            "device path must be (or resolve to) a SCSI generic node /dev/sgN"
        )
    return resolved


def validate_device_path(path, resolver=os.path.realpath):
    resolve_device_path(path, resolver)
    return path


def _sysfs_type(sg_name):
    with open(f"/sys/class/scsi_generic/{sg_name}/device/type", encoding="ascii") as fh:
        return fh.read(16).strip()


def check_device(dev, resolver=os.path.realpath, stat_fn=os.stat, sysfs_type=_sysfs_type):
    """Re-check a device before running commands against it.

    Returns None if it still looks like the pinned SCSI generic tape node,
    else a status string (device_missing | device_changed | device_mismatch).
    """
    try:
        st = stat_fn(dev.resolved)
        if resolver(dev.path) != dev.resolved:
            return "device_changed"
    except FileNotFoundError:
        return "device_missing"
    except OSError:
        return "device_mismatch"
    if not stat.S_ISCHR(st.st_mode) or os.major(st.st_rdev) != SCSI_GENERIC_MAJOR:
        return "device_mismatch"
    try:
        if sysfs_type(os.path.basename(dev.resolved)) != SCSI_TYPE_TAPE:
            return "device_mismatch"
    except (OSError, ValueError):
        return "device_mismatch"
    return None


def _num(raw, key, default, lo, hi, kind=float):
    val = raw.get(key, default)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ConfigError(f"{key} must be a number")
    if kind is int and not isinstance(val, int):
        raise ConfigError(f"{key} must be an integer")
    val = kind(val)
    if not lo <= val <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return val


def validate_port(val):
    if isinstance(val, bool) or not isinstance(val, int) or not 0 <= val <= 65535:
        raise ConfigError("port must be an integer between 0 and 65535")
    return val


def validate_listen_address(val):
    if not isinstance(val, str) or not HOST_RE.fullmatch(val):
        raise ConfigError("listen_address must be a host name or IP address")
    return val


def validate_sg_logs_path(val):
    _safe_abs_path(val, "sg_logs_path")
    if os.path.basename(val) != "sg_logs":
        raise ConfigError("sg_logs_path basename must be sg_logs")
    return val


def check_sg_logs_executable(path, resolver=os.path.realpath,
                             stat_fn=os.stat, access=os.access):
    """Startup check: the configured sg_logs exists, is executable, and is
    not a symlink to some other program."""
    validate_sg_logs_path(path)
    real = resolver(path)
    if os.path.basename(real) != "sg_logs":
        raise ConfigError("sg_logs_path must not resolve to a program other than sg_logs")
    try:
        st = stat_fn(real)
    except OSError as exc:
        raise ConfigError(f"sg_logs_path is not accessible: {exc.strerror}") from None
    if not stat.S_ISREG(st.st_mode) or not access(real, os.X_OK):
        raise ConfigError("sg_logs_path must be an executable regular file")
    return path


def parse_config(raw, resolver=os.path.realpath):
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    for key in REMOVED_KEYS:
        if key in raw:
            raise ConfigError(f"{key} is not supported: tapeinfo is never executed")
    addr = validate_listen_address(raw.get("listen_address", "127.0.0.1"))
    devs_raw = raw.get("devices", [])
    if not isinstance(devs_raw, list) or len(devs_raw) > MAX_DEVICES:
        raise ConfigError(f"devices must be a list of at most {MAX_DEVICES}")
    devices, names, pinned = [], set(), set()
    for d in devs_raw:
        if not isinstance(d, dict):
            raise ConfigError("each device must be an object")
        name = d.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ConfigError("device name must match [A-Za-z][A-Za-z0-9_]{0,31}")
        if name in names:
            raise ConfigError(f"duplicate device name {name!r}")
        names.add(name)
        path = d.get("path")
        resolved = resolve_device_path(path, resolver)
        if resolved in pinned:
            raise ConfigError(f"device {name!r} resolves to an sg node that is already configured")
        pinned.add(resolved)
        cols = d.get("collectors", list(DEFAULT_COLLECTORS))
        if (not isinstance(cols, list) or not cols
                or any(c not in KNOWN_COLLECTORS for c in cols)):
            raise ConfigError(f"collectors must be a non-empty subset of {KNOWN_COLLECTORS}")
        devices.append(Device(name, path, resolved, tuple(dict.fromkeys(cols))))
    return Config(
        listen_address=addr,
        port=validate_port(raw.get("port", 9842)),
        command_timeout_seconds=_num(raw, "command_timeout_seconds", 10.0, 0.1, 120),
        max_output_bytes=_num(raw, "max_output_bytes", 65536, 1024, 1048576, int),
        cache_seconds=_num(raw, "cache_seconds", 30.0, 0, 3600),
        sg_logs_path=validate_sg_logs_path(raw.get("sg_logs_path", "/usr/bin/sg_logs")),
        devices=tuple(devices),
    )


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_config(raw)
