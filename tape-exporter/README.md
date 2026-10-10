# lto-exporter

A small, read-only Prometheus exporter for Linux LTO / SCSI tape drives.
Python 3.10+ standard library only. No cloud services, accounts or telemetry.

It runs exactly one external tool, `sg_logs` from sg3_utils, without a
shell, and parses its text output:

```
sg_logs --readonly --page=0x2 /dev/sgN   # Write Error Counter log page
sg_logs --readonly --page=0x3 /dev/sgN   # Read Error Counter log page
```

**`tapeinfo` (mtx) is never executed** and can't be enabled. See
[Why no tapeinfo](#why-no-tapeinfo).

> Status: MVP, tested only against **synthetic fixtures** — it has not been run
> against real hardware. Treat the first deployment as a trial.

## Safety model

* **Read-only commands only.** Argument vectors are fixed constants plus the
  configured device path (`lto_exporter/commands.py`). `sg_logs` is always run
  with `--readonly` (by default sg_logs tries a read-write open first) and the
  builder refuses `--reset`/`--select`/`--sp` and friends. The exporter never
  issues tape motion, write, erase, load/unload, reset or MODE SELECT.
* **Explicit allowlist, no `/dev` scan.** Up to 16 devices, each given by name
  and path in the config.
* **Only `/dev/sgN` (SCSI generic) nodes are accepted**, directly or through a
  symlink that resolves to one. `/dev/stN` is rejected because the st driver
  *rewinds on close*; `/dev/nstN` is rejected because st nodes are
  exclusive-open, so a scrape could make a backup job fail with `EBUSY`.
* **Bounded execution.** `shell=False`, stdin from `/dev/null`, stderr
  discarded, fixed `PATH`/`LC_ALL=C` environment, per-command timeout (killed
  on expiry) and an output cap (killed when exceeded).
* **Rate limiting.** Results are cached for `cache_seconds` (default 30) and
  collections are serialised, so frequent scrapes don't hammer the drive.
* **Low-cardinality labels.** `device` is your configured alias, never the
  path or serial.
* Device output and errors are never echoed into HTTP responses or logs.

### Why no tapeinfo

An earlier draft had an opt-in `tapeinfo` collector. It was removed:

1. `tapeinfo` reads the TapeAlert log page (0x2E). Depending on the drive and
   its TapeAlert mode, reading that page can **clear** the flags, which could
   hide alerts from backup software. That is a side effect, so it doesn't
   belong in a read-only exporter.
2. mtx's Linux backend opens the device `O_RDWR`, so it needs write
   permission on the sg node.

Enforcement, all covered by tests:
* The config rejects `"collectors": ["tapeinfo"]` and `tapeinfo_path`.
* The command builder refuses any executable whose basename isn't `sg_logs`.
* The exporter has no TapeAlert/tapeinfo metrics.
* The exporter never imports the offline parser below.

**Offline parser (separate, optional).** `offline/tapeinfo_text.py` parses
already-captured `tapeinfo` text from stdin into JSON
(`python3 -m offline.tapeinfo_text < captured.txt`). It runs no commands,
opens no device and emits no metrics. Because `tapeinfo` prints TapeAlert
lines only for set flags, it reports `tapealert_support: "unknown"` and the
listed codes, never a zero count. It never parses the serial number.

## Setup

```sh
# Debian/Ubuntu package names; others vary
sudo apt install sg3-utils          # sg_logs

lsscsi -g                           # find the /dev/sgN for your tape drive
cp example-config.json /etc/lto-exporter.json   # then edit
python3 -m lto_exporter --config /etc/lto-exporter.json
curl -s http://127.0.0.1:9842/metrics
```

Run from this directory, or put it on `PYTHONPATH`. `--listen-address` and
`--port` override the config. Port 9842 is just a default; it isn't registered
anywhere.

## Configuration

```json
{
  "listen_address": "127.0.0.1",
  "port": 9842,
  "command_timeout_seconds": 10,
  "max_output_bytes": 65536,
  "cache_seconds": 30,
  "sg_logs_path": "/usr/bin/sg_logs",
  "devices": [
    {"name": "drive0", "path": "/dev/sg3", "collectors": ["sg_logs"]}
  ]
}
```

| key | default | limits |
|-----|---------|--------|
| `listen_address` | `127.0.0.1` | loopback by default; put a reverse proxy in front if you need TLS/auth |
| `port` | `9842` | 0–65535 (0 = ephemeral) |
| `command_timeout_seconds` | 10 | 0.1–120 |
| `max_output_bytes` | 65536 | 1 KiB–1 MiB |
| `cache_seconds` | 30 | 0–3600 |
| `sg_logs_path` | `/usr/bin/sg_logs` | absolute path, basename must be `sg_logs` |
| `devices[].name` | (required) | `[A-Za-z][A-Za-z0-9_]{0,31}`, unique |
| `devices[].path` | (required) | must be or resolve to `/dev/sgN` |
| `devices[].collectors` | `["sg_logs"]` | only `sg_logs` (`tapeinfo` is rejected) |

Note that `/dev/sgN` numbering can change across reboots. A udev-created
symlink that resolves to the drive's sg node is more stable; the path is
re-resolved only at startup.

## Permissions (no root needed)

Run the exporter as a dedicated unprivileged user. It needs:

* **`sg_logs` collector:** read access to the drive's `/dev/sgN`. sg nodes are
  usually `root:disk` (or `root:tape`) `0660`. Prefer a narrow udev rule that
  gives a dedicated group read-only access to *that drive only*, e.g.
  (illustrative, adapt the match; not installed by this project):

  ```
  # /etc/udev/rules.d/60-lto-exporter.rules
  SUBSYSTEM=="scsi_generic", ATTRS{type}=="1", GROUP="lto-exporter", MODE="0640"
  ```

  `ATTRS{type}=="1"` matches SCSI sequential-access (tape) devices; narrow it
  further with vendor/model attributes if you have several. **Avoid adding the
  user to `disk`**, which grants raw access to every disk.
* Linux filters `SG_IO` on read-only file descriptors for unprivileged callers
  to a list of non-modifying commands. LOG SENSE is expected to pass, but this
  depends on the kernel; if `sg_logs` reports an open/permission error, the
  collector shows `status="open_failed"` or `"error"`. Don't fix it by granting
  `CAP_SYS_RAWIO` or write access unless you've thought through the risk.

This project changes nothing on the system; the rule above is documentation.

## Metrics

Sample output from the synthetic fixtures (`tests/fixtures/`), abridged:

```
tape_exporter_build_info{version="0.1.0"} 1
tape_exporter_collector_success{device="drive0",collector="sg_logs_write_errors"} 1
tape_exporter_collector_duration_seconds{device="drive0",collector="sg_logs_write_errors"} 0.12
tape_exporter_collector_status{device="drive0",collector="sg_logs_write_errors",status="ok"} 1
tape_log_parameter_present{device="drive0",direction="read",parameter="rewrites_or_rereads"} 0
tape_error_counter_total{device="drive0",direction="write",parameter="total_uncorrected"} 0
tape_error_counter_total{device="drive0",direction="write",parameter="corrected_with_delay"} 3
tape_bytes_processed_total{device="drive0",direction="write"} 2199023255553
```

| metric | source |
|--------|--------|
| `tape_exporter_collector_success`, `_duration_seconds`, `_status{status}` | exporter. `status` ∈ ok, unsupported, not_ready, open_failed, device_timeout, timeout, output_too_large, not_found, exec_error, parse_error, error |
| `tape_error_counter_total{direction,parameter}` | Write (0x02) / Read (0x03) Error Counter log page, parameter codes 0–4 and 6 |
| `tape_bytes_processed_total{direction}` | same pages, parameter code 5 |
| `tape_log_parameter_present{direction,parameter}` | 1/0 per known parameter, emitted only when the page was read |

**Missing ≠ zero.** If a collector fails, none of its value series are
emitted, and `collector_success` is 0 with a `status` that tells you why. If a
page is read but a parameter is absent or malformed, its value series is
omitted and `tape_log_parameter_present` is 0. sg_logs exit 5/9 (illegal
request / invalid opcode) maps to `unsupported`.

Counters are whatever the drive reports. They can reset on drive power-cycle,
reset or firmware behaviour, and Prometheus `rate()` handles that. **This
exporter does not estimate remaining tape or head life**, and you shouldn't
derive one from these numbers without vendor-specific guidance.

## Supported output formats and limits

Parsed field names and layouts come from public documentation and source.

* **sg_logs**, part of sg3_utils.
  [sg_logs(8)](https://manpages.debian.org/stable/sg3-utils/sg_logs.8.en.html)
  covers `--page` and `--readonly`.
  [sg3_utils(8)](https://manpages.debian.org/stable/sg3-utils/sg3_utils.8.en.html)
  covers EXIT STATUS (0 ok, 2 not ready, 5 illegal request, 9 invalid opcode,
  15 file error, 33 timeout; also `include/sg_lib.h`). The text layout comes
  from `show_error_counter_page()` in
  [`src/sg_logs.c`](https://github.com/doug-gilbert/sg3_utils/blob/main/src/sg_logs.c):
  header `Write error counter log page  [0x2]`, then lines `  <name> = <n>`,
  optionally suffixed ` [N GB]`/` [N TB]`. Parameter meanings are from T10 SPC
  "Error counter log pages".
* The offline-only tapeinfo parser cites
  [tapeinfo(1)](https://manpages.debian.org/stable/mtx/tapeinfo.1.en.html)
  and `tapeinfo.c` in [mtx 1.3.12](https://sourceforge.net/projects/mtx/).

Limits and tolerance:

* Only the English text output is parsed (forced with `LC_ALL=C`). The
  `--json`/`--hex` modes of newer sg_logs are not used.
* Unknown lines, vendor-specific parameters and unknown header variants are
  ignored. If a page has known parameter lines but no recognised header (an
  unknown sg_logs version), it still parses. If it has neither, the result is
  `parse_error`.
* Values must be decimal and ≤ 2^64−1, otherwise they count as missing.
  Duplicate parameters: first wins. Output is capped at `max_output_bytes`.
* Old sg_logs without `--readonly` exits 1 (syntax error), which shows up as
  `status="error"`. Upgrade sg3_utils rather than dropping the flag.
* No TapeAlert, ready-state, compression or drive-identity metrics. That
  was the cost of removing `tapeinfo`.
* One scrape runs 2 commands per device, sequentially, so worst-case scrape
  time is about `2 × timeout × devices`. Set Prometheus
  `scrape_timeout` accordingly, or keep the cache.
* No TLS/auth. Keep it on loopback or behind a proxy.

## Tests

```sh
cd tape-exporter
python3 -m unittest discover -s tests -t . -v
```

The tests drive the real parsers, the HTTP handler (bound to `127.0.0.1:0`),
command construction, the subprocess runner (timeouts and caps exercised
with a local Python child, never a tape tool), and the offline tapeinfo text
parser, all with synthetic fixtures. They also check that a full collection
spawns only `sg_logs --readonly ...`, that tapeinfo config is rejected, and
that the exporter never loads the offline module.

## License

MIT, see [LICENSE](LICENSE). This covers the code in this directory only.
