# Changelog

## 0.1.0 (unreleased)

First version.

* 30 rules across key validity per Podman version (4.4 to 6.1), booleans,
  image pinning, health checks, risky options, restart policy, stop-timeout
  ordering and read-only root filesystems. See docs/rules.md.
* Text, JSON and GitHub-annotation output; `--fail-on`, `--select`,
  `--disable`, in-file suppression comments.
* pre-commit hook (repository root `.pre-commit-hooks.yaml`) and a composite
  GitHub Action (`quadlet-lint/action.yml`).
* Quoted values are read the way each Podman release's Quadlet reads them
  (no unquoting on 4.x, `strings.Trim` on 5.x, one matched pair on 6.0+).
