# Changelog

All notable changes to tapefec. Versions follow [semantic versioning](https://semver.org/);
the release process is in [`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `tapefec-v1.0.0`. Educational prototype, tested
against synthetic data only.

### Added

* `pyproject.toml`: tapefec now builds an sdist and a wheel. The wheel
  installs the single `tapefec` module and a `tapefec` console script; the
  sdist also carries the tests, `torture.py`, `bench.py` and `DESIGN.md`.
* Wire format v2 (encoder default); the decoder reads v1 and v2.

### Fixed

* `group_data_len`, trailer and `block_id` fields are no longer trusted
  without plausibility checks (2026-10-22 sweep, see `../FINDINGS.md`).
