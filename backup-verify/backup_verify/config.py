"""Configuration loading and validation.

The config is a JSON file. Unknown keys are refused everywhere so a typo
(say "max_age_hour") can't silently disable a check. Secrets never live in
the config: restic reads its password from password_file, and any other
credential (AWS_*, B2_*, ...) is named in pass_env and taken from the
environment at run time.
"""

import json
import os
import re
from dataclasses import dataclass

NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
# restic --read-data-subset: n/t, x% / x.y%, or a size with k/m/g/t suffix.
SUBSET_RE = re.compile(r"[1-9][0-9]*/[1-9][0-9]*|[0-9]{1,3}(\.[0-9]+)?%|[1-9][0-9]*[kKmMgGtT]?")
# A docker/podman image reference: no whitespace, no leading dash.
IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}")
NETWORK_RE = re.compile(r"none|host|bridge|[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
PG_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
STANZA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
METHODS = ("pg_dump", "pgbackrest")
MAX_TARGETS = 64


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SampleRestore:
    files: int = 10
    max_file_bytes: int = 256 << 20
    compare_to_source: bool = False
    seed: int | None = None


@dataclass(frozen=True)
class ResticTarget:
    name: str
    repository: str
    password_file: str
    restic_path: str = "restic"
    pass_env: tuple = ()
    host: str | None = None
    tags: tuple = ()
    paths: tuple = ()
    max_age_hours: float = 26.0
    check: bool = True
    read_data_subset: str | None = None
    sample_restore: SampleRestore | None = SampleRestore()
    timeout_seconds: float = 3600.0
    # A backup or prune holding the lock makes check fail; wait this long first.
    retry_lock_minutes: int = 10
    kind: str = "restic"


@dataclass(frozen=True)
class Assertion:
    name: str
    sql: str
    expect: str | None = None
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class Mount:
    source: str
    target: str


@dataclass(frozen=True)
class PostgresTarget:
    name: str
    method: str
    image: str
    assertions: tuple
    database: str = "verify"
    max_age_hours: float = 26.0
    restore_timeout_seconds: float = 3600.0
    startup_timeout_seconds: float = 300.0
    statement_timeout_seconds: float = 300.0
    network: str = "none"
    # pg_dump
    dump: str | None = None
    create_roles: tuple = ()
    jobs: int = 1
    # pgbackrest
    stanza: str | None = None
    config_file: str | None = None
    mounts: tuple = ()
    backup_set: str | None = None
    superuser: str = "postgres"
    pass_env: tuple = ()
    kind: str = "postgres"


@dataclass(frozen=True)
class Config:
    targets: tuple
    textfile_path: str | None = None
    report_path: str | None = None
    work_dir: str | None = None
    container_runtime: str = "docker"


def _obj(value, where, allowed, required=()):
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be an object")
    unknown = sorted(set(value) - set(allowed) - set(required))
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = [k for k in required if k not in value]
    if missing:
        raise ConfigError(f"{where}: missing required key(s) {', '.join(missing)}")
    return value


def _str(value, where, pattern=None, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ConfigError(f"{where}: must be a non-empty string")
    if "\x00" in value:
        raise ConfigError(f"{where}: must not contain NUL")
    if pattern is not None and not pattern.fullmatch(value):
        raise ConfigError(f"{where}: invalid value {value!r}")
    return value


def _abs_path(value, where):
    _str(value, where)
    if not os.path.isabs(value) or any(c in value for c in "\n\r"):
        raise ConfigError(f"{where}: must be an absolute path")
    return value


def _num(value, where, lo, hi, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: must be a number")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{where}: must be an integer")
    # Comparisons are False for NaN, so NaN is refused here too.
    if not (lo <= value <= hi):
        raise ConfigError(f"{where}: must be between {lo} and {hi}")
    return value


def _bool(value, where):
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: must be true or false")
    return value


def _list(value, where, item, max_len=256):
    if not isinstance(value, list) or len(value) > max_len:
        raise ConfigError(f"{where}: must be a list of at most {max_len} items")
    return tuple(item(v, f"{where}[{i}]") for i, v in enumerate(value))


def _pass_env(value, where):
    return _list(value, where, lambda v, w: _str(v, w, ENV_NAME_RE))


def _restic(raw, where):
    keys = ("restic_path", "pass_env", "host", "tags", "paths", "max_age_hours",
            "check", "read_data_subset", "sample_restore", "timeout_seconds",
            "retry_lock_minutes")
    _obj(raw, where, keys, ("name", "repository", "password_file"))
    kw = {"name": _str(raw["name"], f"{where}.name", NAME_RE),
          "repository": _str(raw["repository"], f"{where}.repository"),
          "password_file": _abs_path(raw["password_file"], f"{where}.password_file")}
    if "restic_path" in raw:
        p = _str(raw["restic_path"], f"{where}.restic_path")
        if p != "restic":
            _abs_path(p, f"{where}.restic_path")
        kw["restic_path"] = p
    if "pass_env" in raw:
        kw["pass_env"] = _pass_env(raw["pass_env"], f"{where}.pass_env")
    if "host" in raw:
        kw["host"] = _str(raw["host"], f"{where}.host")
    if "tags" in raw:
        kw["tags"] = _list(raw["tags"], f"{where}.tags", _str)
    if "paths" in raw:
        kw["paths"] = _list(raw["paths"], f"{where}.paths", _abs_path)
    if "max_age_hours" in raw:
        kw["max_age_hours"] = _num(raw["max_age_hours"], f"{where}.max_age_hours", 0.01, 24 * 366)
    if "check" in raw:
        kw["check"] = _bool(raw["check"], f"{where}.check")
    if "read_data_subset" in raw:
        v = raw["read_data_subset"]
        kw["read_data_subset"] = None if v is None else _str(v, f"{where}.read_data_subset", SUBSET_RE)
        if kw["read_data_subset"] and not kw.get("check", True):
            raise ConfigError(f"{where}.read_data_subset: needs check: true")
    if "sample_restore" in raw:
        kw["sample_restore"] = _sample(raw["sample_restore"], f"{where}.sample_restore")
    if "timeout_seconds" in raw:
        kw["timeout_seconds"] = _num(raw["timeout_seconds"], f"{where}.timeout_seconds", 1, 7 * 86400)
    if "retry_lock_minutes" in raw:
        kw["retry_lock_minutes"] = _num(raw["retry_lock_minutes"], f"{where}.retry_lock_minutes",
                                        0, 24 * 60, integer=True)
    if any("," in t for t in kw.get("tags", ())):
        # The tags are passed as one --tag=a,b (snapshot must carry all of them).
        raise ConfigError(f"{where}.tags: tags must not contain ','")
    return ResticTarget(**kw)


def _sample(raw, where):
    if raw is None:
        return None
    _obj(raw, where, ("files", "max_file_bytes", "compare_to_source", "seed"))
    kw = {}
    if "files" in raw:
        kw["files"] = _num(raw["files"], f"{where}.files", 1, 1000, integer=True)
    if "max_file_bytes" in raw:
        kw["max_file_bytes"] = _num(raw["max_file_bytes"], f"{where}.max_file_bytes", 0, 1 << 40, integer=True)
    if "compare_to_source" in raw:
        kw["compare_to_source"] = _bool(raw["compare_to_source"], f"{where}.compare_to_source")
    if "seed" in raw and raw["seed"] is not None:
        kw["seed"] = _num(raw["seed"], f"{where}.seed", 0, 2**63, integer=True)
    return SampleRestore(**kw)


def _assertion(raw, where):
    _obj(raw, where, ("expect", "min", "max"), ("name", "sql"))
    kw = {"name": _str(raw["name"], f"{where}.name", NAME_RE),
          "sql": _str(raw["sql"], f"{where}.sql")}
    if "expect" in raw:
        kw["expect"] = _str(raw["expect"], f"{where}.expect", allow_empty=True)
    for k in ("min", "max"):
        if k in raw:
            kw[k] = _num(raw[k], f"{where}.{k}", -1e300, 1e300)
    if kw.get("expect") is not None and ("min" in kw or "max" in kw):
        raise ConfigError(f"{where}: use either expect or min/max, not both")
    return Assertion(**kw)


def _mount(raw, where):
    _obj(raw, where, (), ("source", "target"))
    src = _abs_path(raw["source"], f"{where}.source")
    dst = _abs_path(raw["target"], f"{where}.target")
    for v, k in ((src, "source"), (dst, "target")):
        if "," in v or ":" in v:
            raise ConfigError(f"{where}.{k}: must not contain ',' or ':'")
    return Mount(src, dst)


def _postgres(raw, where):
    common = ("database", "max_age_hours", "restore_timeout_seconds", "startup_timeout_seconds",
              "statement_timeout_seconds", "network", "superuser")
    if not isinstance(raw, dict) or raw.get("method") not in METHODS:
        raise ConfigError(f"{where}.method: must be one of {', '.join(METHODS)}")
    if raw["method"] == "pg_dump":
        _obj(raw, where, common + ("create_roles", "jobs"),
             ("name", "method", "image", "assertions", "dump"))
    else:
        _obj(raw, where, common + ("mounts", "backup_set", "pass_env"),
             ("name", "method", "image", "assertions", "stanza", "config_file"))
    kw = {"name": _str(raw["name"], f"{where}.name", NAME_RE),
          "method": raw["method"],
          "image": _str(raw["image"], f"{where}.image", IMAGE_RE),
          "assertions": _list(raw["assertions"], f"{where}.assertions", _assertion)}
    if not kw["assertions"]:
        # A restore that is never queried proves little; make it explicit.
        raise ConfigError(f"{where}.assertions: at least one assertion is required")
    names = [a.name for a in kw["assertions"]]
    if len(set(names)) != len(names):
        raise ConfigError(f"{where}.assertions: names must be unique")
    if "database" in raw:
        kw["database"] = _str(raw["database"], f"{where}.database", PG_IDENT_RE)
    if "superuser" in raw:
        kw["superuser"] = _str(raw["superuser"], f"{where}.superuser", PG_IDENT_RE)
    if "max_age_hours" in raw:
        kw["max_age_hours"] = _num(raw["max_age_hours"], f"{where}.max_age_hours", 0.01, 24 * 366)
    for k in ("restore_timeout_seconds", "startup_timeout_seconds", "statement_timeout_seconds"):
        if k in raw:
            kw[k] = _num(raw[k], f"{where}.{k}", 1, 7 * 86400)
    if "network" in raw:
        kw["network"] = _str(raw["network"], f"{where}.network", NETWORK_RE)
    if "pass_env" in raw:
        kw["pass_env"] = _pass_env(raw["pass_env"], f"{where}.pass_env")
    if raw["method"] == "pg_dump":
        kw["dump"] = _abs_path(raw["dump"], f"{where}.dump")
        if "create_roles" in raw:
            kw["create_roles"] = _list(raw["create_roles"], f"{where}.create_roles",
                                       lambda v, w: _str(v, w, PG_IDENT_RE))
        if "jobs" in raw:
            kw["jobs"] = _num(raw["jobs"], f"{where}.jobs", 1, 64, integer=True)
    else:
        kw["stanza"] = _str(raw["stanza"], f"{where}.stanza", STANZA_RE)
        kw["config_file"] = _abs_path(raw["config_file"], f"{where}.config_file")
        if "mounts" in raw:
            kw["mounts"] = _list(raw["mounts"], f"{where}.mounts", _mount, max_len=16)
        if "backup_set" in raw:
            kw["backup_set"] = _str(raw["backup_set"], f"{where}.backup_set",
                                    re.compile(r"[0-9]{8}-[0-9]{6}F(_[0-9]{8}-[0-9]{6}[DI])?"))
    return PostgresTarget(**kw)


def parse(raw):
    _obj(raw, "config", ("textfile_path", "report_path", "work_dir", "container_runtime",
                         "restic", "postgres"))
    kw = {}
    for k in ("textfile_path", "report_path", "work_dir"):
        if k in raw and raw[k] is not None:
            kw[k] = _abs_path(raw[k], k)
    if kw.get("textfile_path") and not kw["textfile_path"].endswith(".prom"):
        # node_exporter's textfile collector ignores anything else.
        raise ConfigError("textfile_path: must end in .prom")
    if "container_runtime" in raw:
        rt = _str(raw["container_runtime"], "container_runtime")
        if rt not in ("docker", "podman"):
            _abs_path(rt, "container_runtime")
        kw["container_runtime"] = rt
    targets = []
    targets += _list(raw.get("restic", []), "restic", _restic, MAX_TARGETS)
    targets += _list(raw.get("postgres", []), "postgres", _postgres, MAX_TARGETS)
    if not targets:
        raise ConfigError("config: no restic or postgres targets configured")
    names = [t.name for t in targets]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"config: duplicate target name(s) {', '.join(dupes)}")
    return Config(targets=tuple(targets), **kw)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from None
    except (ValueError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: not valid JSON: {e}") from None
    return parse(raw)
