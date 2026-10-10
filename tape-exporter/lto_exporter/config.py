"""Configuration loading and validation.

Devices are an explicit allowlist; nothing under /dev is ever scanned.
"""

import json
import os
import re
from dataclasses import dataclass, field

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
# Only SCSI generic nodes are accepted. Rewinding st nodes (/dev/stN) rewind
# the tape on close, and st/nst nodes are exclusive-open, which would collide
# with backup software. Symlinks (e.g. /dev/tape/by-id/...-generic style) are
# allowed as long as they resolve to /dev/sgN.
SG_RE = re.compile(r"^/dev/sg[0-9]{1,4}$")
KNOWN_COLLECTORS = ("sg_logs",)
# Keys from the withdrawn tapeinfo collector. Rejected so nobody believes it
# is still running.
REMOVED_KEYS = ("tapeinfo_path",)
DEFAULT_COLLECTORS = ("sg_logs",)
MAX_DEVICES = 16


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Device:
    name: str
    path: str
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


def validate_device_path(path, resolver=os.path.realpath):
    if not isinstance(path, str) or not path.startswith("/dev/"):
        raise ConfigError("device path must be an absolute /dev/ path")
    if "\x00" in path or ".." in path.split("/"):
        raise ConfigError("device path contains forbidden components")
    resolved = resolver(path)
    if not SG_RE.match(resolved):
        raise ConfigError(
            "device path must be (or resolve to) a SCSI generic node /dev/sgN"
        )
    return path


def _num(raw, key, default, lo, hi, kind=float):
    val = raw.get(key, default)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ConfigError(f"{key} must be a number")
    val = kind(val)
    if not lo <= val <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return val


def _exe(raw, key, default):
    val = raw.get(key, default)
    if not isinstance(val, str) or not os.path.isabs(val):
        raise ConfigError(f"{key} must be an absolute path")
    return val


def parse_config(raw, resolver=os.path.realpath):
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    for key in REMOVED_KEYS:
        if key in raw:
            raise ConfigError(f"{key} is not supported: tapeinfo is never executed")
    addr = raw.get("listen_address", "127.0.0.1")
    if not isinstance(addr, str) or not addr:
        raise ConfigError("listen_address must be a non-empty string")
    devs_raw = raw.get("devices", [])
    if not isinstance(devs_raw, list) or len(devs_raw) > MAX_DEVICES:
        raise ConfigError(f"devices must be a list of at most {MAX_DEVICES}")
    devices, names = [], set()
    for d in devs_raw:
        if not isinstance(d, dict):
            raise ConfigError("each device must be an object")
        name = d.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError("device name must match [A-Za-z][A-Za-z0-9_]{0,31}")
        if name in names:
            raise ConfigError(f"duplicate device name {name!r}")
        names.add(name)
        path = validate_device_path(d.get("path"), resolver)
        cols = d.get("collectors", list(DEFAULT_COLLECTORS))
        if (not isinstance(cols, list) or not cols
                or any(c not in KNOWN_COLLECTORS for c in cols)):
            raise ConfigError(f"collectors must be a non-empty subset of {KNOWN_COLLECTORS}")
        devices.append(Device(name, path, tuple(dict.fromkeys(cols))))
    return Config(
        listen_address=addr,
        port=_num(raw, "port", 9842, 0, 65535, int),
        command_timeout_seconds=_num(raw, "command_timeout_seconds", 10.0, 0.1, 120),
        max_output_bytes=_num(raw, "max_output_bytes", 65536, 1024, 1048576, int),
        cache_seconds=_num(raw, "cache_seconds", 30.0, 0, 3600),
        sg_logs_path=_exe(raw, "sg_logs_path", "/usr/bin/sg_logs"),
        devices=tuple(devices),
    )


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return parse_config(raw)
