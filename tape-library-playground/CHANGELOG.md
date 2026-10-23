# Changelog

All notable changes to the tape library playground. Versions follow
[semantic versioning](https://semver.org/); the release process is in
[`RELEASING.md`](../RELEASING.md).

## Unreleased

First tagged release, to be `tape-library-playground-v1.0.0` (the version in
`package.json`), shipped as a static bundle: `index.html`, `sim.js`, `ui.js`,
`style.css` and docs. Serve the unpacked directory from any static server.
Teaching simulator, not an operational controller.

### Added

* Robotic tape library simulator with four scheduling policies, scenarios
  including a big restore, an urgent pre-emption and a drive failure, and a
  reduced-motion mode.
