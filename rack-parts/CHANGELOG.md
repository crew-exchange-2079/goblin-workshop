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
* Five more part families, twelve in total: `keystone_panel`, `rack_drawer`
  (10-inch housing + drawer box from one file), `label_holder`, `cable_comb`
  and `cartridge_holder` (SFP modules, tape cartridges), each with
  validation, refusal fixtures and two synthetic examples.
* Per-part spec checks: `generate` probes the rendered STL for every feature
  the validated numbers describe and rejects it (exit 4) if one is missing;
  `rackparts spec-check DIR` re-runs them.
* 3MF export on every `generate` (read back against the STL's geometry hash)
  and an optional PNG preview (`--png`), both stdlib-only.
* `rackparts catalog`: the docs-site catalog page with a preview of every
  example (`docs/rack-parts/`), checked against the code in CI.

### Fixed

* Non-finite STL vertices are rejected by the mesh check (2026-10-22 sweep,
  see `../FINDINGS.md`).
