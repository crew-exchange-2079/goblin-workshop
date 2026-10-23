# mhvtl scenarios for lto-exporter

Scripted [mhvtl](https://github.com/markh794/mhvtl) scenarios that put one emulated
LTO-8 drive through media states, with assertions on the
[tape-exporter](../../tape-exporter/) metrics after each step. It complements
`tape-exporter/ci/mhvtl/run_integration.sh`, which proves the exporter's
read-only command path against an *empty* drive. Here the drive has media, moves
tape, fills up, cleans, goes offline and refuses WORM overwrites.

Everything that changes drive state is the **harness**, running as root:
`mktape`, `vtlcmd <drive> load|unload`, `sg_raw` SSC commands (WRITE(6), READ(6),
WRITE FILEMARKS, REWIND, SPACE to EOD, TEST UNIT READY), the SCSI device's
sysfs `state`, and reads of the TapeAlert page. The **exporter** runs as an
unprivileged user with read access to that one sg node, under `strace`, and is
only scraped. The final verdict also runs tape-exporter's strace checker over the
whole run: through all scenarios the exporter may only have exec'd its two
`sg_logs` argv shapes and sent INQUIRY / LOG SENSE of pages 0x02, 0x03, 0x0c.

## Runner requirement

`run.sh` needs a **disposable Linux VM** that can build and load an out-of-tree
kernel module:

- Linux, root or passwordless `sudo`;
- not a container (`systemd-detect-virt --container` = `none`, no `/.dockerenv`);
- `kernel.modules_disabled=0` and kernel lockdown off (an unsigned `mhvtl.ko` is
  refused under `integrity`/`confidentiality` lockdown, e.g. Secure Boot);
- headers for the running kernel (`/lib/modules/$(uname -r)/build`, or an
  installable `linux-headers-$(uname -r)` package);
- at run time: `sg3-utils` (`sg_logs`, `sg_raw`, `sg_turs`), `strace`, `curl`,
  `python3`, and a Python venv with lto-exporter installed.

GitHub-hosted `ubuntu-24.04` runners meet all of this; the job runs there. The
module, emulated drive and media are torn down at the end, but the run is not
meant to be clean on a host you keep: it refuses to start outside CI unless
`LTO_MHVTL_I_UNDERSTAND=yes`.

**Skipping.** [`preflight.sh`](preflight.sh) checks only those static host
properties. If one is missing it prints `skip: <reason>` and exits 77, `run.sh`
exits 77 with a `SKIPPED` summary, and the workflow ends green with a
notice. Once preflight passes, every later problem (insmod refused, the drive
never appears, a harness step or an assertion fails) is a **failure**, because a
runner that should do this and suddenly can't is a regression. CI sets
`MHVTL_SCENARIOS_REQUIRE=1` after its own probe passed, so a skip at run time
also fails there.

## Run

```sh
# on a throwaway VM, with mhvtl built at the pinned commit (see the workflow):
sudo env LTO_MHVTL_I_UNDERSTAND=yes MHVTL_SRC=/path/to/mhvtl \
  LTO_VENV=/path/to/venv EVIDENCE=/tmp/evidence ./run.sh
sudo env ... SCENARIOS="full_tape worm" ./run.sh     # subset; baseline always runs
python3 -m pytest -q test_scenarios.py             # anywhere: fake drive, real collector
```

`.github/workflows/mhvtl-scenarios.yml` runs on pushes touching this directory,
`tape-exporter/lto_exporter/` or `tape-exporter/ci/mhvtl/`, and by hand
(`workflow_dispatch`, optional `scenarios` subset). The evidence directory
(per-step metrics, the harness's own page reads, every harness command and its
output, `vtltape.log`, strace, `summary.json`/`summary.md`) is uploaded as the
`mhvtl-scenarios-evidence` artifact. mhvtl is pinned to the same commit as the
tape-exporter integration job.

## What each scenario asserts

After **every** step: the exporter's status for each collector, its values, and
its `collector_success` must equal what the harness gets from running the
exporter's exact `sg_logs` argv itself, right after the scrape. When that read
fails, the exporter must export **no** counter, flag or `*_present` series for
the page (unknown is not zero). `tape_exporter_unreaped_children` must be 0.

On top of that, per step (bytes are the four `tape_data_bytes_total` series;
unnamed ones must be 0; "media > 0" because mhvtl compresses with LZO):

| scenario | steps and expected metrics |
|---|---|
| `baseline` | empty drive: all collectors `ok`, bytes 0, `tape_cleaning_required` 0, error counters present and 0 |
| `load_unload` | load: bytes 0 · 3×64 KiB WRITE + filemark: host-write = 196608, media-write > 0 · rewind + 3 READ: host-read = 196608, media-read > 0, write side unchanged · **unload: all four unchanged** · **reload: all four back to 0** |
| `full_tape` | 4 MiB cartridge, 64 KiB WRITEs until refused. Harness must see plain accepts, then early-warning accepts (NO SENSE + EOM), then VOLUME OVERFLOW. Exporter: host-write = accepted × 64 KiB exactly; a further refused WRITE doesn't change it; collectors stay `ok` at end of medium |
| `cleaning_cartridge` | after a host TapeAlert read: cleaning 0 · cleaning cart mounted: **cleaning 1**, bytes 0, collectors `ok`; a WRITE is refused NOT READY · two more scrapes: still 1 (the exporter's reads don't clear it) · unload: **still 1** · host reads TapeAlert page 0x2E: **0** |
| `drive_offline` | data cart, one WRITE: host-write = 65536 · sysfs `state=offline`: every collector **`error`**, success 0, **no** byte/cleaning/error/present series, twice · `state=running`: `ok` again, host-write still 65536 |
| `worm` | blank WORM cart: 2 WRITEs + filemark accepted, host-write = 131072 · rewind, WRITE at BOT refused **DATA PROTECT**, host-write unchanged · SPACE to EOD, WRITE accepted: host-write = 196608 |

### Why these numbers (mhvtl at the pinned commit)

All of this is emulator behaviour, read from mhvtl's source; it says nothing
about real drives.

- **Byte counters are per load.** `loadTape()` zeroes `bytesWritten_I/_M` and
  `bytesRead_I/_M`; unload doesn't touch them. They feed parameters 0–3 of page
  0x0c (`update_SequentialAccessDevice()`). So `tape_data_bytes_total` drops to 0
  on every load, which Prometheus sees as a counter reset.
- **Early warning / overflow.** `writeBlock()` accepts a WRITE past
  `capacity − 2 MiB` with NO SENSE + EOM and counts it; at or past capacity it
  returns VOLUME OVERFLOW *before* writing, so the refused WRITE is not counted.
- **Cleaning flag.** mhvtl puts its whole 64-bit TapeAlert word into page 0x0c
  parameter 0x0100, and sg_logs prints "Cleaning action required" for any nonzero
  value. Mounting a cleaning cart sets TapeAlert `TA_CLEANING_MEDIA`.
  `update_TapeAlert()` ORs flags in; only a full-length read of page 0x2E
  (`ssc_log_sense()` → `set_TapeAlert(TA_NONE)`) clears them. That makes this an
  end-to-end check of the exporter's rule never to read 0x2E: if it did, the
  flag would vanish on the first rescrape.
- **WORM.** `rewind_tape()` makes a WORM cart non-writable unless it is blank;
  `check_restrictions()` refuses the WRITE with DATA PROTECT / medium overwrite
  attempted before anything is counted; SPACE to EOD makes it writable again.
- **Error counter pages 0x02/0x03 are static zeros** in mhvtl (never updated),
  so their assertions only cover parsing and presence, not counting.
- **Offline** is the SCSI midlayer's state, as after failed error recovery.
  sg3_utils opens the node `O_NONBLOCK`, and `sg_open()` only refuses an offline
  device on a *blocking* open, so the open succeeds. The midlayer then rejects
  the LOG SENSE, and sg_logs exits with a code outside its documented table, which
  the exporter maps to `error`. The driver prints the exact exit codes into the job
  log. The exporter's per-scrape device re-check still passes (same node, still
  type 1), so the status comes from sg_logs alone. A distinct `offline` status
  would need the exporter to read sysfs `state`; that is out of scope here.

`test_scenarios.py` runs every scenario against a small fake of exactly these
behaviours, scraped by the real `lto_exporter` collector. It also checks that the
assertions fail when the exporter caches stale data, reports zeros for a failed
read, or drops the cleaning line, and when the emulator semantics drift (counters
reset on unload, TapeAlert cleared on unload, WORM overwrite allowed).

## Files

| file | role |
|---|---|
| [`run.sh`](run.sh) | preflight, module + drive + exporter setup, scenario run, verdict, teardown |
| [`preflight.sh`](preflight.sh) | can this host do it? exit 0 or 77 + reason |
| [`scenarios.py`](scenarios.py) | harness actions, scenarios, assertions, verdict (stdlib + tape-exporter's parser and `ci/mhvtl/check_evidence.py`) |
| [`device.conf.in`](device.conf.in) | one library entry (media home only, no library daemon) and one IBM ULT3580-TD8 with a synthetic serial |
| [`test_scenarios.py`](test_scenarios.py) | fake-drive tests, run in `ci.yml` |

mhvtl is GPL-2.0-or-later by Mark Harvey and contributors. Nothing from it is
copied here; CI builds upstream at the pinned commit.
