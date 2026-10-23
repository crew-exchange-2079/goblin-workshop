"""restic checks: snapshot freshness, repository check, sample restore.

Only read-side restic commands are run: snapshots, check, ls and restore
(into a private temporary directory). restic check takes a non-exclusive
lock, like any reader; nothing here forgets, prunes, or writes data.
"""

import calendar
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import tempfile
import time

from . import runner
from .report import FAILED, OK, SKIPPED, Check, Target, timed

# restic (Go) prints RFC 3339 with up to nanosecond precision and either Z or
# an offset: 2026-10-23T21:49:04.293099505Z, 2026-10-23T23:49:04+02:00.
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
                    r"(Z|[+-]\d{2}:\d{2})")
_PATTERN_SPECIAL = re.compile(r"([\\*?\[\]])")
HASH_CHUNK = 1 << 20


def parse_time_ns(text):
    """RFC 3339 timestamp -> Unix nanoseconds (int). Raises ValueError."""
    m = _TS_RE.fullmatch(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"unparseable timestamp {text!r}")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    secs = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0))
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        secs -= sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return secs * 10**9 + int((frac or "0").ljust(9, "0"))


def parse_time(text):
    """RFC 3339 timestamp -> Unix seconds (float). Raises ValueError."""
    return parse_time_ns(text) / 1e9


def escape_pattern(path):
    """Make a literal path safe as a restic --include pattern.

    restic matches each path component with Go's filepath.Match, where
    backslash escapes the next character on Unix.
    """
    return _PATTERN_SPECIAL.sub(r"\\\1", path)


class Restic:
    def __init__(self, target, work_dir=None, run=runner.run, environ=None):
        self.t = target
        self.work_dir = work_dir
        self._run = run
        # HOME/XDG_CACHE_HOME let restic use its cache for index and tree
        # blobs; "restic check" ignores it and uses a fresh one anyway.
        self.env = runner.build_env(
            ("HOME", "XDG_CACHE_HOME", "TMPDIR") + tuple(target.pass_env),
            {"RESTIC_PROGRESS_FPS": "0.0167"}, environ)

    def argv(self, *args):
        return [self.t.restic_path, f"--repo={self.t.repository}",
                f"--password-file={self.t.password_file}", "--quiet",
                f"--retry-lock={self.t.retry_lock_minutes}m", *args]

    def call(self, *args, timeout=None, **kw):
        return self._run(self.argv(*args), timeout or self.t.timeout_seconds,
                         env=self.env, work_dir=self.work_dir, **kw)

    def snapshot_filters(self):
        out = []
        if self.t.host:
            out.append(f"--host={self.t.host}")
        if self.t.tags:
            out.append(f"--tag={','.join(self.t.tags)}")
        out += [f"--path={p}" for p in self.t.paths]
        return out


def verify(target, work_dir=None, run=runner.run, now=time.time, environ=None):
    """Run every configured check for one restic target. Never raises."""
    r = Restic(target, work_dir, run, environ)
    result = Target(target.name, "restic")
    latest = _freshness(r, result, now)
    if target.check:
        _check(r, result)
    if target.sample_restore is not None:
        check = Check("sample_restore")
        result.checks.append(check)
        if latest is None:
            check.status = SKIPPED
            check.error = "no snapshot to restore from"
        else:
            with timed(check):
                try:
                    _sample_restore(r, latest, check, result)
                except Exception as e:  # report, never crash the run
                    check.status = FAILED
                    check.error = f"internal error: {type(e).__name__}: {e}"
    return result


def _freshness(r, result, now):
    """Return the newest matching snapshot (dict) or None."""
    check = Check("freshness")
    result.checks.append(check)
    with timed(check):
        res = r.call("snapshots", "--json", *r.snapshot_filters(), max_stdout=256 << 20)
        if not res.ok or res.stdout_truncated:
            check.error = res.describe() if not res.ok else "snapshot list too large"
            return None
        try:
            snaps = json.loads(res.stdout)
            if not isinstance(snaps, list):
                raise ValueError("expected a JSON list")
            dated = [(parse_time(s["time"]), s) for s in snaps]
        except (ValueError, KeyError, TypeError) as e:
            check.error = f"cannot parse restic snapshots output: {e}"
            return None
        check.details["snapshots"] = len(dated)
        if not dated:
            check.error = "no snapshots match the configured filter"
            return None
        ts, snap = max(dated, key=lambda x: x[0])
        age = now() - ts
        result.latest_backup_timestamp = ts
        check.details.update({"latest_id": snap.get("id"), "latest_time": snap.get("time"),
                              "latest_host": snap.get("hostname"),
                              "age_hours": round(age / 3600, 3),
                              "max_age_hours": r.t.max_age_hours})
        if age > r.t.max_age_hours * 3600:
            check.error = (f"newest snapshot is {age / 3600:.1f}h old "
                           f"(limit {r.t.max_age_hours}h)")
        else:
            check.status = OK
        return snap


def _check(r, result):
    check = Check("check")
    result.checks.append(check)
    with timed(check):
        args = ["check"]
        if r.t.read_data_subset:
            args.append(f"--read-data-subset={r.t.read_data_subset}")
            check.details["read_data_subset"] = r.t.read_data_subset
        res = r.call(*args, max_stdout=1 << 20)
        if res.ok:
            check.status = OK
        else:
            check.error = res.describe()
            check.details["stderr_tail"] = res.stderr_tail[-2000:]


def _pick_files(lines, k, max_bytes, rng):
    """Reservoir-sample k regular files (<= max_bytes) from restic ls --json lines."""
    chosen, seen = [], 0
    for line in lines:
        try:
            node = json.loads(line)
        except ValueError:
            continue
        if not isinstance(node, dict) or node.get("type") != "file":
            continue
        if node.get("message_type", node.get("struct_type")) != "node":
            continue
        size, path = node.get("size"), node.get("path")
        if not isinstance(size, int) or not isinstance(path, str) or size > max_bytes:
            continue
        seen += 1
        entry = {"path": path, "size": size, "mtime": node.get("mtime")}
        if len(chosen) < k:
            chosen.append(entry)
        else:
            j = rng.randrange(seen)
            if j < k:
                chosen[j] = entry
    return chosen, seen


def _sha256(path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _sample_restore(r, snap, check, result):
    cfg = r.t.sample_restore
    snap_id = snap.get("id")
    if not isinstance(snap_id, str) or not re.fullmatch(r"[0-9a-f]{64}", snap_id):
        check.error = f"unexpected snapshot id {snap_id!r}"
        return
    rng = random.Random(cfg.seed) if cfg.seed is not None else secrets.SystemRandom()
    picked = {}

    def sink(fh):
        lines = (raw.decode("utf-8", "surrogateescape") for raw in fh)
        picked["files"], picked["eligible"] = _pick_files(lines, cfg.files, cfg.max_file_bytes, rng)

    res = r.call("ls", "--json", snap_id, stdout_sink=sink)
    if not res.ok:
        check.error = res.describe()
        return
    files, eligible = picked["files"], picked["eligible"]
    check.details.update({"snapshot": snap_id, "eligible_files": eligible})
    if not files:
        check.error = (f"snapshot has no regular files of at most {cfg.max_file_bytes} bytes "
                       "to sample")
        return

    tmp = tempfile.mkdtemp(prefix="backup-verify-restic-", dir=r.work_dir)
    try:
        os.chmod(tmp, 0o700)
        includes = [f"--include={escape_pattern(f['path'])}" for f in files]
        # --verify re-reads every restored file and checks it against the
        # blob hashes stored in the repository.
        res = r.call("restore", snap_id, f"--target={tmp}", "--verify", *includes)
        if not res.ok:
            check.error = res.describe()
            check.details["stderr_tail"] = res.stderr_tail[-2000:]
            return
        _compare(files, tmp, cfg, check, result)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _compare(files, tmp, cfg, check, result):
    total_bytes = mismatches = compared = 0
    report = []
    for f in files:
        entry = {"path": f["path"], "size": f["size"]}
        restored = os.path.join(tmp, f["path"].lstrip("/"))
        try:
            digest, n = _sha256(restored)
        except OSError as e:
            entry["status"] = "missing"
            entry["error"] = e.strerror
            mismatches += 1
            report.append(entry)
            continue
        total_bytes += n
        entry["sha256"] = digest
        if n != f["size"]:
            entry["status"] = "size_mismatch"
            entry["restored_size"] = n
            mismatches += 1
        elif cfg.compare_to_source:
            entry["status"] = _against_source(f, digest)
            if entry["status"] == "source_mismatch":
                mismatches += 1
            if entry["status"] in ("source_match", "source_mismatch"):
                compared += 1
        else:
            entry["status"] = "restored"
        report.append(entry)
    check.details.update({"files": report, "restored_bytes": total_bytes,
                          "mismatches": mismatches})
    if cfg.compare_to_source:
        check.details["source_compared"] = compared
        result.gauges.append(("sample_source_compared", {}, compared))
    result.gauges += [("sample_files", {}, len(files)), ("sample_bytes", {}, total_bytes),
                      ("sample_mismatches", {}, mismatches)]
    if mismatches:
        check.error = f"{mismatches} of {len(files)} sampled files did not restore intact"
    else:
        check.status = OK


def _against_source(f, digest):
    """Compare with the live file, only if it looks unchanged since the backup.

    A file modified after the snapshot is expected to differ; that is
    "source_changed", not a failure. Unchanged means same size and an mtime
    equal to the one restic recorded (restic keeps the nanoseconds stat
    returned, so an untouched file matches exactly).
    """
    try:
        st = os.stat(f["path"], follow_symlinks=False)
    except OSError:
        return "source_missing"
    try:
        snap_mtime_ns = parse_time_ns(f.get("mtime"))
    except ValueError:
        return "source_unknown_mtime"
    if st.st_size != f["size"] or st.st_mtime_ns != snap_mtime_ns:
        return "source_changed"
    try:
        live, _ = _sha256(f["path"])
    except OSError:
        return "source_unreadable"
    return "source_match" if live == digest else "source_mismatch"
