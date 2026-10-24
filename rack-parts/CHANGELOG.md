# Changelog

All notable changes to rack-parts. Versions follow [semantic versioning](https://semver.org/);
the release process is in [`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `rack-parts-v0.1.0`, shipped as a source bundle
(`.tar.gz`/`.zip` of this directory). Nothing here has been printed,
load-tested or installed in a rack.

### Added

* Seven parametric part families: `rack_bracket`, `sff_cradle`,
  `switch_cradle`, `blanking_panel`, `cable_panel`, `rack_shelf` (10-inch
  only) and `pdu_mount`, with JSON schema, validation and synthetic examples.

### Fixed

* Non-finite STL vertices are rejected by the mesh check (2026-10-22 sweep,
  see `../FINDINGS.md`).
