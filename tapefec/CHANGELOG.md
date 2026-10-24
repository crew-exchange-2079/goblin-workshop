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
* `test_properties.py` (hypothesis; pinned and hashed in
  `requirements-dev.txt`) and `fuzz.py` (stdlib) check the erasure contract:
  up to `m` erased frames per group recover exactly; more fail loudly and
  never return wrong bytes outside the reported unrecoverable ranges.
* Decode reports gain `implausible_frames`: CRC-valid frames whose header
  does not fit the stream's layout, which are now ignored.

### Fixed

* `group_data_len`, trailer and `block_id` fields are no longer trusted
  without plausibility checks (2026-10-22 sweep, see `../FINDINGS.md`).
* Found by fuzzing: a CRC-valid data frame from another stream with the same
  stream id (e.g. two archives encoded with the default `--stream-id 0`)
  could fill a missing slot, yielding wrong data or an `OverflowError` crash;
  one stray frame with a far-ahead `block_id` zero-filled phantom groups and
  wrote past the trailer length; a trailer with another stream id was
  accepted. Every data, parity and trailer frame is now checked against the
  encoder's layout (`DESIGN.md` §1.1). A frame whose `group_data_len`
  exceeds `k` is now ignored rather than read as a full group.
* `encode_stream(version=1)` stamped data and parity frames as version 2;
  streams written that way still decode.
