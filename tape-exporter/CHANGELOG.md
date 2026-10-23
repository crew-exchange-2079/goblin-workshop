# Changelog

## Unreleased

To be tagged `tape-exporter-v0.3.0` (the version in `lto_exporter/__init__.py`).

Synthetic fixtures (0x0c decoded by the real sg_logs 1.46 binary), unit
tests, mhvtl emulation and the Grafana smoke test only. Still **not** run
against real tape hardware.

### Added

* **SSC Sequential-Access Device log page (0x0c)** collector,
  `sg_logs_sequential_access`, run as
  `sg_logs --readonly --verbose --page=0xc /dev/sgN` (`--verbose` is the
  only way to get exact byte counts from sg_logs text). New metrics:
  `tape_data_bytes_total{direction,side}` (host vs media bytes written and
  read), `tape_cleaning_required` and
  `tape_sequential_access_field_present{field}`. Unknown is not zero: a
  failed page or an inexact/conflicting field exports no value.
* Dashboard panels *Cleaning required* and *Data bytes (rate): host vs
  media* (dashboard `version` 3, uid unchanged; panels 1 and 2 are
  narrower to make room). Alert `LTOCleaningRequired` (30 min), with
  promtool tests.
* Golden fixtures in `tests/fixtures/sg3_utils_1.46/seqaccess_*`, including
  nonverbose output (no exact bytes) and page 0x0c decoded for a disk
  (Logical Block Provisioning, rejected).

### Changed

* **Default collectors are now `["sg_logs", "sequential_access"]`**: a
  device without a `collectors` key gets one more LOG SENSE per scrape
  (worst case per device `3 × (timeout + 2 s)`, was 2 ×). Configs that list
  `"collectors": ["sg_logs"]` explicitly behave exactly as before.
* `commands._check()` accepts a second exact shape,
  `[sg_logs, --readonly, --verbose, --page=0xc, /dev/sgN]`; `--verbose` is
  refused with any other page and 0xc is refused without it.
* mhvtl integration: the exec/CDB allowlist includes page 0x0c, at least one
  LOG SENSE of 0x0c is required, and its metrics must equal the harness's
  own `sg_logs --verbose` read when the emulator lists the page.

### Fixed

* The SSC parser added after 0.2.0 matched only sg3_utils git's wording
  ("Sequential access device log page", "... WRITE operations",
  "Cleaning action required (or in progress)"). sg3_utils 1.46, the version
  this project targets (Ubuntu 24.04), prints "Sequential access device
  page", "... WRITE commands" and "Cleaning action required", so every 1.46
  page would have been a `parse_error`. Both spellings are now accepted,
  literally.

### Not included (by design)

* TapeAlert flags (page 0x2E can clear on read) and `mt status` drive state
  (needs exclusive-open `/dev/stN`/`nstN`; `/dev/stN` rewinds on close). See
  README "Out of scope".

## 0.2.0 - 2026-10-17

Synthetic fixtures, unit tests, mhvtl emulation and the Grafana smoke test
only. Still **not** run against real tape hardware.

### Documentation

* README install walkthrough: all Debian/Ubuntu dependencies (`lsscsi`,
  `python3-venv`), one working directory, `sudo` where needed, the
  `lto-exporter` group and narrow udev rule, a systemd drop-in that points
  the unchanged example unit at `/opt/lto-exporter/bin/lto-exporter` and the
  pinned `/dev/sgN`, manual start/status checks, a same-host Prometheus
  scrape/rules example and manual Grafana import. Documentation only: no
  code, unit, rule or dashboard changes; not validated on physical tape
  hardware.

### Fixed

* **Duplicate `sg_logs` parameters: first value now wins.** A malformed or
  overflowing first value let a later duplicate win, contradicting the
  documented behaviour. A malformed first value now leaves the parameter
  absent; a valid first value blocks later duplicates.
* **Vanished device node reported as `device_changed`.** A dangling symlink
  resolves to itself, so the pinned node is now checked first and a missing
  node reports `device_missing`.
* **SIGTERM now exits 0** (`systemctl stop`, `docker stop`). The handler
  only calls `sys.exit(0)`; calling `server.shutdown()` there deadlocks.

### Tests

* New coverage for CLI overrides beating the config file and bind failure
  exiting 1, IPv6 loopback serving `/metrics`, `HEAD /metrics`, and the
  `exec_error` / `read_error` runner paths. These pass against the code
  without further changes. The IPv6 test skips on hosts with no IPv6
  loopback.

### Also in this release

* **Dashboard: "Collectors OK" showed failures in green.** With every
  collector failing it displayed `ok 0` / `total 4` in Grafana's default
  green (found by the new Grafana smoke test, evidence in its
  `case-failing.json` and screenshots). Panel 1 is now **Collectors failing**:
  `sum(1 - tape_exporter_collector_success)`, green at 0, red from 1, with
  `total` in neutral text. Dashboard `version` 2; uid unchanged.
* **Grafana smoke test** (`ci/grafana/`, workflow
  `tape-exporter-grafana.yml`): the dashboard is loaded into a real pinned
  Grafana OSS container for the first time, with synthetic exporter data
  through a pinned Prometheus, and checked through Grafana's HTTP API and a
  headless browser. See README "Grafana smoke test".

## 0.1.0 - 2026-10-11

First public release of lto-exporter: a read-only Prometheus exporter for
the SCSI Write (0x02) and Read (0x03) Error Counter log pages of Linux
LTO/SCSI tape drives, via `sg_logs --readonly`.

### Fixed during release review

* **Runner could raise after SIGKILL.** The post-kill `wait(timeout=2)`
  could raise `TimeoutExpired` (unkillable D-state child), escaping as an
  HTTP 500 on every scrape and spawning a new child each time. Cleanup is now
  bounded and never raises, pipes are always closed, the timeout is reported
  as `timeout`, and unreaped children are tracked: no respawn for that
  device, global cap of 4, new `tape_exporter_unreaped_children` gauge and
  `blocked_unreaped` status.
* **argv check skipped the last argument.** `_check()` looked at
  `argv[1:-1]`. It now requires the exact shape
  `[sg_logs, --readonly, --page=0x2|0x3, /dev/sgN]`, and `run_command()`
  enforces it itself so direct callers can't bypass it.
* **Config.** `sg_logs_path` basename is enforced at config load, and at
  startup the file must exist, be executable and not resolve to another
  program. Regexes use full matches (a trailing newline used to pass).
  Invalid CLI `--port`/`--listen-address` are rejected; non-integer ports in
  JSON too. The resolved `/dev/sgN` is pinned and is what gets executed (the
  configured symlink used to be re-resolved by every `open()`), and it is
  re-checked before each scrape. Docs no longer imply this prevents kernel
  device replacement.
* **Exit-status mapping** checked against sg3_utils source and binary:
  open failures exit `50 + errno` (new `os_error`), not 15 (`file_error`).
  `open_failed` is gone; `syntax_error` (1), `unit_attention` (6) and `not_ready` for 12/13
  added (the mhvtl run showed a fresh drive answering the first command
  with UNIT ATTENTION).
* **Parser fails closed** when the expected header (direction + page code)
  is missing, and no longer attributes lines under another page's header
  (e.g. Verify 0x5) to the requested page.
* **Metric renamed** before first release: `tape_error_counter_total` ->
  `tape_log_errors_total` (`promtool check metrics` rejects "counter" in a
  counter's name). Labels unchanged.
* **HTTP**: client disconnects are handled quietly, `handle_error` no longer
  prints tracebacks, per-connection socket timeout; tests close HTTPError
  responses.

### Added

* Standard packaging (`pyproject.toml`), `lto-exporter` console script,
  `--version`. The wheel ships only `lto_exporter` (no offline parser).
* Grafana dashboard and Prometheus alert rules with promtool unit tests,
  validated against the real metric/label set.
* Disabled, least-privilege systemd example and a commented udev example.
* GitHub Actions: unit tests on Python 3.10–3.13, packaging checks,
  monitoring validation, and an mhvtl emulated-drive integration job with
  strace-level evidence.
* Tests: 33 → 74 unit tests (plus promtool rule tests and the mhvtl job).

### Known limitations

See [SECURITY.md](SECURITY.md). Not tested on real hardware; device pinning
is point-in-time; stuck D-state children can't be reaped; no TLS/auth; no
TapeAlert/identity metrics by design.
