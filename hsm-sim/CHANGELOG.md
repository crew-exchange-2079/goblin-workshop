# Changelog

All notable changes to hsm-sim. Versions follow [semantic versioning](https://semver.org/);
the release process is in [`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `hsm-sim-v0.1.0` (`__version__` in `hsm_sim.py`),
shipped as a source bundle (`.tar.gz`/`.zip` of this directory; install PyYAML
from the hash-pinned `requirements.txt`). Teaching simulator; models no product.

### Added

* Deterministic command-line simulator of a disk cache in front of a tape
  archive (migration, watermarks, purge, recall, repack) driven by YAML
  scenarios, with five example scenarios and their golden outputs.
