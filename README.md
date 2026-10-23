# goblin-workshop

This is the crew's public workshop for small open tools — a place where we build, share, and tinker with lightweight utilities that anyone is welcome to use, fork, or improve.

## Tools

- [`tape-exporter/`](tape-exporter/): read-only Prometheus exporter for Linux LTO/SCSI tape drives (MIT).
- [`tapefec/`](tapefec/): streaming forward-error-correction (Reed–Solomon) framer for tar-to-tape pipelines; pure-Python educational prototype (MIT).
- [`tape-library-playground/`](tape-library-playground/): browser-based teaching simulator of a robotic tape library and its scheduling policies; not an operational controller (MIT).

## Tape tools

The tape tools here are open and vendor-agnostic, aimed at tape administrators. They are designed around public standards — T10 SCSI log pages and MAM attributes (read via sg3_utils), LTFS, and plain byte streams — and are developed from public specifications and synthetic data only. That describes design intent, not testing: operation across particular vendors, drives or libraries has not been verified, and each tool's own README states what it has and has not been tested against.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on pull requests and on pushes to `main`. It has one independent job per project, so a failure in one never cancels the others. Jobs have read-only `contents` permission, pip is cached, and action versions are pinned to commit SHAs. A newer push to the same branch or PR cancels the superseded run. [`codeql.yml`](.github/workflows/codeql.yml) scans the Python and JavaScript code, and [Dependabot](.github/dependabot.yml) proposes Action, pip and npm updates. See [`SECURITY.md`](SECURITY.md) to report a vulnerability.

To run the same tests locally (Python 3.12 or newer; `pip install -r .github/ci-requirements.txt` provides pytest):

```sh
(cd tapefec && python -m pytest -q test_tapefec.py)
(cd tape-exporter && python -m pytest -q tests)
(cd rack-parts && python -m pytest -q tests)      # render tests skip without openscad and admesh; CI installs both and requires them
(cd ltfs-index-validator && pip install -r requirements.txt && python -m pytest -q tests)   # needs lxml
(cd tape-regression-lab/mhvtl-status && python -m pytest -q test_runner.py)
(cd tape-regression-lab/fixtures && python -m pytest -q test_corpus.py)   # every tape-exporter parser vs the sg_logs corpus
python -m pytest -q tests/sweep                  # regression tests for the 2026-10-22 bug sweep (from the repo root)
(cd tape-library-playground && node --test)      # Node 22, no Python
(cd tape-library-playground && npm ci && npx playwright install chromium && npx playwright test)   # browser: axe-core, keyboard, 360 px
```

The older per-project workflows (packaging, Grafana and Prometheus checks, the mhvtl build, OpenSCAD renders) remain and still run on their own paths.
