# hsm-sim — a teaching simulator of a disk cache in front of a tape archive

A small, deterministic command-line simulator of hierarchical storage management
(HSM): files land on a disk cache, get copied to tape, lose their disk copy when the
cache fills, and come back from tape when someone reads them. You describe a workload
in a YAML scenario; it prints a timestamped timeline of everything the system did and
a block of statistics.

It is a **teaching tool, not a model of any product**. It covers the generic ideas
most disk+tape archives share (migration, watermarks, purge, recall, repack) and
nothing vendor-specific: no real on-tape format, no real policy language, no real
scheduler. Every cost in the examples is invented and illustrative, not measured
against any drive, library or software.

**Reproduce:** `pip install -r requirements.txt pytest && python -m pytest -q tests`
runs 47 tests, including a byte-for-byte comparison of all five examples with their
golden outputs in `examples/*.out`.

```console
$ python3 hsm_sim.py examples/01-lifecycle.yaml
...
00:20:00  write     delta.dat 300 MB (new) cache 900/1000 MB
00:20:00  purge     2 file(s) 400 MB for watermark cycle (down to 50%): beta.dat, gamma.dat -> stubs, cache 500/1000 MB
00:30:00  read      beta.dat miss: stub only, recall queued from T01
00:30:00  recall    start on drive0: 1 file(s) 200 MB from T01 in tape order: beta.dat, 22 s, done at 00:30:22
...
```

## What is here

| File | What it is |
|---|---|
| `hsm_sim.py` | the whole simulator and CLI, one file; needs only PyYAML |
| `examples/*.yaml` | five scenarios, each built to show one idea (below) |
| `examples/*.out` | their exact output; the tests compare against these |
| `tests/test_hsm_sim.py` | golden-output, validation and behaviour tests (pytest) |
| `requirements.txt` | PyYAML, pinned with hashes for the CI platforms |
| `LICENSE` | MIT |

## Running it

Python 3.12 or newer and PyYAML:

```sh
pip install -r requirements.txt          # or: pip install pyyaml==6.0.3
python3 hsm_sim.py examples/03-recall-storm.yaml
python3 hsm_sim.py --stats-only examples/05-contention.yaml
```

A scenario that fails validation prints `hsm-sim: error: <where>: <what>` to stderr
and exits with status 2. Same scenario in, same bytes out: there is no randomness,
no wall clock and no dependence on dict or hash ordering.

## The five examples

| Scenario | What to look for |
|---|---|
| `01-lifecycle` | one file through every state: resident → premigrated → stub → recalled; a second read is a hit |
| `02-watermarks` | a burst written faster than tape can absorb it: the cache fills with resident data that cannot be purged, so writes are rejected until early ("pressure") migration lands batches that can |
| `03-recall-storm` | twelve reads hopping between three tapes on two drives: queued recalls for one tape share a mount and run in tape order; mounts and seeks, not transfer, make up almost all of the wait; a too-tight watermark purges freshly recalled files again |
| `04-repack` | deletes and overwrites leave dead copies on tape; auto-repack copies the live 40 % of T01 to another tape and frees T01; T02, with nothing live, is reclaimed without a mount; an overwrite during migration produces a copy that is stale on arrival |
| `05-contention` | recalls, migration and repack on two drives: repack waits for both drives to be free at once, then holds them, and a read that arrives meanwhile waits 310 s instead of 32 s |

## The model

**File states.** *resident*: on disk only (not yet safe). *premigrated*: on disk and
on tape, so its disk copy can be released at any moment. *migrated*: on tape only; a
stub remains on disk and a read of it must wait for a recall. Writing to an existing
file makes it resident again and turns its old tape copy into dead space; deleting a
file does the same.

**Migration.** Every `scan_interval_s` a policy scan queues migration of resident files
at least `migrate_after_s` old, oldest first. One migration stream runs at a time and
appends to an open ("filling") tape, or takes a scratch tape when no open tape has
room for the next file (closing the ones that don't). With `batch_mb`, each job
copies at most that much, and follow-up jobs continue until nothing eligible is left.
A file that changes while its batch is being written lands on tape already stale.

**Watermarks and purge.** When cache use goes above `high_watermark`, a cycle starts
that runs until use is at or below `low_watermark` (hysteresis: two thresholds so the
system is not purging on every write). In the cycle, premigrated copies are released,
least recently used first. If that is not enough, resident files are migrated early,
ignoring their age, so that they can be purged in turn. A write that does not fit
even after purging is rejected: purge can only free data that is already on tape.

**Recall.** A read of a stub queues a recall. When a drive starts on a tape it takes
every queued recall for that tape, reserves cache space for them, and reads them in
tape order: one mount, one seek per file. A recall that cannot get cache space waits.
Reads of a file already being recalled join that recall.

**Repack.** Dead space on a tape is only recovered by copying its live files to
another tape and returning the cartridge to scratch. Repack runs when a full tape's
live fraction falls below `repack_below`, or on an explicit `repack` event. It
needs two drives at once (read one tape, write a scratch tape). A full tape with no
live data at all is returned to scratch by the scan without any mount.

**Drives and scheduling.** Queued work starts in priority order — recall, then
migrate, then repack, oldest first within a kind — whenever the tapes it needs are
not in use and enough drives are idle. A started job is never preempted. A drive keeps
its tape loaded after a job, so the next job for the same tape skips the mount.

**Costs.** `mount_s` covers unloading the previous cartridge and loading the next.
Migration costs one mount (if needed), one seek, and size ÷ `rate_mb_s`. A recall
costs one mount plus, per file, one seek and its transfer. A repack costs one mount
(both tapes load in parallel), one seek per live file, and the live data's transfer.
Times are whole seconds, rounded up.

**Simplifications, on purpose.** No robot, slots or exchange time; no drive or media
errors; no partial-file recall; one copy per file (no second tape pool); no file
spanning two tapes; seek time is a constant, not a function of distance; same-time
events run in a fixed order (job completions, then scenario events in file order, then
the scan). The simulator checks its own bookkeeping after every event and stops with
an assertion if the cache ever exceeds its capacity or a tape's live data disagrees
with its segments.

## Scenario format

```yaml
name: my-scenario                  # required
description: |                     # optional, printed as the header
  What this scenario is meant to show.
duration_s: 3600                   # simulate [0, duration_s]
cache:
  capacity_mb: 1000
  high_watermark: 0.80             # fractions of capacity; low must be below high
  low_watermark: 0.50
library:
  drives: 2
  tapes: 4                         # named T01, T02, ... (at most 99)
  tape_capacity_mb: 4000
  mount_s: 60
  seek_s: 20
  rate_mb_s: 100
policy:
  scan_interval_s: 300
  migrate_after_s: 600
  batch_mb: 500                    # optional: cap on each migration job
  repack_below: 0.5                # optional: auto-repack full tapes below this live fraction
events:                            # any order; sorted by t, ties keep file order
  - {t: 0, op: write, file: a.dat, size_mb: 200}
  - {t: 0, op: write, file: "log-{i:03d}.txt", size_mb: 5, count: 50, every_s: 10}
  - {t: 900, op: read, file: a.dat}
  - {t: 1200, op: delete, file: a.dat}
  - {t: 1800, op: repack, tape: T01}
```

All sizes are whole MB and all times whole seconds. `count` repeats an event
`every_s` apart, with `{i}` (or `{i:03d}`) in the file name replaced by 0, 1, 2, ….
Unknown keys are errors, not silently ignored. Quote file names YAML would otherwise
read as something else (`file: "no"`, `file: "1.0"`).

## Tests

```sh
python -m pytest -q tests
```

If you change the simulator on purpose and the golden outputs should change, rerun
with `HSM_SIM_UPDATE_GOLDEN=1` and review the diff of `examples/*.out` before
committing it.
