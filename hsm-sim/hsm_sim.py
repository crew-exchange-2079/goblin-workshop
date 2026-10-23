#!/usr/bin/env python3
"""hsm-sim: a deterministic teaching simulator of a disk cache in front of a tape archive.

It models the generic hierarchical-storage-management (HSM) life cycle that most
disk+tape archives share, without imitating any particular product:

  write    a file lands on the disk cache            -> RESIDENT (disk only)
  migrate  a policy scan copies it to tape            -> PREMIGRATED (disk + tape)
  purge    a watermark frees its disk copy            -> MIGRATED (tape only, stub on disk)
  recall   a read of a stub copies it back from tape  -> PREMIGRATED again
  repack   live data is copied off a tape full of dead copies, freeing the cartridge

The simulation is integer-second, event-driven and contains no randomness: the same
scenario always prints the same timeline. See README.md for the scenario format and
the modelling simplifications.
"""

import argparse
import heapq
import re
import sys

import yaml

__version__ = "0.1.0"

RESIDENT, PREMIGRATED, MIGRATED = "resident", "premigrated", "migrated"
SCRATCH, FILLING, FULL = "scratch", "filling", "full"

# Queued jobs are started in this order; within a kind, oldest first.
# Recalls go first because a reader is waiting on them; repack is housekeeping.
PRIORITY = {"recall": 0, "migrate": 1, "repack": 2}

# Events that share a timestamp are handled in this order.
EV_DONE, EV_SCENARIO, EV_SCAN = 0, 1, 2

MAX_NAMES = 6  # file names listed per timeline line before "+N more"


class ScenarioError(Exception):
    """The scenario file is missing, unparsable or fails validation."""


# --------------------------------------------------------------------------
# scenario loading and validation

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pos_int(v):
    return _is_int(v) and v > 0


def _nonneg_int(v):
    return _is_int(v) and v >= 0


def _fraction(v):
    return _is_num(v) and 0 < v <= 1


def _text(v):
    return isinstance(v, str) and v.strip() != ""


_DESC = {
    _pos_int: "a positive integer",
    _nonneg_int: "a non-negative integer",
    _fraction: "a number in (0, 1]",
    _text: "a non-empty string",
}

REQUIRED = object()

SECTIONS = {
    "cache": {
        "capacity_mb": (_pos_int, REQUIRED),
        "high_watermark": (_fraction, REQUIRED),
        "low_watermark": (_fraction, REQUIRED),
    },
    "library": {
        "drives": (_pos_int, REQUIRED),
        "tapes": (_pos_int, REQUIRED),
        "tape_capacity_mb": (_pos_int, REQUIRED),
        "mount_s": (_nonneg_int, REQUIRED),
        "seek_s": (_nonneg_int, REQUIRED),
        "rate_mb_s": (_pos_int, REQUIRED),
    },
    "policy": {
        "scan_interval_s": (_pos_int, REQUIRED),
        "migrate_after_s": (_nonneg_int, REQUIRED),
        "batch_mb": (_pos_int, None),
        "repack_below": (_fraction, None),
    },
}

TOP_KEYS = {"name", "description", "duration_s", "events"} | set(SECTIONS)
EVENT_KEYS = {
    "write": {"file", "size_mb"},
    "read": {"file"},
    "delete": {"file"},
    "repack": {"tape"},
}
EVENT_COMMON = {"t", "op", "count", "every_s"}
NAME_FIELD = re.compile(r"\{i(?::0?\d+d)?\}")


def _check_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        raise ScenarioError(f"{where}: expected a mapping, got {type(obj).__name__}")
    unknown = sorted(str(k) for k in obj if k not in allowed)
    if unknown:
        raise ScenarioError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                            f"allowed: {', '.join(sorted(allowed))}")


def _field(obj, key, check, default, where):
    if key not in obj or obj[key] is None:
        if default is REQUIRED:
            raise ScenarioError(f"{where}.{key}: required")
        return default
    value = obj[key]
    if not check(value):
        raise ScenarioError(f"{where}.{key}: must be {_DESC[check]}, got {value!r}")
    return value


def tape_id(n):
    return f"T{n:02d}"


def _expand_name(pattern, i, where):
    # Only {i} and {i:03d}-style fields are allowed, so a scenario cannot reach
    # anything through str.format attribute lookups.
    rest = NAME_FIELD.sub("", pattern)
    if "{" in rest or "}" in rest:
        raise ScenarioError(f"{where}.file: only {{i}} or {{i:03d}} placeholders are allowed, "
                            f"got {pattern!r}")
    return pattern.format(i=i)


def parse_scenario(doc):
    """Validate a decoded YAML document and return a normalised scenario dict."""
    _check_keys(doc, TOP_KEYS, "scenario")
    sc = {
        "name": _field(doc, "name", _text, REQUIRED, "scenario"),
        "description": _field(doc, "description", _text, "", "scenario").strip(),
        "duration_s": _field(doc, "duration_s", _pos_int, REQUIRED, "scenario"),
    }
    for sec, spec in SECTIONS.items():
        raw = doc.get(sec)
        if raw is None:
            raise ScenarioError(f"{sec}: required")
        _check_keys(raw, set(spec), sec)
        sc[sec] = {k: _field(raw, k, chk, dflt, sec) for k, (chk, dflt) in spec.items()}

    cache, lib = sc["cache"], sc["library"]
    if cache["low_watermark"] >= cache["high_watermark"]:
        raise ScenarioError("cache: low_watermark must be below high_watermark")
    if lib["tapes"] > 99:
        raise ScenarioError("library.tapes: at most 99")

    raw_events = doc.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ScenarioError("events: required, a non-empty list")
    tapes = {tape_id(n) for n in range(1, lib["tapes"] + 1)}
    duration = sc["duration_s"]
    events = []
    uses_repack = sc["policy"]["repack_below"] is not None
    for idx, ev in enumerate(raw_events):
        where = f"events[{idx}]"
        if not isinstance(ev, dict):
            raise ScenarioError(f"{where}: expected a mapping, got {type(ev).__name__}")
        op = ev.get("op")
        if op not in EVENT_KEYS:
            raise ScenarioError(f"{where}.op: must be one of {', '.join(EVENT_KEYS)}, got {op!r}")
        _check_keys(ev, EVENT_COMMON | EVENT_KEYS[op], where)
        t0 = _field(ev, "t", _nonneg_int, REQUIRED, where)
        count = _field(ev, "count", _pos_int, 1, where)
        every = _field(ev, "every_s", _nonneg_int, 0, where)
        last = t0 + (count - 1) * every
        if last > duration:
            raise ScenarioError(f"{where}: event at t={last} is after duration_s={duration}")
        if op == "repack":
            uses_repack = True
            tape = _field(ev, "tape", _text, REQUIRED, where)
            if tape not in tapes:
                raise ScenarioError(f"{where}.tape: no tape {tape!r} (library has "
                                    f"{tape_id(1)}..{tape_id(lib['tapes'])})")
        else:
            pattern = _field(ev, "file", _text, REQUIRED, where)
            if count > 1 and not NAME_FIELD.search(pattern) and op == "write":
                raise ScenarioError(f"{where}.file: a write with count > 1 needs an {{i}} "
                                    f"placeholder, or every copy overwrites the same file")
        size = None
        if op == "write":
            size = _field(ev, "size_mb", _pos_int, REQUIRED, where)
            if size > lib["tape_capacity_mb"]:
                raise ScenarioError(f"{where}.size_mb: {size} MB does not fit on one "
                                    f"{lib['tape_capacity_mb']} MB tape (files are not split)")
            if size > cache["capacity_mb"]:
                raise ScenarioError(f"{where}.size_mb: {size} MB is larger than the cache")
        for i in range(count):
            e = {"t": t0 + i * every, "op": op, "order": (idx, i)}
            if op == "repack":
                e["tape"] = tape
            else:
                e["file"] = _expand_name(pattern, i, where)
            if size is not None:
                e["size_mb"] = size
            events.append(e)
    if uses_repack and lib["drives"] < 2:
        raise ScenarioError("library.drives: repack copies tape to tape and needs at least 2 drives")
    events.sort(key=lambda e: (e["t"], e["order"]))
    sc["events"] = events
    return sc


def load_scenario(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except OSError as exc:
        raise ScenarioError(f"{path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML: {exc}") from None
    return parse_scenario(doc)


# --------------------------------------------------------------------------
# model

class File:
    def __init__(self, name):
        self.name = name
        self.size = 0
        self.state = RESIDENT
        self.version = 0
        self.mtime = 0
        self.atime = 0
        self.seg = None          # live Segment holding this version, if any
        self.migrating = False
        self.recall = None       # None, "queued" or "running"
        self.waiters = []        # request times of reads waiting on a recall


class Segment:
    def __init__(self, tape, pos, f, version, size):
        self.tape = tape
        self.pos = pos           # MB offset from the start of the tape
        self.file = f
        self.version = version
        self.size = size
        self.live = False


class Tape:
    def __init__(self, tid, capacity):
        self.id = tid
        self.capacity = capacity
        self.state = SCRATCH
        self.used = 0
        self.live = 0
        self.segments = []
        self.busy = False
        self.repack_target = False
        self.mounts = 0

    @property
    def free(self):
        return self.capacity - self.used


class Drive:
    def __init__(self, n):
        self.id = f"drive{n}"
        self.tape = None
        self.busy = False
        self.mounts = 0
        self.busy_s = 0


class Job:
    def __init__(self, kind, seq, **kw):
        self.kind = kind
        self.seq = seq
        self.blocked = None
        self.__dict__.update(kw)


def fmt_t(t):
    return f"{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}"


def ceil_div(a, b):
    return -(-a // b)


def names(items):
    shown = [x.name if hasattr(x, "name") else x for x in items[:MAX_NAMES]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (f" +{more} more" if more else "")


class Sim:
    def __init__(self, sc):
        self.sc = sc
        self.cap = sc["cache"]["capacity_mb"]
        self.high_mb = sc["cache"]["high_watermark"] * self.cap
        self.low_mb = sc["cache"]["low_watermark"] * self.cap
        lib = sc["library"]
        self.lib = lib
        self.policy = sc["policy"]
        self.duration = sc["duration_s"]
        self.tapes = [Tape(tape_id(n), lib["tape_capacity_mb"]) for n in range(1, lib["tapes"] + 1)]
        self.tape_by_id = {t.id: t for t in self.tapes}
        self.drives = [Drive(n) for n in range(lib["drives"])]
        self.files = {}
        self.reserved = 0
        self.draining = False    # inside a high-to-low watermark cycle
        self.queue = []
        self.running = []
        self.heap = []
        self.seq = 0
        self.now = 0
        self.lines = []
        self.peak = 0
        self.st = {
            "writes": 0, "writes_rejected": 0, "deletes": 0,
            "reads": 0, "hits": 0, "misses": 0, "read_errors": 0,
            "latencies": [], "migrated_mb": 0, "migrated_files": 0,
            "stale_copies": 0, "recalled_mb": 0, "recalled_files": 0,
            "purged_mb": 0, "purged_files": 0, "repacks": 0, "repacked_mb": 0,
            "reclaimed": 0, "pressure": 0,
        }

    # ---- plumbing

    def _next_seq(self):
        self.seq += 1
        return self.seq

    def push(self, t, cls, kind, payload):
        heapq.heappush(self.heap, (t, cls, self._next_seq(), kind, payload))

    def log(self, kind, msg):
        self.lines.append(f"{fmt_t(self.now)}  {kind:<9} {msg}")

    @property
    def used(self):
        return self.reserved + sum(f.size for f in self.files.values() if f.state != MIGRATED)

    def cache_str(self):
        return f"cache {self.used}/{self.cap} MB"

    def check(self):
        used = self.used
        assert 0 <= used <= self.cap, f"cache {used}/{self.cap}"
        self.peak = max(self.peak, used)
        for t in self.tapes:
            live = sum(s.size for s in t.segments if s.live)
            assert live == t.live <= t.used <= t.capacity, t.id
        for f in self.files.values():
            assert (f.seg is not None) == (f.state != RESIDENT), f.name
            assert f.seg is None or (f.seg.live and f.seg.file is f), f.name

    # ---- main loop

    def run(self):
        for ev in self.sc["events"]:
            self.push(ev["t"], EV_SCENARIO, ev["op"], ev)
        if self.policy["scan_interval_s"] < self.duration:
            self.push(self.policy["scan_interval_s"], EV_SCAN, "scan", None)
        while self.heap and self.heap[0][0] <= self.duration:
            t, _cls, _seq, kind, payload = heapq.heappop(self.heap)
            self.now = t
            getattr(self, "on_" + kind)(payload)
            self.dispatch()
            self.check()
        self.now = self.duration
        return self

    # ---- scenario events

    def on_write(self, ev):
        name, size = ev["file"], ev["size_mb"]
        self.st["writes"] += 1
        f = self.files.get(name)
        old = f.size if f and f.state != MIGRATED else 0
        need = size - old
        if need > self.cap - self.used:
            self.purge(self.cap - need, f"room for {name}", keep=f)
        if need > self.cap - self.used:
            self.st["writes_rejected"] += 1
            self.log("write", f"{name} {size} MB REJECTED: cache full, nothing purgeable "
                              f"({self.cache_str()})")
            return
        how = "new"
        if f is None:
            f = self.files[name] = File(name)
        else:
            how = "overwrite"
            self.drop_tape_copy(f)
            f.version += 1
        f.size, f.state, f.mtime, f.atime = size, RESIDENT, self.now, self.now
        self.log("write", f"{name} {size} MB ({how}) {self.cache_str()}")
        if f.waiters:
            self.serve_waiters(f, "overwritten before recall finished")
        if f.recall == "queued":
            self.cancel_recall(f)
        f.recall = None  # a recall already running will find the version changed
        self.check_watermark()

    def on_read(self, ev):
        name = ev["file"]
        self.st["reads"] += 1
        f = self.files.get(name)
        if f is None:
            self.st["read_errors"] += 1
            self.log("read", f"{name} ERROR: no such file")
            return
        f.atime = self.now
        if f.state != MIGRATED:
            self.st["hits"] += 1
            self.log("read", f"{name} hit ({f.state})")
            return
        self.st["misses"] += 1
        f.waiters.append(self.now)
        if f.recall:
            self.log("read", f"{name} miss, joins the {f.recall} recall")
            return
        f.recall = "queued"
        self.queue.append(Job("recall", self._next_seq(), file=f))
        self.log("read", f"{name} miss: stub only, recall queued from {f.seg.tape.id}")

    def on_delete(self, ev):
        name = ev["file"]
        f = self.files.pop(name, None)
        if f is None:
            self.log("delete", f"{name} ERROR: no such file")
            return
        self.st["deletes"] += 1
        had_tape = f.seg is not None
        if had_tape:
            tape = f.seg.tape.id
        self.drop_tape_copy(f)
        if f.recall == "queued":
            self.cancel_recall(f)
        for _ in f.waiters:
            self.st["read_errors"] += 1
        tail = f"; {len(f.waiters)} waiting read(s) fail" if f.waiters else ""
        f.waiters = []
        dead = f", tape copy on {tape} is now dead space" if had_tape else ""
        self.log("delete", f"{name}{dead}{tail}; {self.cache_str()}")

    def on_repack(self, ev):
        tape = self.tape_by_id[ev["tape"]]
        if tape.state == SCRATCH:
            self.log("repack", f"{tape.id} requested but it is a scratch tape, ignored")
        elif any(j.kind == "repack" and j.tape is tape for j in self.queue + self.running):
            self.log("repack", f"{tape.id} requested, already scheduled")
        else:
            self.queue_repack(tape, "requested")

    # ---- policy scan

    def on_scan(self, _):
        nxt = self.now + self.policy["scan_interval_s"]
        if nxt < self.duration:  # a scan at the very end could start nothing that finishes
            self.push(nxt, EV_SCAN, "scan", None)
        for tape in self.tapes:
            if tape.state == FULL and tape.live == 0 and not tape.busy and not self.repack_pending(tape):
                self.reclaim(tape, "no live data left")
        threshold = self.policy["repack_below"]
        if threshold is not None:
            for tape in self.tapes:
                if (tape.state == FULL and not tape.busy and tape.used
                        and tape.live < threshold * tape.used and not self.repack_pending(tape)):
                    pct = 100 * tape.live // tape.used
                    self.queue_repack(tape, f"live {pct}% < {threshold:.0%}")
        if self.migration_candidates(False) and not self.migration_pending():
            n = len(self.migration_candidates(False))
            self.queue.append(Job("migrate", self._next_seq(), pressure=False))
            self.log("scan", f"{n} file(s) at least {self.policy['migrate_after_s']} s old, "
                             f"migration queued")
        self.check_watermark()

    def repack_pending(self, tape):
        return any(j.kind == "repack" and j.tape is tape for j in self.queue + self.running)

    def migration_pending(self):
        return [j for j in self.queue + self.running if j.kind == "migrate"]

    def migration_candidates(self, pressure):
        age = self.policy["migrate_after_s"]
        out = [f for f in self.files.values()
               if f.state == RESIDENT and not f.migrating and (pressure or self.now - f.mtime >= age)]
        return sorted(out, key=lambda f: (f.mtime, f.name))

    # ---- cache space

    def purge(self, target, why, keep=None):
        """Release premigrated disk copies, least recently used first, until used <= target."""
        victims = sorted((f for f in self.files.values() if f.state == PREMIGRATED and f is not keep),
                         key=lambda f: (f.atime, f.name))
        used, freed, done = self.used, 0, []
        for f in victims:
            if used <= target:
                break
            f.state = MIGRATED
            used -= f.size
            freed += f.size
            done.append(f)
        if done:
            self.st["purged_files"] += len(done)
            self.st["purged_mb"] += freed
            self.log("purge", f"{len(done)} file(s) {freed} MB for {why}: {names(done)} "
                              f"-> stubs, {self.cache_str()}")

    def check_watermark(self):
        """Two-watermark hysteresis: crossing high starts a cycle that runs until low.

        During the cycle, premigrated copies are purged; if that is not enough,
        resident files are migrated early (ignoring age) so they can be purged too.
        """
        if self.used > self.high_mb and not self.draining:
            self.draining = True
        if not self.draining:
            return
        low = self.sc["cache"]["low_watermark"]
        self.purge(self.low_mb, f"watermark cycle (down to {low:.0%})")
        if self.used <= self.low_mb:
            self.draining = False
            return
        pending = self.migration_pending()
        if not pending:
            if self.migration_candidates(True):
                self.st["pressure"] += 1
                self.queue.append(Job("migrate", self._next_seq(), pressure=True))
                self.log("pressure", f"{self.cache_str()} still above low watermark after "
                                     f"purging: migrating early, ignoring age")
        else:
            for j in pending:
                if j in self.queue and not j.pressure:
                    j.pressure = True
                    self.st["pressure"] += 1
                    self.log("pressure", "queued migration now ignores age")

    def drop_tape_copy(self, f):
        seg = f.seg
        if seg is not None:
            seg.live = False
            seg.tape.live -= seg.size
            f.seg = None

    def serve_waiters(self, f, how):
        for t in f.waiters:
            self.st["latencies"].append(self.now - t)
        self.log("served", f"{f.name}: {len(f.waiters)} read(s) {how}")
        f.waiters = []

    def cancel_recall(self, f):
        self.queue = [j for j in self.queue if not (j.kind == "recall" and j.file is f)]
        f.recall = None

    # ---- jobs

    def queue_repack(self, tape, why):
        self.queue.append(Job("repack", self._next_seq(), tape=tape))
        self.log("repack", f"{tape.id} queued ({why}): {tape.live}/{tape.used} MB live")

    def reclaim(self, tape, why):
        tape.state, tape.used, tape.live, tape.segments = SCRATCH, 0, 0, []
        self.st["reclaimed"] += 1
        self.log("reclaim", f"{tape.id} -> scratch ({why})")

    def pick_drives(self, tapes):
        """Choose an idle drive for each tape, preferring one that already holds it."""
        idle = [d for d in self.drives if not d.busy]
        chosen = []
        for tape in tapes:
            pool = [d for d in idle if d not in chosen]
            pick = (next((d for d in pool if d.tape is tape), None)
                    or next((d for d in pool if d.tape is None), None)
                    or next((d for d in pool if d.tape not in tapes), None))
            if pick is None:
                return None
            chosen.append(pick)
        return chosen

    def mount(self, drive, tape):
        """Put tape in drive; return the seconds it costs (unload + load, as one figure)."""
        if drive.tape is tape:
            return 0
        for d in self.drives:
            if d.tape is tape:
                d.tape = None
        drive.tape = tape
        drive.mounts += 1
        tape.mounts += 1
        return self.lib["mount_s"]

    def block(self, job, reason):
        if job.blocked != reason:
            job.blocked = reason
            self.log("wait", f"{job.kind} {reason}")
        return False

    def start(self, job, drives, tapes, secs, what):
        for d in drives:
            d.busy = True
            d.busy_s += min(secs, self.duration - self.now)
        for t in tapes:
            t.busy = True
        job.drives, job.tapes, job.started = drives, tapes, self.now
        self.queue.remove(job)
        self.running.append(job)
        self.push(self.now + secs, EV_DONE, "done", job)
        on = "+".join(d.id for d in drives)
        self.log(job.kind, f"start on {on}: {what}, {secs} s, done at {fmt_t(self.now + secs)}")
        return True

    def dispatch(self):
        progressed = True
        while progressed:
            progressed = False
            for job in sorted(self.queue, key=lambda j: (PRIORITY[j.kind], j.seq)):
                if getattr(self, "try_" + job.kind)(job):
                    progressed = True
                    break

    def try_recall(self, job):
        # Look the tape up now: a repack may have moved the file since the read.
        tape = job.file.seg.tape
        if tape.busy:
            return self.block(job, f"{job.file.name}: {tape.id} is in use")
        group = sorted((j for j in self.queue if j.kind == "recall" and j.file.seg.tape is tape),
                       key=lambda j: j.file.seg.pos)
        # Check for a drive first, so a recall that cannot start yet purges nothing.
        drives = self.pick_drives([tape])
        if drives is None:
            return self.block(job, f"{job.file.name}: no free drive")
        total = sum(j.file.size for j in group)
        if total > self.cap - self.used:
            self.purge(self.cap - total, f"recall from {tape.id}")
        room, take = self.cap - self.used, []
        for j in group:
            if j.file.size <= room:
                take.append(j)
                room -= j.file.size
        if not take:
            return self.block(job, f"{job.file.name}: no cache space to recall into")
        secs = self.mount(drives[0], tape)
        # The first recall in tape order carries the whole batch; the rest leave the queue.
        lead = take[0]
        for j in take[1:]:
            self.queue.remove(j)
        files = [j.file for j in take]
        for f in files:
            f.recall = "running"
            secs += self.lib["seek_s"] + ceil_div(f.size, self.lib["rate_mb_s"])
        mb = sum(f.size for f in files)
        self.reserved += mb
        lead.files = [(f, f.version) for f in files]
        lead.mb, lead.tape = mb, tape
        return self.start(lead, drives, [tape], secs,
                          f"{len(files)} file(s) {mb} MB from {tape.id} in tape order: {names(files)}")

    def try_migrate(self, job):
        if any(j.kind == "migrate" for j in self.running):
            return False
        cands = self.migration_candidates(job.pressure)
        if not cands:
            self.queue.remove(job)
            return True
        first = cands[0].size
        open_tapes = [t for t in self.tapes if t.state == FILLING]
        for t in open_tapes:
            if t.free < first and not t.busy:
                t.state = FULL
                self.log("tape", f"{t.id} closed: {t.free} MB free < next file {first} MB")
        fits = [t for t in self.tapes if t.state == FILLING and t.free >= first and not t.repack_target]
        idle = [t for t in fits if not t.busy]
        if fits and not idle:
            return self.block(job, f"open tape {fits[0].id} is in use")
        if idle:
            tape = idle[0]
        else:
            scratch = [t for t in self.tapes if t.state == SCRATCH and not t.busy]
            if not scratch:
                return self.block(job, "stalled: no scratch tape left")
            tape = scratch[0]
        drives = self.pick_drives([tape])
        if drives is None:
            return self.block(job, "no free drive")
        if tape.state == SCRATCH:
            tape.state = FILLING
        take, room = [], tape.free
        if self.policy["batch_mb"] is not None:
            room = min(room, max(self.policy["batch_mb"], first))
        for f in cands:
            if f.size > room:
                break
            take.append(f)
            room -= f.size
        secs = self.mount(drives[0], tape) + self.lib["seek_s"]
        job.files = []
        for f in take:
            seg = Segment(tape, tape.used, f, f.version, f.size)
            tape.used += f.size
            tape.segments.append(seg)
            f.migrating = True
            job.files.append((f, seg))
        mb = sum(f.size for f in take)
        secs += ceil_div(mb, self.lib["rate_mb_s"])
        job.mb = mb
        why = "early, cache pressure" if job.pressure else "aged"
        return self.start(job, drives, [tape], secs,
                          f"{len(take)} file(s) {mb} MB -> {tape.id} ({why}): {names(take)}")

    def try_repack(self, job):
        src = job.tape
        if src.busy:
            return self.block(job, f"{src.id} is in use")
        live = [s for s in src.segments if s.live]
        if not live:
            self.queue.remove(job)
            self.reclaim(src, "repack found no live data")
            return True
        scratch = [t for t in self.tapes if t.state == SCRATCH and not t.busy]
        if not scratch:
            return self.block(job, f"{src.id}: no scratch tape to copy onto")
        dst = scratch[0]
        drives = self.pick_drives([src, dst])
        if drives is None:
            return self.block(job, f"{src.id}: needs 2 free drives")
        # Both cartridges load in parallel; the copy then streams source to target.
        secs = max(self.mount(drives[0], src), self.mount(drives[1], dst))
        mb = sum(s.size for s in live)
        secs += len(live) * self.lib["seek_s"] + ceil_div(mb, self.lib["rate_mb_s"])
        src.state = FULL
        dst.state = FILLING
        dst.repack_target = True
        job.segs, job.dst, job.mb = live, dst, mb
        return self.start(job, drives, [src, dst], secs,
                          f"{src.id} -> {dst.id}, {len(live)} live file(s) {mb} MB "
                          f"of {src.used} MB used")

    def on_done(self, job):
        self.running.remove(job)
        for d in job.drives:
            d.busy = False
        for t in job.tapes:
            t.busy = False
        getattr(self, "done_" + job.kind)(job)
        self.check_watermark()
        if job.kind == "migrate":
            self.follow_up_migrate(job)

    def done_recall(self, job):
        self.reserved -= job.mb
        ok, stale = [], []
        for f, version in job.files:
            if self.files.get(f.name) is f and f.version == version and f.state == MIGRATED:
                f.state = PREMIGRATED
                f.atime = self.now
                f.recall = None
                ok.append(f)
            else:
                stale.append(f)
        self.st["recalled_files"] += len(ok)
        self.st["recalled_mb"] += sum(f.size for f in ok)
        tail = f"; {len(stale)} discarded (changed meanwhile)" if stale else ""
        self.log("recall", f"done from {job.tape.id}: {len(ok)} file(s) back on disk{tail} "
                           f"{self.cache_str()}")
        for f in ok:
            waits = ", ".join(f"{self.now - t} s" for t in f.waiters)
            self.serve_waiters(f, f"served after waiting {waits}")

    def done_migrate(self, job):
        tape = job.tapes[0]
        ok, stale = [], 0
        for f, seg in job.files:
            if self.files.get(f.name) is f:
                f.migrating = False
            if self.files.get(f.name) is f and f.version == seg.version and f.state == RESIDENT:
                seg.live = True
                tape.live += seg.size
                f.seg = seg
                f.state = PREMIGRATED
                ok.append(f)
            else:
                stale += 1
        self.st["migrated_files"] += len(ok)
        self.st["migrated_mb"] += sum(f.size for f in ok)
        self.st["stale_copies"] += stale
        tail = f"; {stale} copy(ies) already stale (file changed or deleted)" if stale else ""
        self.log("migrate", f"done -> {tape.id}: {len(ok)} file(s) premigrated{tail} "
                            f"[{tape.id} {tape.used}/{tape.capacity} MB]")
        if tape.free == 0:
            tape.state = FULL
            self.log("tape", f"{tape.id} full")

    def follow_up_migrate(self, job):
        # Keep going while there is work; pressure only carries over while the
        # watermark cycle is still running after the purge this batch made possible.
        pressure = job.pressure and self.draining
        if self.migration_candidates(pressure) and not self.migration_pending():
            self.queue.append(Job("migrate", self._next_seq(), pressure=pressure))

    def done_repack(self, job):
        src, dst = job.tape, job.dst
        dst.repack_target = False
        moved = dead = 0
        for seg in job.segs:
            new = Segment(dst, dst.used, seg.file, seg.version, seg.size)
            dst.used += seg.size
            dst.segments.append(new)
            if seg.live:
                new.live = True
                dst.live += seg.size
                seg.file.seg = new
                moved += seg.size
            else:
                dead += seg.size
        self.st["repacks"] += 1
        self.st["repacked_mb"] += moved
        tail = f" ({dead} MB died during the copy)" if dead else ""
        self.log("repack", f"done: {moved} MB live now on {dst.id}{tail}")
        self.reclaim(src, f"repacked onto {dst.id}")
        if dst.free == 0:
            dst.state = FULL

    # ---- report

    def report(self, stats_only=False):
        sc, st = self.sc, self.st
        out = [f"# hsm-sim {__version__}: {sc['name']}"]
        if sc["description"]:
            out += [f"# {line}".rstrip() for line in sc["description"].splitlines()]
        c, lib, p = sc["cache"], self.lib, self.policy
        out.append(f"# cache {c['capacity_mb']} MB, watermarks {c['high_watermark']:.0%} high / "
                   f"{c['low_watermark']:.0%} low")
        out.append(f"# {lib['drives']} drive(s), {lib['tapes']} tape(s) x {lib['tape_capacity_mb']} MB, "
                   f"mount {lib['mount_s']} s, seek {lib['seek_s']} s, {lib['rate_mb_s']} MB/s")
        repack = "off" if p["repack_below"] is None else f"below {p['repack_below']:.0%} live"
        batch = "" if p["batch_mb"] is None else f" in batches of {p['batch_mb']} MB"
        out.append(f"# scan every {p['scan_interval_s']} s, migrate after {p['migrate_after_s']} s"
                   f"{batch}, auto-repack {repack}")
        if not stats_only:
            out += ["", "== timeline =="] + self.lines
        out += ["", "== stats =="]
        lat = st["latencies"]
        waiting = sum(len(f.waiters) for f in self.files.values())
        reads = st["reads"]
        ratio = f", hit ratio {100 * st['hits'] / reads:.1f}%" if reads else ""
        rows = [
            ("simulated", f"{self.duration} s ({fmt_t(self.duration)})"),
            ("writes", f"{st['writes']} ({st['writes_rejected']} rejected), deletes {st['deletes']}"),
            ("reads", f"{reads}: {st['hits']} hit, {st['misses']} miss, "
                      f"{st['read_errors']} error{ratio}"),
        ]
        if lat:
            srt = sorted(lat)
            rows.append(("recall wait", f"n={len(lat)} mean {sum(lat) / len(lat):.1f} s, "
                                        f"median {srt[(len(srt) - 1) // 2]} s, max {srt[-1]} s"))
        if waiting:
            rows.append(("still waiting", f"{waiting} read(s) unserved at end"))
        rows += [
            ("migrated", f"{st['migrated_files']} file(s) {st['migrated_mb']} MB"
                         + (f", {st['stale_copies']} stale copy(ies)" if st["stale_copies"] else "")
                         + (f", {st['pressure']} early batch(es) under cache pressure" if st["pressure"] else "")),
            ("purged", f"{st['purged_files']} file(s) {st['purged_mb']} MB"),
            ("recalled", f"{st['recalled_files']} file(s) {st['recalled_mb']} MB"),
            ("repacked", f"{st['repacks']} tape(s), {st['repacked_mb']} MB copied; "
                         f"{st['reclaimed']} tape(s) returned to scratch"),
            ("cache", f"peak {self.peak}/{self.cap} MB, end {self.used}/{self.cap} MB"),
        ]
        states = {RESIDENT: 0, PREMIGRATED: 0, MIGRATED: 0}
        for f in self.files.values():
            states[f.state] += 1
        rows.append(("files", ", ".join(f"{v} {k}" for k, v in states.items())))
        if self.running or self.queue:
            busy = [j.kind for j in sorted(self.running + self.queue, key=lambda j: j.seq)]
            rows.append(("unfinished", f"{len(self.running)} running, {len(self.queue)} queued: "
                                       f"{', '.join(busy)}"))
        width = max(len(k) for k, _ in rows)
        out += [f"{k:<{width}}  {v}" for k, v in rows]
        out += ["", "drive    mounts  busy s  util"]
        for d in self.drives:
            out.append(f"{d.id:<7}  {d.mounts:>6}  {d.busy_s:>6}  {100 * d.busy_s / self.duration:>3.0f}%")
        out += ["", "tape  state     used MB  live MB  live  mounts"]
        for t in self.tapes:
            pct = f"{100 * t.live // t.used}%" if t.used else "-"
            out.append(f"{t.id:<4}  {t.state:<8}  {t.used:>7}  {t.live:>7}  {pct:>4}  {t.mounts:>6}")
        return "\n".join(out) + "\n"


def simulate(sc, stats_only=False):
    return Sim(sc).run().report(stats_only)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hsm-sim", description=__doc__.splitlines()[0])
    ap.add_argument("scenario", help="YAML scenario file")
    ap.add_argument("--stats-only", action="store_true", help="print the header and stats, no timeline")
    ap.add_argument("--version", action="version", version=f"hsm-sim {__version__}")
    args = ap.parse_args(argv)
    try:
        sc = load_scenario(args.scenario)
    except ScenarioError as exc:
        print(f"hsm-sim: error: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(simulate(sc, args.stats_only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
