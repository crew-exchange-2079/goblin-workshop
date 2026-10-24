# Releasing

Each tool in this repository is versioned and released on its own. There is
no repository-wide version.

| Tool | Version lives in | Release assets |
| --- | --- | --- |
| `tape-exporter` | `lto_exporter/__init__.py` (`__version__`) | sdist + wheel (`lto_exporter-X.Y.Z…`) |
| `tapefec` | `tapefec.py` (`__version__`) | sdist + wheel |
| `rack-parts` | `rackparts/__init__.py` (`__version__`) | source bundle (`.tar.gz`, `.zip`) |
| `ltfs-index-validator` | `validate.py` (`TOOL_VERSION`) | source bundle (`.tar.gz`, `.zip`) |
| `hsm-sim` | `hsm_sim.py` (`__version__`) | source bundle (`.tar.gz`, `.zip`) |
| `timer2dagu` | `timer2dagu.py` (`__version__`) | source bundle (`.tar.gz`, `.zip`) |
| `tape-library-playground` | `package.json` (`version`) | static bundle (`.tar.gz`, `.zip`) |

Every release also carries `SHA256SUMS` and `<tool>-<version>.intoto.jsonl`
(the signed provenance attestation, see [Verifying a release](#verifying-a-release)).

The list is [`.github/release/tools.json`](.github/release/tools.json).
`tape-regression-lab/` (an evidence kit for an upstream project) and `tests/`
are not released. `rack-parts` and `ltfs-index-validator` ship as source
bundles rather than wheels because they read data from outside a Python
package (`scad/`, `schema/`) and, for the validator, install `lxml` from a
hash-pinned `requirements.txt` that a wheel's metadata cannot express.
`hsm-sim` is a bundle for the same reason (PyYAML pinned by hash) and because
its tests compare against golden outputs in `examples/`.

## Versions

[Semantic versioning](https://semver.org/) per tool: `MAJOR.MINOR.PATCH`, with
an optional pre-release (`1.0.0-rc.1`). For these tools, "the public API" is
the command line, exit codes, config and input formats, output formats
(metrics names and labels, JSON reports, the tapefec wire format, generated
files) and anything documented in the tool's README. Breaking any of those is
a major bump (a minor bump while the major version is `0`).

Versions are written in SemVer everywhere: in the source, the changelog and
the tag. Python packaging uses [PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/#normalization)
instead, so the Python tools (`tape-exporter`, `tapefec`) may only use the
pre-releases that map onto it with the same ordering, and their sdist/wheel
file names and metadata carry the PEP 440 form:

| SemVer (source, changelog, tag) | PEP 440 (sdist, wheel) |
| --- | --- |
| `1.2.0-alpha.1` | `1.2.0a1` |
| `1.2.0-beta.2` | `1.2.0b2` |
| `1.2.0-rc.1` | `1.2.0rc1` |
| `1.2.0` | `1.2.0` |

Any other pre-release (`-rc`, `-preview.1`, `-alpha.beta`) fails the check for
a Python tool. The bundle tools accept any SemVer pre-release.

## Changelogs

Each tool has a `CHANGELOG.md` with entries newest first:

```markdown
## Unreleased

* What changed, for users of this tool.

## 1.1.0 - 2026-10-23

* ...
```

Headings are exactly `## Unreleased` (only as the first entry) or
`## X.Y.Z - YYYY-MM-DD` (`## X.Y.Z (YYYY-MM-DD)` is also accepted); any other
`## ` heading fails CI. Use `###` for sub-sections such as `### Fixed`.

[`changelog.yml`](.github/workflows/changelog.yml) enforces, on every pull
request:

* every tool with changed files also changed its `CHANGELOG.md`, and the
  change is not to an entry that is already tagged (write under
  `## Unreleased`);
* no tool's version went backwards;
* each tool's source version agrees with its changelog: equal to the top
  dated entry, or — under `## Unreleased` — equal to or greater than the last
  dated one (you may bump the version ahead of the release);
* every existing release tag has a dated changelog entry.

For a change no user could notice (a test-only refactor, a typo in a comment),
add the `skip-changelog` label to the pull request. It waives only the first
rule. Run the same check locally with

```sh
python3 .github/release/release.py check --base origin/main
```

## Cutting a release

1. **Release pull request.** On a branch, for one tool:
   * set the version in the file listed above, if it is not already bumped;
   * rename `## Unreleased` in its `CHANGELOG.md` to `## X.Y.Z - YYYY-MM-DD`
     (today's date) and make sure the entry reads well as release notes —
     it becomes the GitHub Release body verbatim.

   Open the pull request, get CI green (the `release-dry-run` job in
   [`ci.yml`](.github/workflows/ci.yml) builds every tool exactly as the
   release will), and merge it to `main`.
2. **Tag the merge commit on `main`.** The tag is `<tool>-v<version>`:

   ```sh
   git fetch origin main
   git tag -a tapefec-v1.0.0 -m "tapefec 1.0.0" origin/main
   git push origin tapefec-v1.0.0
   ```

3. **Watch [`release.yml`](.github/workflows/release.yml).** It refuses the
   tag unless the tagged commit is on `main`, the tool's source version equals
   the tag's, and the top changelog entry is the dated entry for that version.
   Then it builds, attests and publishes. Nothing else to do.

After the release, the next change to that tool starts a new
`## Unreleased` entry above the dated one.

Pre-release versions (`-rc.1` etc.) are published as GitHub pre-releases.
No release is marked "Latest", since releases of different tools share one
list.

### If a release fails

* *Refused before building* (wrong version, undated changelog, commit not on
  `main`): delete the tag (`git push --delete origin <tag>; git tag -d <tag>`),
  fix it in a pull request, and tag again.
* *Failed after building but before publishing*: re-run the failed jobs from
  the Actions page.
* *Published but wrong*: do not replace assets or move the tag. The workflow
  refuses to publish into an existing release. Release a new patch version.

## How the release workflow is built

* **Tags only.** `release.yml` triggers only on pushed tags matching
  `*-v[0-9]*`. It has no `pull_request`, branch-push or manual trigger, so a
  pull request can never run it; the first job also checks it is running for
  a tag, and the privileged jobs depend on it. The legacy `tape-exporter` tags `v0.1.0` and `v0.2.0` do not match.
* **Least privilege per job.** `build` has `contents: read` only and is the
  only job that runs repository code (the build backend and smoke tests).
  `attest` (`id-token: write`, `attestations: write`) and `publish`
  (`contents: write`) only download the build's artifacts; neither checks out
  the repository. The workflow default is no permissions at all.
* **Pinned toolchain.** Actions are pinned to commit SHAs. The Python build
  uses `build`, `setuptools`, `packaging` and `pyproject_hooks` from
  [`.github/release/requirements.txt`](.github/release/requirements.txt),
  installed with `--require-hashes`, and builds with `--no-isolation` so no
  unpinned package is fetched mid-build.
* **Reproducible.** `SOURCE_DATE_EPOCH` is the tagged commit's time; bundles
  and sdists are written with sorted entries and fixed owners, modes and
  timestamps. The `release-dry-run` CI job builds every tool twice and fails
  if any byte differs. Only files tracked by git are packaged, and a dirty
  tool directory is refused.
* **Smoke-tested.** Python wheels are installed into a clean virtual
  environment and their console script is run (`--version`); the
  `tape-exporter` sdist and wheel also go through
  `tape-exporter/ci/check_package.py`.
* **Provenance.** `actions/attest-build-provenance` signs a SLSA build
  provenance statement for every asset with the workflow's GitHub OIDC
  identity (Sigstore). It records the repository, workflow file, tag and
  commit the assets were built from.

All of this is driven by [`.github/release/release.py`](.github/release/release.py)
(standard library only; tests in `.github/release/test_release.py`).

### Recommended repository settings

These are outside the repository and must be set by an administrator:

* A tag ruleset for `*-v*` that restricts creation, update and deletion to
  maintainers, so only they can trigger a release, and tags cannot be moved.
* Branch protection on `main` requiring the CI and Changelog checks.
* Artifact attestations are available for public repositories; a private
  repository needs a plan that supports them.

## Verifying a release

With the [GitHub CLI](https://cli.github.com/), from a directory holding the
downloaded assets:

```sh
gh attestation verify tapefec-1.0.0-py3-none-any.whl \
  --repo crew-exchange-2079/goblin-workshop \
  --signer-workflow crew-exchange-2079/goblin-workshop/.github/workflows/release.yml \
  --source-ref refs/tags/tapefec-v1.0.0
sha256sum -c SHA256SUMS
```

Offline, use the attached bundle: `gh attestation verify <file>
--bundle tapefec-1.0.0.intoto.jsonl --repo crew-exchange-2079/goblin-workshop`.

To reproduce a build, check out the tag and run, with the pinned toolchain
installed (`pip install --require-hashes --only-binary=:all: -r
.github/release/requirements.txt`, Python 3.12 as in the workflow):

```sh
python .github/release/release.py build tapefec /tmp/dist
cat /tmp/dist/SHA256SUMS
```
