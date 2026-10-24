# Changelog

All notable changes to ltfs-index-validator. Versions follow
[semantic versioning](https://semver.org/); the release process is in
[`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `ltfs-index-validator-v0.1.0` (`TOOL_VERSION` in
`validate.py`), shipped as a source bundle (`.tar.gz`/`.zip` of this
directory; install `lxml` from the hash-pinned `requirements.txt`).

### Added

* Offline schema-only checker for LTFS 2.4.0 full index XML against the
  SNIA Annex B XSD, with a bounded JSON report and fixed exit codes. Not
  LTFS compliance or certification.
