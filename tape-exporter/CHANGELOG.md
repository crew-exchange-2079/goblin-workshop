# Changelog

## 0.1.0 (release candidate, not yet tagged)

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
  `open_failed` is gone; `syntax_error` (1) and `not_ready` for 12/13 added.
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
