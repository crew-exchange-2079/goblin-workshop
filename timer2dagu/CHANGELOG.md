# Changelog

All notable changes to timer2dagu. Versions follow [semantic versioning](https://semver.org/);
the release process is in [`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `timer2dagu-v0.1.0` (`__version__` in
`timer2dagu.py`), shipped as a source bundle (`.tar.gz`/`.zip` of this
directory). Python 3.10+ standard library only.

### Added

* Convert a systemd `.timer` + `.service` pair into a Dagu DAG, with a report
  of every directive that was not carried over exactly.
