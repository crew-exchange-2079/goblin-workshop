"""Policy configuration (TOML). Every key is optional; see README.md."""

from __future__ import annotations

import dataclasses
import re
import tomllib


class ConfigError(Exception):
    pass


@dataclasses.dataclass
class Config:
    queues: list[str] = dataclasses.field(default_factory=list)
    require_queue: bool = True
    handlers_required: list[str] = dataclasses.field(default_factory=lambda: ["failure"])
    require_dag_timeout: bool = True
    max_dag_timeout_sec: int = 86400
    max_step_timeout_sec: int = 86400
    max_retry_limit: int = 5
    max_retry_interval_sec: int = 3600
    timezone: str = "UTC"
    next_runs: int = 3
    entropy_threshold: float = 4.0
    entropy_min_length: int = 20
    secret_allow: list = dataclasses.field(default_factory=list)  # compiled regexes
    exclude: list[str] = dataclasses.field(default_factory=list)
    disable: set[str] = dataclasses.field(default_factory=set)


HANDLER_EVENTS = ("init", "success", "failure", "abort", "exit", "wait")

# section -> {key: (Config attribute, type)}
_SCHEMA = {
    "queues": {"allow": ("queues", list), "require": ("require_queue", bool)},
    "handler_on": {"require": ("handlers_required", list)},
    "timeouts": {
        "require_dag": ("require_dag_timeout", bool),
        "max_dag_sec": ("max_dag_timeout_sec", int),
        "max_step_sec": ("max_step_timeout_sec", int),
    },
    "retry": {"max_limit": ("max_retry_limit", int), "max_interval_sec": ("max_retry_interval_sec", int)},
    "schedule": {"timezone": ("timezone", str), "next_runs": ("next_runs", int)},
    "secrets": {
        "entropy_threshold": ("entropy_threshold", float),
        "min_length": ("entropy_min_length", int),
        "allow": ("secret_allow", list),
    },
    "files": {"exclude": ("exclude", list)},
    "rules": {"disable": ("disable", list)},
}


def load(path: str | None) -> Config:
    cfg = Config()
    if not path:
        return cfg
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e.strerror}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from e
    for section, body in data.items():
        if section not in _SCHEMA or not isinstance(body, dict):
            raise ConfigError(f"unknown config section [{section}]")
        for key, value in body.items():
            if key not in _SCHEMA[section]:
                raise ConfigError(f"unknown config key {section}.{key}")
            attr, typ = _SCHEMA[section][key]
            ok = isinstance(value, typ) and not (typ in (int, float) and isinstance(value, bool))
            if typ is float and isinstance(value, int) and not isinstance(value, bool):
                value, ok = float(value), True
            if not ok or (typ is list and not all(isinstance(v, str) for v in value)):
                raise ConfigError(f"{section}.{key} must be {'a list of strings' if typ is list else typ.__name__}")
            setattr(cfg, attr, value)
    unknown = set(cfg.handlers_required) - set(HANDLER_EVENTS)
    if unknown:
        raise ConfigError(f"handler_on.require has unknown events: {', '.join(sorted(unknown))}")
    try:
        cfg.secret_allow = [re.compile(p) for p in cfg.secret_allow]
    except re.error as e:
        raise ConfigError(f"secrets.allow: invalid regex: {e}") from e
    cfg.disable = set(cfg.disable)
    for attr in ("max_dag_timeout_sec", "max_step_timeout_sec", "max_retry_interval_sec", "entropy_min_length"):
        if getattr(cfg, attr) <= 0:
            raise ConfigError(f"{attr} must be positive")
    if cfg.max_retry_limit < 0 or cfg.next_runs < 0:
        raise ConfigError("retry.max_limit and schedule.next_runs must not be negative")
    return cfg
