# goblin-workshop

This is the crew's public workshop for small open tools — a place where we build, share, and tinker with lightweight utilities that anyone is welcome to use, fork, or improve.

## Tools

- [`tape-exporter/`](tape-exporter/): read-only Prometheus exporter for Linux LTO/SCSI tape drives (MIT).
- [`tapefec/`](tapefec/): streaming forward-error-correction (Reed–Solomon) framer for tar-to-tape pipelines; pure-Python educational prototype (MIT).
- [`tape-library-playground/`](tape-library-playground/): browser-based teaching simulator of a robotic tape library and its scheduling policies; not an operational controller (MIT).
- [`tailnet-policy-tester/`](tailnet-policy-tester/): offline evaluator for Tailscale policy files: reachability and SSH queries, the policy's own `tests`/`sshTests`, and policy diffs; stdlib-only Python, not affiliated with Tailscale (MIT).
- [`hsm-sim/`](hsm-sim/): deterministic command-line teaching simulator of a disk cache in front of a tape archive (migration, watermarks, purge, recall, repack) driven by YAML scenarios; generic concepts only, models no product (MIT).
- [`timer2dagu/`](timer2dagu/): converts systemd `.timer` + `.service` pairs into Dagu DAG YAML and reports what could not be translated; standard library only (MIT).
- [`quadlet-lint/`](quadlet-lint/): linter for Podman Quadlet `.container`/`.volume`/`.network`/`.pod` units, with a pre-commit hook and a GitHub Action (MIT).
- [`tape-label-gen/`](tape-label-gen/): LTO Code 39 barcode labels (volser + media ID) as print-ready PDF sheets from a stdlib-only CLI, or from a static web page; includes a volser validator (MIT).

## Examples

[`examples/`](examples/) has end-to-end walkthroughs that chain these tools on synthetic data. Each one runs in containers, comes with a short recording, and is run in CI. The first is [exporter → Prometheus → Grafana](examples/exporter-to-grafana/).

## Tape tools

The tape tools here are open and vendor-agnostic, aimed at tape administrators. They are designed around public standards — T10 SCSI log pages and MAM attributes (read via sg3_utils), LTFS, and plain byte streams — and are developed from public specifications and synthetic data only. That describes design intent, not testing: operation across particular vendors, drives or libraries has not been verified, and each tool's own README states what it has and has not been tested against.

## Releases

Each tool is versioned and released on its own (semantic versioning, its own `CHANGELOG.md`), from a tag `<tool>-v<version>` such as `tapefec-v1.0.0`. Release assets — sdists and wheels for the Python packages, source or static bundles for the rest — are built by [`.github/workflows/release.yml`](.github/workflows/release.yml) on tags only, carry signed build-provenance attestations, and are attached to the tag's GitHub Release. Pull requests must update the changelog of every tool they change. See [`RELEASING.md`](RELEASING.md).

## CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on pull requests and on pushes to `main`. It has one independent job per project, so a failure in one never cancels the others. Jobs have read-only `contents` permission, pip is cached, and action versions are pinned to commit SHAs. A newer push to the same branch or PR cancels the superseded run. [`codeql.yml`](.github/workflows/codeql.yml) scans the Python and JavaScript code, and [Dependabot](.github/dependabot.yml) proposes Action, pip and npm updates. See [`SECURITY.md`](SECURITY.md) to report a vulnerability.

To run the same tests locally (Python 3.12 or newer; `pip install -r .github/ci-requirements.txt` provides pytest):

```sh
(cd tapefec && python -m pytest -q test_tapefec.py)
(cd tape-exporter && python -m pytest -q tests)
(cd rack-parts && python -m pytest -q tests)      # render tests skip without openscad and admesh; CI installs both and requires them
(cd ltfs-index-validator && pip install -r requirements.txt && python -m pytest -q tests)   # needs lxml
(cd tape-regression-lab/mhvtl-status && python -m pytest -q test_runner.py)
(cd tailnet-policy-tester && python -m pytest -q tests)
(cd tape-regression-lab/fixtures && python -m pytest -q test_corpus.py)   # every tape-exporter parser vs the sg_logs corpus
(cd tape-regression-lab/mhvtl-scenarios && python -m pytest -q test_scenarios.py)   # fake drive; the kernel-module run is below
(cd quadlet-lint && python -m pytest -q tests)   # stdlib only
python -m pytest -q tests/sweep                  # regression tests for the 2026-10-22 bug sweep (from the repo root)
(cd hsm-sim && pip install -r requirements.txt && python -m pytest -q tests)   # needs PyYAML
(cd timer2dagu && python -m pytest -q tests)
python -m pytest -q .github/release              # release tooling (from the repo root)
python .github/release/release.py check --base origin/main   # changelog/version check, as in changelog.yml
(cd tape-library-playground && node --test)      # Node 22, no Python
(cd tape-library-playground && npm ci && npx playwright install chromium && npx playwright test)   # browser: axe-core, keyboard, 360 px
python -m pytest -q examples/tests               # examples/: static checks (from the repo root)
python examples/run_walkthrough.py exporter-to-grafana   # examples/: live run, needs Docker + compose
(cd tape-label-gen && pip install --require-hashes --only-binary=:all: -r requirements-test.txt && python -m pytest -q tests && node --test)   # decode tests skip without the requirements; CI requires them
```

The older per-project workflows (packaging, Grafana and Prometheus checks, the mhvtl build, OpenSCAD renders) remain and still run on their own paths.
[`mhvtl-scenarios.yml`](.github/workflows/mhvtl-scenarios.yml) loads the mhvtl kernel module and runs the [tape-regression-lab scenarios](tape-regression-lab/mhvtl-scenarios/) (load/unload, full tape, cleaning cartridge, drive offline, WORM) against lto-exporter; on a runner that can't load modules it reports SKIPPED.

## Docs site

[`mkdocs.yml`](mkdocs.yml) builds a MkDocs (Material) site: a landing page ([`docs/index.md`](docs/index.md)) and one page per tool, generated at build time from each tool's README by [`docs/hooks.py`](docs/hooks.py). Edit the READMEs, not generated pages. `TOOLS` in the hook is the one list of tools: it drives the pages, the nav and the landing-page tables; a listed tool whose directory is not on the branch is left out, and a top-level directory with a README that is not listed fails the build. The `docs` job in `ci.yml` runs `mkdocs build --strict` on every PR; [`.github/workflows/pages.yml`](.github/workflows/pages.yml) deploys to GitHub Pages only on pushes to `main`. Versions are hash-locked in [`docs/requirements.txt`](docs/requirements.txt) (compiled from `docs/requirements.in`).

```sh
python -m pip install --require-hashes --only-binary=:all: -r docs/requirements.txt
python -m mkdocs serve        # or: python -m mkdocs build --strict
```
