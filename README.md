# goblin-workshop

This is the crew's public workshop for small open tools — a place where we build, share, and tinker with lightweight utilities that anyone is welcome to use, fork, or improve.

## Tools

- [`tape-exporter/`](tape-exporter/): read-only Prometheus exporter for Linux LTO/SCSI tape drives (MIT).
- [`tapefec/`](tapefec/): streaming forward-error-correction (Reed–Solomon) framer for tar-to-tape pipelines; pure-Python educational prototype (MIT).
- [`tape-library-playground/`](tape-library-playground/): browser-based teaching simulator of a robotic tape library and its scheduling policies; not an operational controller (MIT).

## Tape tools

The tape tools here are open and vendor-agnostic, aimed at tape administrators. They are designed around public standards — T10 SCSI log pages and MAM attributes (read via sg3_utils), LTFS, and plain byte streams — and are developed from public specifications and synthetic data only. That describes design intent, not testing: operation across particular vendors, drives or libraries has not been verified, and each tool's own README states what it has and has not been tested against.

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on pull requests and on pushes to `main`. It has one independent job per project, so a failure in one never cancels the others. Jobs have read-only `contents` permission, pip is cached, and action versions are pinned to commit SHAs.

To run the same tests locally (Python 3.12 or newer; `pip install -r .github/ci-requirements.txt` provides pytest):

```sh
(cd tapefec && python -m pytest -q test_tapefec.py)
(cd tape-exporter && python -m pytest -q tests)
(cd rack-parts && python -m pytest -q tests)      # render tests skip without openscad and admesh; CI installs both and requires them
(cd ltfs-index-validator && pip install -r requirements.txt && python -m pytest -q tests)   # needs lxml
(cd tape-regression-lab/mhvtl-status && python -m pytest -q test_runner.py)
python -m pytest -q tests/sweep                  # regression tests for the 2026-10-22 bug sweep (from the repo root)
(cd tape-library-playground && node --test)      # Node 22, no Python
```

The older per-project workflows (packaging, Grafana and Prometheus checks, the mhvtl build, OpenSCAD renders) remain and still run on their own paths.

## Docs site

[`mkdocs.yml`](mkdocs.yml) builds a MkDocs (Material) site: a landing page ([`docs/index.md`](docs/index.md)) and one page per tool, generated at build time from each tool's README by [`docs/hooks.py`](docs/hooks.py). Edit the READMEs, not generated pages. `TOOLS` in the hook is the one list of tools: it drives the pages, the nav and the landing-page tables; a listed tool whose directory is not on the branch is left out, and a top-level directory with a README that is not listed fails the build. The `docs` job in `ci.yml` runs `mkdocs build --strict` on every PR; [`.github/workflows/pages.yml`](.github/workflows/pages.yml) deploys to GitHub Pages only on pushes to `main`. Versions are hash-locked in [`docs/requirements.txt`](docs/requirements.txt) (compiled from `docs/requirements.in`).

```sh
python -m pip install --require-hashes --only-binary=:all: -r docs/requirements.txt
python -m mkdocs serve        # or: python -m mkdocs build --strict
```
