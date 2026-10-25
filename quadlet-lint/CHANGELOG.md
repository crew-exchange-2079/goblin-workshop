# Changelog

## Unreleased

First version, to be tagged `quadlet-lint-v0.1.0` (`__version__` in
`quadlet_lint/__init__.py`).

* 30 rules across key validity per Podman version (4.4 to 6.1), booleans,
  image pinning, health checks, risky options, restart policy, stop-timeout
  ordering and read-only root filesystems. See docs/rules.md.
* Text, JSON and GitHub-annotation output; `--fail-on`, `--select`,
  `--disable`, in-file suppression comments.
* pre-commit hook (repository root `.pre-commit-hooks.yaml`) and a composite
  GitHub Action (`quadlet-lint/action.yml`).
* Quoted values are read the way each Podman release's Quadlet reads them
  (no unquoting on 4.x, `strings.Trim` on 5.x, one matched pair on 6.0+).
* A leading UTF-8 BOM is skipped before parsing, as systemd's own parser
  does: `﻿[Container]` no longer turns line 1 into a syntax error. A BOM
  anywhere other than the very start of the file is still an error.
* Whitespace inside a section header's brackets (`[ Container ]`) is now
  reported as QL001 `syntax-error` with its line number. systemd does not
  trim inside brackets, so that header is not `[Container]`; previously the
  header and every key under it were dropped without a word.
