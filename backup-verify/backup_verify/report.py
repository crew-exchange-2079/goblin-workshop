"""Results, the JSON report, and the Prometheus textfile.

A check is "ok", "failed" or "skipped" (could not run because an earlier
check of the same target failed, e.g. no snapshot to sample). Skipped is
never reported as success: a target is successful only if every check that
was configured for it is ok.
"""

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field

OK, FAILED, SKIPPED = "ok", "failed", "skipped"


@dataclass
class Check:
    name: str
    status: str = FAILED
    duration_seconds: float = 0.0
    error: str = ""
    details: dict = field(default_factory=dict)

    def to_json(self):
        out = {"name": self.name, "status": self.status,
               "duration_seconds": round(self.duration_seconds, 3)}
        if self.error:
            out["error"] = self.error
        if self.details:
            out["details"] = self.details
        return out


@dataclass
class Target:
    name: str
    kind: str                     # restic | postgres
    checks: list = field(default_factory=list)
    # Unix time of the newest backup found, or None.
    latest_backup_timestamp: float | None = None
    # Prometheus gauges specific to this target: (metric suffix, extra labels, value).
    gauges: list = field(default_factory=list)
    # Per-assertion results for postgres targets: (assertion name, passed).
    assertions: list = field(default_factory=list)

    @property
    def ok(self):
        return bool(self.checks) and all(c.status == OK for c in self.checks)

    def to_json(self):
        return {"name": self.name, "kind": self.kind,
                "status": OK if self.ok else FAILED,
                "latest_backup_timestamp": self.latest_backup_timestamp,
                "latest_backup_time": _iso(self.latest_backup_timestamp),
                "checks": [c.to_json() for c in self.checks]}


@dataclass
class Report:
    started: float
    finished: float = 0.0
    targets: list = field(default_factory=list)

    @property
    def ok(self):
        return bool(self.targets) and all(t.ok for t in self.targets)

    def to_json(self):
        from . import __version__
        return {"tool": "backup-verify", "version": __version__,
                "status": OK if self.ok else FAILED,
                "started": _iso(self.started), "finished": _iso(self.finished),
                "duration_seconds": round(self.finished - self.started, 3),
                "targets": [t.to_json() for t in self.targets]}


def _iso(ts):
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class timed:
    """with timed(check): ... records duration even if the body raises."""

    def __init__(self, check):
        self.check = check

    def __enter__(self):
        self.t0 = time.monotonic()
        return self.check

    def __exit__(self, *exc):
        self.check.duration_seconds = time.monotonic() - self.t0
        return False


# --- Prometheus text exposition format ---

PREFIX = "backup_verify_"

HELP = {
    "run_success": ("gauge", "1 if every configured target passed every check in the last run."),
    "last_run_timestamp_seconds": ("gauge", "Unix time the last run finished."),
    "run_duration_seconds": ("gauge", "Wall time of the last run."),
    "target_success": ("gauge", "1 if every check of this target passed."),
    "check_success": ("gauge", "1 if the check passed, 0 if it failed or was skipped."),
    "check_skipped": ("gauge", "1 if the check could not run because an earlier check failed."),
    "check_duration_seconds": ("gauge", "Wall time of the check."),
    "latest_backup_timestamp_seconds": ("gauge", "Unix time of the newest backup found (restic snapshot time, dump file mtime, pgBackRest backup stop time)."),
    "sample_files": ("gauge", "Files restored by the last sample restore."),
    "sample_bytes": ("gauge", "Bytes restored and hashed by the last sample restore."),
    "sample_mismatches": ("gauge", "Sampled files whose restored content did not match (size, or source hash when compared)."),
    "sample_source_compared": ("gauge", "Sampled files that were also compared against the unchanged source file."),
    "assertion_success": ("gauge", "1 if the SQL assertion held on the restored database."),
}


def _esc(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"


def _num(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "+Inf" if v > 0 else "-Inf"
        return repr(v)
    return str(v)


def render_metrics(report):
    samples = {}   # metric -> [(labels, value)]

    def add(metric, labels, value):
        samples.setdefault(metric, []).append((labels, value))

    add("run_success", {}, report.ok)
    add("last_run_timestamp_seconds", {}, round(report.finished, 3))
    add("run_duration_seconds", {}, round(report.finished - report.started, 3))
    for t in report.targets:
        base = {"target": t.name, "kind": t.kind}
        add("target_success", base, t.ok)
        if t.latest_backup_timestamp is not None:
            add("latest_backup_timestamp_seconds", base, round(t.latest_backup_timestamp, 3))
        for c in t.checks:
            labels = {**base, "check": c.name}
            add("check_success", labels, c.status == OK)
            add("check_skipped", labels, c.status == SKIPPED)
            add("check_duration_seconds", labels, round(c.duration_seconds, 3))
        for suffix, extra, value in t.gauges:
            add(suffix, {**base, **extra}, value)
        for name, passed in t.assertions:
            add("assertion_success", {**base, "assertion": name}, passed)

    lines = []
    for metric in HELP:
        if metric not in samples:
            continue
        mtype, text = HELP[metric]
        lines.append(f"# HELP {PREFIX}{metric} {text}")
        lines.append(f"# TYPE {PREFIX}{metric} {mtype}")
        for labels, value in samples[metric]:
            lines.append(f"{PREFIX}{metric}{_labels(labels)} {_num(value)}")
    return "\n".join(lines) + "\n"


def atomic_write(path, data):
    """Write via a temp file in the same directory and rename into place.

    node_exporter's textfile collector only reads *.prom, so the temp name
    (*.prom.tmp-XXXX) is never scraped half-written.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render_json(report):
    return json.dumps(report.to_json(), indent=2, sort_keys=False) + "\n"
