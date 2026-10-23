# lto-exporter

A small, read-only Prometheus exporter for Linux LTO / SCSI tape drives.
Python 3.10+ standard library only. No cloud services, accounts or telemetry.

It runs exactly one external tool, `sg_logs` from sg3_utils, without a
shell, and parses its text output:

```
sg_logs --readonly --page=0x2 /dev/sgN             # Write Error Counter log page
sg_logs --readonly --page=0x3 /dev/sgN             # Read Error Counter log page
sg_logs --readonly --verbose --page=0xc /dev/sgN   # SSC Sequential-Access Device log page
```

From these it exports read/write error counters, bytes processed, host and
media byte counters (written/read) and the drive's "cleaning required" flag.
It does **not** export TapeAlert flags or `mt status` drive state; see
[Out of scope](#out-of-scope-tapealert-flags-and-mt-drive-state) for why.

**`tapeinfo` (mtx) is never executed** and can't be enabled. See
[Why no tapeinfo](#why-no-tapeinfo).

> **Status: v0.3.0 release candidate.** Tested with unit tests, real
> `sg_logs` 1.46 output (decoded offline from synthetic pages) and, in CI,
> against an **mhvtl emulated** drive (see [Integration test](#integration-test-mhvtl)).
> It has **not** been run against real tape hardware. Treat the first
> deployment as a trial. See [SECURITY.md](SECURITY.md) and
> [CHANGELOG.md](CHANGELOG.md).

## Safety model

* **Two fixed command shapes.** `commands._check()` accepts only
  `[<sg_logs_path>, --readonly, --page=0x2|0x3, /dev/sgN]` and
  `[<sg_logs_path>, --readonly, --verbose, --page=0xc, /dev/sgN]`: every
  argument validated, including the last, and `--verbose` only together with
  page 0xc. `run_command()` calls it itself, so code that builds its own argv
  can't bypass it. No LOG SELECT, `--reset`, `--select`, `--sp`, other pages
  (TapeAlert 0x2E included), tape motion, writes, load/unload or MODE SELECT
  are ever constructed. `--verbose` is needed because without it sg_logs
  prints page 0xc byte counters only as truncated GB; it adds diagnostics
  on stderr (discarded) and changes no SCSI command.
* **Explicit allowlist, no `/dev` scan.** Up to 16 devices, each given by name
  and path in the config.
* **Only `/dev/sgN` (SCSI generic) nodes.** `/dev/stN` rewinds on close;
  `/dev/stN`/`nstN` are exclusive-open and could make a backup job fail with
  `EBUSY`. Symlinks are allowed if they resolve to `/dev/sgN`.
* **Pinning and re-checking (point-in-time only).** The configured path is
  resolved once at startup and the resulting `/dev/sgN` is pinned; commands
  only ever run against that pinned node. Before each collection the
  exporter re-resolves the configured path (must still give the pinned
  node), and checks the pinned node is a character device with major 21
  (SCSI generic) whose sysfs peripheral type is 1 (tape). If not, it runs
  nothing for that device and reports `device_changed`, `device_mismatch` or
  `device_missing`. **This does not prevent kernel device replacement:** if
  the drive disappears and another tape device takes the same sg number
  between the check and `sg_logs`' `open()`, the command reaches the new
  device. The command is still the read-only LOG SENSE above.
* **Bounded execution.** `shell=False`, stdin from `/dev/null`, stderr
  discarded, fixed `PATH`/`LC_ALL=C` environment, per-command timeout and
  output cap (child is SIGKILLed when exceeded). After SIGKILL the exporter
  waits at most 2 s. **SIGKILL does not guarantee termination**: a process
  stuck in the kernel (D state, e.g. a hung HBA) can't be reaped. Such
  children are counted in `tape_exporter_unreaped_children`, and no new
  command is spawned for that device (nor at all once 4 are stuck) until the
  kernel lets them go. Pipes are always closed.
* **Rate limiting.** Results are cached for `cache_seconds` (default 30) and
  collections are serialised, so frequent scrapes don't hammer the drive.
* **Low-cardinality labels.** `device` is your configured alias, never the
  path or serial.
* **Quiet HTTP.** Device output, errors and tracebacks are never echoed into
  HTTP responses or logs; client disconnects are handled; each connection
  has a 30 s socket timeout.

### Why no tapeinfo

An earlier draft had an opt-in `tapeinfo` collector. It was removed:

1. `tapeinfo` reads the TapeAlert log page (0x2E). Depending on the drive and
   its TapeAlert mode, reading that page can **clear** the flags, which could
   hide alerts from backup software. (The mhvtl emulator does exactly this:
   `ssc_log_sense()` clears TapeAlert flags on a full-length read.)
2. mtx's Linux backend opens the device `O_RDWR`, so it needs write
   permission on the sg node.

Enforcement, all covered by tests:
* The config rejects `"collectors": ["tapeinfo"]` and `tapeinfo_path`.
* The command check refuses any executable whose basename isn't `sg_logs`
  and any page other than 0x2/0x3/0xc.
* The exporter has no TapeAlert/tapeinfo metrics.
* The exporter never imports the offline parser below, and the wheel does
  not ship it.

**Offline parser (separate, optional, source tree only).**
`offline/tapeinfo_text.py` parses already-captured `tapeinfo` text from stdin
into JSON (`python3 -m offline.tapeinfo_text < captured.txt`). It runs no
commands, opens no device and emits no metrics. Because `tapeinfo` prints
TapeAlert lines only for set flags, it reports `tapealert_support: "unknown"`
and the listed codes, never a zero count. It never parses the serial number.

### Out of scope: TapeAlert flags and `mt` drive state

Both were considered for 0.3.0 and left out on purpose:

* **TapeAlert flags** come from LOG SENSE page 0x2E (or `tapeinfo`, which
  reads the same page). Reading it can clear the flags, as described above,
  which would hide alerts from the backup software that relies on them.
  "Read-only" in the SCSI sense isn't side-effect free here.
  The one TapeAlert-derived signal that is safe to read is the
  **cleaning-required** flag, which drives also report in the
  Sequential-Access Device page (0x0c, parameter 0x0100). It is exported as
  `tape_cleaning_required`. SSC-4's TapeAlert Response page (0x12) is not
  read either: it isn't confirmed on real drives here, and mhvtl doesn't
  emulate it.
* **`mt status` / drive state** (online, BOT/EOD, write-protect, file and
  block number) needs the st driver: `mt` opens `/dev/stN` or `/dev/nstN`.
  Those nodes are exclusive-open, so a scrape during a backup can make the
  backup job's open fail with `EBUSY`. Closing `/dev/stN` also rewinds the
  tape. The exporter therefore never touches st nodes and never runs `mt`.
  The availability signals it does have are the per-collector `status`
  values (`ok`, `not_ready`, `unit_attention`, `device_missing`, ...),
  shown in the dashboard's *Collector status* table.

## Install and run

A manual walkthrough for a single Debian/Ubuntu host. Nothing here is run
for you, and the project installs, enables and starts nothing. Commands
prefixed `sudo` need root; the rest don't. Needs Python 3.10+.
**Not tested on physical tape hardware**, so treat the first start as a
trial.

### 1. Install

```sh
# Debian/Ubuntu package names; others vary
sudo apt install sg3-utils lsscsi python3-venv git   # sg_logs, lsscsi, venv, clone

git clone --branch main https://github.com/crew-exchange-2079/goblin-workshop
cd goblin-workshop/tape-exporter                     # later steps run from here

# root-owned venv: the service's DynamicUser can read it but not change it
sudo python3 -m venv /opt/lto-exporter
sudo /opt/lto-exporter/bin/pip install .             # or a built wheel

lsscsi -g                                            # tape drive -> /dev/stN and /dev/sgN
sudo install -m 0644 example-config.json /etc/lto-exporter.json
sudoedit /etc/lto-exporter.json                      # set devices[].path to your /dev/sgN
```

Keep `/opt/lto-exporter` owned by root; don't `chown` it to a normal user.
`python3 -m lto_exporter` also works from this directory without installing.
`--listen-address` and `--port` override the config and are validated the
same way (port 0–65535). Port 9842 is just a default; it isn't registered
anywhere.

Startup fails (exit 2) on any config error, including an `sg_logs_path` that
is missing, not executable, not named `sg_logs`, or a symlink to some other
program. It exits 1 if it can't bind.

### 2. Permissions: group and udev rule

Examples, **not installed or enabled by this project**:
[`contrib/systemd/lto-exporter.service`](contrib/systemd/lto-exporter.service)
(DynamicUser, no capabilities, `DevicePolicy=closed` with one read-only
`DeviceAllow`, loopback-only IP policy) and
[`contrib/udev/60-lto-exporter.rules.example`](contrib/udev/60-lto-exporter.rules.example)
(every line commented out). The unit's `SupplementaryGroups=lto-exporter`
needs that group to exist, and a udev rule gives it read access to one drive.

```sh
sudo groupadd --system lto-exporter

# 1. Inspect your drive (replace sg3 with your node) and note ATTRS{type}=="1",
#    vendor and model. Add KERNELS=="H:C:T:L" if you have identical drives.
udevadm info -a -n /dev/sg3

# 2. Copy the example, then edit the rule: replace the placeholder
#    vendor/model so it matches ONE actual tape drive, and uncomment it.
sudo install -m 0644 contrib/udev/60-lto-exporter.rules.example \
    /etc/udev/rules.d/60-lto-exporter.rules
sudoedit /etc/udev/rules.d/60-lto-exporter.rules

# 3. Reload rules and re-apply them to that one node only. Never trigger
#    all devices.
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=scsi_generic --sysname-match=sg3
ls -l /dev/sg3          # expect: crw-r----- 1 root lto-exporter ...
```

The rule matches the SCSI generic node (`SUBSYSTEM=="scsi_generic"`), i.e.
the `/dev/sgN` that `lsscsi -g` lists for the drive, not `/dev/stN`. If you
put a udev symlink in front of it, the exporter resolves the symlink once at
startup and pins the resulting `/dev/sgN`; the permissions and the systemd
`DeviceAllow` below must both refer to that resolved node. The service logs
`pinned to /dev/sgN` at startup.

The rule sets the node to `root:lto-exporter` `0640`, replacing whatever
group it had (usually `disk` or `tape`, `0660`). Check that nothing else (a
backup tool, say) relies on that group for this sg node before applying it.
Don't add write access, capabilities, or the `disk`/`tape` groups to make it
work; see [Permissions](#permissions-no-root-needed).

### 3. Install the service and adapt it before the first start

The shipped unit hardcodes `ExecStart=/usr/local/bin/lto-exporter` and
`DeviceAllow=/dev/sg3 r`, which are examples. Leave the shipped file as is
and override both with a reviewed drop-in. Reset the list first
(`ExecStart=` / `DeviceAllow=` with an empty value), then give exactly one
value:

```sh
sudo install -m 0644 contrib/systemd/lto-exporter.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/lto-exporter.service.d
sudoedit /etc/systemd/system/lto-exporter.service.d/override.conf
```

```ini
[Service]
ExecStart=
ExecStart=/opt/lto-exporter/bin/lto-exporter --config /etc/lto-exporter.json
DeviceAllow=
DeviceAllow=/dev/sg3 r
```

`/dev/sg3` is a placeholder: use the node pinned from your config's
`devices[].path`, one device and read-only. systemd turns it into a
major:minor rule at start, so after an sg renumbering restart the service
(and fix the config, drop-in and udev rule if the drive moved). The drop-in
changes nothing else: sandboxing, `DynamicUser`, empty capability sets,
`DevicePolicy=closed` and the loopback-only IP policy stay as shipped.

Trial start (no `enable` yet):

```sh
sudo systemctl daemon-reload
sudo systemctl cat lto-exporter                   # review the merged result
sudo systemctl start lto-exporter
sudo systemctl status lto-exporter
sudo journalctl -u lto-exporter -b --no-pager | tail     # look for "pinned to /dev/sgN"
curl -s http://127.0.0.1:9842/metrics | grep -E '^tape_exporter_collector_(success|status)'
```

Expect `collector_success ... 1` and `status="ok"`. Anything else: see
[Collector status](#collector-status); `os_error` usually means the
permissions or `DeviceAllow` above don't match the pinned node. Only once
that looks right, and you've reviewed the unit, run
`sudo systemctl enable lto-exporter` so it starts at boot.

### 4. Prometheus and Grafana

Add a same-host scrape job (the bundled alert `LTOExporterDown` assumes the
job name `lto-exporter`) and load the shipped rules from an installed,
absolute path. Paths and the reload method depend on your Prometheus setup.

```yaml
# prometheus.yml
rule_files:
  - /etc/prometheus/rules/lto-exporter-alerts.yml

scrape_configs:
  - job_name: lto-exporter
    scrape_interval: 60s
    scrape_timeout: 30s
    static_configs:
      - targets: ['127.0.0.1:9842']
```

```sh
sudo install -d -m 0755 /etc/prometheus/rules
sudo install -m 0644 monitoring/prometheus/lto-exporter-alerts.yml /etc/prometheus/rules/
promtool check rules /etc/prometheus/rules/lto-exporter-alerts.yml
promtool check config /etc/prometheus/prometheus.yml
# then reload Prometheus the way you normally do (SIGHUP, or POST /-/reload if enabled)
```

`127.0.0.1` means Prometheus must run on the same host; don't widen
`listen_address` or the unit's IP policy for this (the exporter has no
TLS/auth). A scrape runs three `sg_logs` commands per device (two with
`"collectors": ["sg_logs"]`), one device at a time, so with the defaults a
device can take up to `3 × (10 + 2) = 36 s` on an uncached scrape. Size
`scrape_timeout` for your number of devices and the worst-case command
latency (`36 s × devices` with defaults), and keep it at or below
`scrape_interval`.

The cache is only a rate limit, not something to rely on: a result is reused
only while it is younger than `cache_seconds`. With `scrape_interval: 60s` and
the default `cache_seconds` of 30, regularly spaced scrapes find the cache
expired, so each one queries the drive (about once a minute). The cache just
serves repeated or concurrent scrapes inside those 30 s. If several scrapers
(or ad-hoc `curl`s) hit the exporter, raise `cache_seconds` above the
shortest scrape interval (served data is then up to that old) rather than scraping the drive more often.

For Grafana, import the dashboard by hand: **Dashboards → New → Import →
Upload JSON file**, choose
[`monitoring/grafana/lto-exporter-dashboard.json`](monitoring/grafana/lto-exporter-dashboard.json),
and import. It carries no hardcoded datasource UID: every panel uses the
`datasource` dashboard variable, so after import pick
your existing Prometheus datasource in the **datasource** dropdown at the top
of the dashboard; `instance` and `device` then populate from it. The dashboard uid is
`lto-exporter-v1`; importing again over it asks before overwriting. Importing
needs no new accounts or integrations beyond permission to create dashboards.

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
    {"name": "drive0", "path": "/dev/sg3", "collectors": ["sg_logs", "sequential_access"]}
  ]
}
```

| key | default | limits |
|-----|---------|--------|
| `listen_address` | `127.0.0.1` | host name or IP (IPv6 OK); put a reverse proxy in front if you need TLS/auth |
| `port` | `9842` | integer 0–65535 (0 = ephemeral) |
| `command_timeout_seconds` | 10 | 0.1–120 |
| `max_output_bytes` | 65536 | integer, 1 KiB–1 MiB |
| `cache_seconds` | 30 | 0–3600 |
| `sg_logs_path` | `/usr/bin/sg_logs` | absolute, no whitespace/control chars, basename `sg_logs`; checked executable at startup |
| `devices[].name` | (required) | `[A-Za-z][A-Za-z0-9_]{0,31}` exactly (no trailing newline), unique |
| `devices[].path` | (required) | absolute `/dev/` path, no whitespace/control chars, must be or resolve to `/dev/sgN`; two entries may not pin the same node |
| `devices[].collectors` | `["sg_logs", "sequential_access"]` | non-empty subset of `sg_logs` (pages 0x2, 0x3) and `sequential_access` (page 0xc); `tapeinfo` is rejected |

`/dev/sgN` numbering can change across reboots and hot-plug. A udev-created
symlink that resolves to the drive's sg node is more stable, but it is
resolved **once at startup**; if it later points elsewhere the exporter
reports `device_changed` and stops querying that device until restarted.

## Permissions (no root needed)

Run the exporter as a dedicated unprivileged user. It needs:

* Read access to the drive's `/dev/sgN` only. sg nodes are usually
  `root:disk` (or `root:tape`) `0660`. Use a narrow udev rule that gives a
  dedicated group read-only access to *that drive only* (see
  `contrib/udev/`). **Never add the user to `disk`** (raw access to every
  disk) or `tape` (every tape device).
* Linux filters `SG_IO` on read-only file descriptors for unprivileged
  callers to a list of non-modifying commands; INQUIRY and LOG SENSE are on
  it. The CI integration run exercises exactly this (unprivileged user,
  read-only node, 0640). If your kernel refuses, the collector shows
  `os_error` or `error`. Don't "fix" it with `CAP_SYS_RAWIO` or write access.

## Metrics

Sample output from the synthetic fixtures (`tests/fixtures/`; the 0x0c
values come from real `sg_logs` 1.46 decoding a synthetic page), abridged:

```
tape_exporter_build_info{version="0.3.0"} 1
tape_exporter_collector_success{device="drive0",collector="sg_logs_write_errors"} 1
tape_exporter_collector_success{device="drive0",collector="sg_logs_sequential_access"} 1
tape_exporter_collector_duration_seconds{device="drive0",collector="sg_logs_write_errors"} 0.12
tape_exporter_collector_status{device="drive0",collector="sg_logs_write_errors",status="ok"} 1
tape_log_parameter_present{device="drive0",direction="read",parameter="rewrites_or_rereads"} 0
tape_log_errors_total{device="drive0",direction="write",parameter="total_uncorrected"} 0
tape_log_errors_total{device="drive0",direction="write",parameter="corrected_with_delay"} 3
tape_bytes_processed_total{device="drive0",direction="write"} 2199023255553
tape_sequential_access_field_present{device="drive0",field="cleaning_action_required"} 1
tape_data_bytes_total{device="drive0",direction="write",side="host"} 2199023255553
tape_data_bytes_total{device="drive0",direction="write",side="media"} 1500000000123
tape_data_bytes_total{device="drive0",direction="read",side="media"} 734003200000
tape_data_bytes_total{device="drive0",direction="read",side="host"} 1468006400000
tape_cleaning_required{device="drive0"} 1
tape_exporter_unreaped_children 0
```

| metric | labels | source |
|--------|--------|--------|
| `tape_exporter_build_info` | `version` | exporter |
| `tape_exporter_unreaped_children` | – | exporter: killed `sg_logs` children not yet reaped |
| `tape_exporter_collector_success`, `tape_exporter_collector_duration_seconds` | `device`, `collector` | exporter |
| `tape_exporter_collector_status` | `device`, `collector`, `status` | exporter, see below |
| `tape_log_errors_total` | `device`, `direction`, `parameter` | Write (0x02) / Read (0x03) Error Counter log page, parameter codes 0–4 and 6 |
| `tape_bytes_processed_total` | `device`, `direction` | same pages, parameter code 5 |
| `tape_log_parameter_present` | `device`, `direction`, `parameter` | 1/0 per known parameter, emitted only when the page was read |
| `tape_data_bytes_total` | `device`, `direction`, `side` | Sequential-Access Device log page (0x0c), parameter codes 0–3 |
| `tape_cleaning_required` | `device` | same page, parameter code 0x0100: 1 = cleaning required (or in progress), 0 = not required (or completed) |
| `tape_sequential_access_field_present` | `device`, `field` | 1/0 per field of page 0x0c, emitted only when the page was read |

`collector` ∈ `sg_logs_write_errors`, `sg_logs_read_errors`,
`sg_logs_sequential_access`; `direction` ∈ `write`, `read`; `parameter` ∈
`corrected_without_delay`, `corrected_with_delay`, `rewrites_or_rereads`,
`total_corrected`, `correction_algorithm_processed`, `total_uncorrected`
(and `bytes_processed` on `tape_log_parameter_present` only).

Page 0x0c byte counters, by `direction` and `side`:

| `direction` | `side` | SSC parameter | meaning |
|---|---|---|---|
| `write` | `host` | 0x0000 | data bytes received from the host with WRITE commands |
| `write` | `media` | 0x0001 | data bytes written to tape (after compression) |
| `read` | `media` | 0x0002 | data bytes read from tape (before decompression) |
| `read` | `host` | 0x0003 | data bytes transferred to the host by READ commands |

`rate(...{side="host"}) / rate(...{side="media"})` per direction is the
effective compression ratio. `field` ∈ `bytes_received_write`,
`bytes_written_media`, `bytes_read_media`, `bytes_transferred_read`,
`cleaning_action_required`.

A byte counter is exported only if `sg_logs --verbose` printed its exact
value and that value matches the truncated GB figure on the same line. A
field that is repeated with different values, or a cleaning flag reported
both ways, is treated as unknown. Page 0x0c means Logical Block Provisioning
on disks; the parser accepts only the SSC heading, which `sg_logs` prints
after INQUIRY reports a tape (sysfs type 1 is also checked before each
scrape). sg3_utils 1.46 and current git spell the heading and labels
differently; both are accepted, matched literally.

### Collector status

| status | meaning |
|--------|---------|
| `ok` | page read and its header recognised |
| `unsupported` | sg_logs exit 5 (illegal request) or 9 (invalid opcode): the drive doesn't support the page. Its counters are **unknown, not zero** |
| `not_ready` | exit 2, 12 or 13 (sense key NOT READY, incl. standby/unavailable) |
| `unit_attention` | exit 6: the drive reported UNIT ATTENTION (e.g. after power-on/reset); usually clears on the next scrape |
| `os_error` | exit 50–96: an OS errno (`50 + errno`), usually from `open()` of the node, e.g. 52 = ENOENT, 63 = EACCES |
| `file_error` | exit 15 |
| `device_timeout` | exit 33: the SCSI command timed out inside sg_logs |
| `syntax_error` | exit 1, e.g. an sg_logs too old for `--readonly` (upgrade sg3_utils; don't drop the flag) |
| `error` | any other non-zero exit (e.g. 99) |
| `parse_error` | exit 0 but the expected page header wasn't found |
| `timeout` | the exporter's own timeout fired; child killed |
| `output_too_large` | output exceeded `max_output_bytes`; child killed |
| `read_error` | reading the child's output failed |
| `not_found` / `exec_error` | sg_logs couldn't be started |
| `blocked_unreaped` | an earlier child for this device (or 4 children overall) is still unreaped; nothing was spawned |
| `device_changed` | the configured path no longer resolves to the pinned node |
| `device_mismatch` | the pinned node isn't a char device 21:N with sysfs type 1 (or sysfs unreadable) |
| `device_missing` | the pinned node doesn't exist |
| `internal_error` | unexpected exception in the exporter (details not exposed) |

Exit codes were checked against sg3_utils `include/sg_lib.h` and
`sg_convert_errno()` in `lib/sg_lib.c` (git
`1929463554c5dc749c5a89bcefb2c88a08d45575`) and by running the Ubuntu 24.04
`sg3-utils` 1.46 binary: an open failure is **not** exit 15 but `50 + errno`
(a missing node exits 52). Earlier drafts mapped 15 to `open_failed`; that
status no longer exists.

**Missing ≠ zero.** If a collector fails, none of its value series are
emitted, and `collector_success` is 0 with a `status` that tells you why. If a
page is read but a parameter is absent or malformed, its value series is
omitted and `tape_log_parameter_present` is 0.

Counters are whatever the drive reports. They can reset on drive power-cycle,
reset or firmware behaviour, and Prometheus `rate()`/`increase()` handle
that. **This exporter does not estimate remaining tape or head life**, and
you shouldn't derive one from these numbers without vendor-specific
guidance.

## Dashboards and alerts

* [`monitoring/grafana/lto-exporter-dashboard.json`](monitoring/grafana/lto-exporter-dashboard.json):
  Grafana dashboard (uid `lto-exporter-v1`, schemaVersion 39) with
  `datasource`, `instance` and `device` variables. "Collectors failing" is
  red from 1; the colours and the 1 h windows are example choices, not
  vendor guidance. A missing counter shows **No data**, never 0. A whole
  exporter that is down also shows nothing (its `instance` drops out of the
  variable); `LTOExporterDown` is what reports that, not the dashboard.
  Panels *Cleaning required* (per device, red = required) and *Data bytes
  (rate): host vs media* show the 0x0c page; a drive whose page can't be
  read shows no value there, not "not required".
* [`monitoring/prometheus/lto-exporter-alerts.yml`](monitoring/prometheus/lto-exporter-alerts.yml):
  alert rules, with promtool unit tests in `lto-exporter-alerts.test.yml`.
  `LTOCleaningRequired` fires after 30 minutes of cleaning required (long
  enough not to fire while a cleaning cycle is running).
  `LTOExporterDown` assumes the scrape job is named `lto-exporter`.
  Thresholds are starting points, not vendor guidance.

Validation: `ci/check_monitoring.py` checks every dashboard and alert query
against the metric names, label names and label values the exporter really
emits (and is unit-tested to catch mismatches); CI runs `promtool check
rules`, `promtool test rules`, and `promtool check rules` on every dashboard
query; the mhvtl job evaluates all of them against a live Prometheus that
scraped the exporter. The [Grafana smoke test](#grafana-smoke-test) loads
the dashboard into a real Grafana OSS container with synthetic data; it has
**not** been used with real tape drives or a production Grafana.

## Supported output formats and limits

* **sg_logs**, part of sg3_utils.
  [sg_logs(8)](https://manpages.debian.org/stable/sg3-utils/sg_logs.8.en.html)
  covers `--page` and `--readonly`. The text layout comes from
  `show_error_counter_page()` in
  [`src/sg_logs.c`](https://github.com/doug-gilbert/sg3_utils/blob/main/src/sg_logs.c):
  header `Write error counter log page  [0x2]` (1.46 prints `... error
  counter page  [0x2]`, without "log"; both accepted), then lines
  `  <name> = <n>`, optionally suffixed ` [N GB]`/` [N TB]`. Parameter
  meanings are from T10 SPC "Error counter log pages". Page 0x0c comes from
  `show_sequential_access_page()` in the same file: heading
  `Sequential access device page (ssc-3)` (git: `... device log page ...`),
  then `  Data bytes <...> WRITE|READ commands: <N> GB [<exact> bytes]`
  (git: `operations`; the `[... bytes]` part only with `--verbose`) and a
  cleaning line; parameter meanings are from T10 SSC "Sequential-Access
  Device log page".
  `tests/fixtures/sg3_utils_1.46/` holds real 1.46 output for synthetic pages.
* The offline-only tapeinfo parser cites
  [tapeinfo(1)](https://manpages.debian.org/stable/mtx/tapeinfo.1.en.html)
  and `tapeinfo.c` in [mtx 1.3.12](https://sourceforge.net/projects/mtx/).

Limits and tolerance:

* Only the English text output is parsed (forced with `LC_ALL=C`). The
  `--json`/`--hex` modes of newer sg_logs are not used.
* **Headerless output fails closed.** Values are only taken from lines under
  the expected header (right direction *and* page code). Output without it
  (e.g. an unknown sg_logs version that changed the header) is `parse_error`,
  and lines after any other page header (Verify 0x5, Read Reverse 0x4, …)
  are ignored. Unknown and vendor-specific parameter lines are ignored.
* Values must be decimal and ≤ 2^64−1, otherwise they count as missing.
  Duplicate parameters: first wins. Output is capped at `max_output_bytes`.
* No TapeAlert, `mt`/ready-state or drive-identity metrics (see
  [Out of scope](#out-of-scope-tapealert-flags-and-mt-drive-state)). Cleaning
  required and host/media byte counts (hence compression) come from page
  0x0c.
* One scrape runs up to 3 commands per device, sequentially, under one lock.
  Worst case is about `3 × (command_timeout_seconds + 2 s) × devices`. Set
  Prometheus `scrape_timeout` accordingly, or keep the cache.
* No TLS/auth. Keep it on loopback or behind a proxy.

## Tests

```sh
cd tape-exporter
python3 -m unittest discover -s tests -t . -v
```

The tests drive the real parsers (including real sg3_utils 1.46 output), the
HTTP handler (bound to `127.0.0.1:0`; disconnects, 404/500), command
validation in every argv position, the bounded runner (timeouts, caps and a
simulated unkillable D-state child; real children are a local Python
process, never a tape tool), config/CLI validation, per-scrape device
re-checks, the monitoring assets, the service example, the integration
verdict logic and the Grafana smoke harness (mocked). CI (`.github/workflows/tape-exporter-ci.yml`) runs them on
Python 3.10–3.13, plus packaging and promtool checks.

### Integration test (mhvtl)

`.github/workflows/tape-exporter-mhvtl.yml` runs
[`ci/mhvtl/run_integration.sh`](ci/mhvtl/run_integration.sh) on a
disposable GitHub-hosted VM. It builds [mhvtl](https://github.com/markh794/mhvtl)
at a pinned commit, `insmod`s its kernel module, starts **one** emulated
IBM ULT3580-TD8 drive with a synthetic serial and **no media**, and
identifies it only if its SCSI host is the `mhvtl` pseudo adapter *and* the
serial matches. The exporter then runs as an unprivileged user with read
access to that node only, under `strace`, and the run fails unless every
exec, `open()`, `ioctl()` and SCSI CDB is the expected read-only one (INQUIRY
and LOG SENSE of pages 0x02/0x03/0x0c with SP=0) and the metrics agree with
what the emulator reports in its Supported Log Pages page and with the
harness's own `sg_logs` reads of the same pages. Evidence (identify
JSON, strace, metrics, summary) is uploaded as the `mhvtl-evidence`
artifact.

mhvtl's error-counter and 0x0c pages are static zeros (no media is ever
loaded, and its cleaning flag is clear): a passing run proves the
command path, permissions and parsing against a real kernel SCSI stack, not
that the numbers mean anything for real drives. The script refuses to run
outside CI unless `LTO_MHVTL_I_UNDERSTAND=yes`; only run it on a throwaway VM.

### Grafana smoke test

`.github/workflows/tape-exporter-grafana.yml` runs
[`ci/grafana/smoke.py`](ci/grafana/smoke.py) on a GitHub-hosted runner, on
pull requests that touch the dashboard or `ci/grafana/`, or by hand. It
starts three throwaway containers, pinned by digest:

| role | image |
|------|-------|
| Grafana | `grafana/grafana-oss:13.0.2@sha256:5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2` |
| Prometheus | `prom/prometheus:v3.14.0@sha256:5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0` |
| synthetic targets | `python:3.13.16-alpine3.24@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a` |

The synthetic targets ([`ci/grafana/synthetic_target.py`](ci/grafana/synthetic_target.py))
are the exporter's own `Collector` and HTTP handler with a fake `sg_logs`
runner, so names, labels and format are exactly the exporter's: a
**healthy** exporter (non-zero, growing counters; one page with missing
parameters; one drive needing cleaning), a **failing** one (`unsupported`,
`timeout`, `not_ready`, `device_missing`; no counters) and an **absent** one (a name that doesn't resolve, `up` = 0).
All values are made up. Nothing is executed and no device is opened.

Isolation: containers sit on an `--internal` Docker network; only Grafana
is also on a second network so it can publish port 3000, on `127.0.0.1`
with an ephemeral host port. Every container runs with `--cap-drop=ALL`,
`no-new-privileges`, `--read-only`, tmpfs data dirs and read-only bind
mounts of the files it needs; no host network, `--privileged` or devices.
The Grafana admin password is generated per run, handed over in a 0600
`--env-file` and sent only in an HTTP header to 127.0.0.1, never through
a proxy and never on a redirect (redirects are refused, not followed);
artifacts are scanned for it. Grafana's update checks, plugin pre-install, news feed and
analytics are switched off, and the browser aborts every request that isn't
to the local Grafana. All containers, networks and the temp dir are removed
in `finally` (also on failure, timeout or SIGTERM); the run fails if any
labelled resource is left **or if it can't list them to check**, and the
workflow re-checks that independently.

Checks, all through Grafana (not straight to Prometheus):

* datasource health OK for the real Prometheus, and **not** OK for a second,
  unreachable datasource;
* the dashboard is provisioned from the repo file unchanged (panels,
  queries, variables) and has no zero-filling defaults (`noValue: 0`,
  `vector(0)`);
* the `instance`/`device` variable queries resolve through the
  datasource's resource API to exactly the synthetic instances/devices;
* every panel query through `/api/ds/query` for: healthy instance, failing
  instance, absent instance, `device=drive1`, and `device=drive0|drive2`,
  against per-panel expectations: HTTP 200 with exactly the requested
  query refs, each a well-formed result (frames whose columns match their
  schema; anything else fails, so an error can't pass as "no data"), no
  query errors, non-zero activity where
  the data has some, **no series at all** (not 0) where counters are
  missing, only the selected devices, and no stat value in a healthy
  colour while collectors fail (colour derived from the panel's threshold
  config); `$__rate_interval` is expanded by Grafana itself;
* negative control: the same checks against the unreachable datasource
  must report an error for every query;
* with `--browser`: headless Chrome (Playwright) opens four views
  (all, failing instance, `device=drive1`, unreachable datasource), waits
  for all 9 panel headers, and requires no panel error indicator, no
  query errors, "No data" on the five counter panels of the failing
  instance, the device variable substituted into the queries the frontend
  sent, and error indicators on all panels for the unreachable datasource.
  Screenshots must be non-blank 1600x1600 PNGs with no dialog over the
  dashboard. **That is a smoke check, not a visual or layout review**: the
  screenshots are kept so a person can look at them.

Evidence (summary, per-case query results, served dashboard, container logs,
screenshots, page text) is uploaded as the `grafana-smoke-evidence`
artifact. Exit codes: 0 pass, 1 a check failed, 2 the stack could not be
started (the smoke test did **not** run; never a pass).

The first run found a real defect: with every collector failing, panel 1
("Collectors OK") showed `ok 0` / `total 4` in Grafana's default green. It
now counts failing collectors (red from 1). See [CHANGELOG.md](CHANGELOG.md).

Run it yourself on a machine with Docker you don't mind using for this
(it pulls about 500 MB of images):

```sh
cd tape-exporter
python3 ci/grafana/smoke.py --artifacts /tmp/grafana-smoke           # API checks
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright==1.56.0
/tmp/pw/bin/python -m playwright install chromium   # or CHROME_PATH=/path/to/chrome
/tmp/pw/bin/python ci/grafana/smoke.py --artifacts /tmp/grafana-smoke --browser
```

`tests/test_grafana_smoke.py` (part of the normal unit tests) covers the
verifier and cleanup logic with a fake docker and hand-written Grafana
responses; it starts no containers. Limits: synthetic data only, minutes
of history (queries use the last 15 minutes, not the dashboard's 24 h
default), one Grafana version, and only what the API and DOM expose;
legends, colours and layout are not judged automatically.

## License

MIT, see [LICENSE](LICENSE). This covers the code in this directory only.
