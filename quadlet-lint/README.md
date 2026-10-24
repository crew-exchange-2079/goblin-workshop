# quadlet-lint

A linter for [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
unit files: `.container`, `.volume`, `.network` and `.pod`. Python 3.10+
standard library only; it reads files and never runs Podman or systemd.

It catches the things Quadlet and systemd fail on quietly, and the options
that weaken isolation:

* **keys per Podman version**: unknown keys (with a did-you-mean), keys newer
  than your target release, deprecated keys. The key table is extracted from
  Podman's own Quadlet source at every release from 4.4 to 6.1.
* **booleans Quadlet reads as false** (`ReadOnly=y` is false).
* **images** not pinned by digest, or given as short names.
* **health checks**: none defined, `Notify=healthy` without one,
  `HealthOnFailure=kill` with nothing to restart the container.
* **risky options**: `--privileged`, `SecurityLabelDisable=`, host network or
  PID/IPC namespaces, mounting the Podman/Docker socket, broad capabilities,
  unconfined seccomp/AppArmor. `PodmanArgs=` is parsed for the same flags.
* **restart policy sanity**: invalid values, `Restart=always` on oneshot
  units (systemd refuses them), restart loops that exhaust the start limit.
* **stop timeout ordering**: the container's `StopTimeout=` must be shorter
  than systemd's `TimeoutStopSec=`, or systemd kills it mid-shutdown.
* **read-only root filesystem** suggestions.

Every rule is documented in [docs/rules.md](docs/rules.md).

```console
$ quadlet-lint --podman-version 5.2 units
units/web.container:6: warning QL101 [image-not-pinned] Image=docker.io/library/nginx:1.27 uses tag :1.27 and no @sha256: digest; the running image can change under you
units/web.container:9: error QL304 [podman-socket-mount] mounting %t/podman/podman.sock hands the container full control of the container engine, which is root-equivalent on the host for rootful Podman
units/web.container:10: warning QL501 [stop-timeout-exceeds-systemd] StopTimeout=120 is not shorter than systemd's default TimeoutStopSec (90s): systemd kills the stop command before podman's graceful window ends. Keep StopTimeout= a few seconds below TimeoutStopSec
3 file(s) checked against Podman 5.2: 1 error(s), 2 warning(s), 0 info
```

> **Status: 0.1.0, new.** Tested with unit tests and fixture units only. The
> key table is generated from upstream source, but the behavioural rules
> (restart, timeouts, image references) encode our reading of the Podman and
> systemd documentation and have not been checked against a running Podman.
> Report false positives as issues.

## Install and run

From a checkout, no install needed:

```sh
quadlet-lint/bin/quadlet-lint path/to/units
```

or install the package (`pip install ./quadlet-lint`) to get a `quadlet-lint`
command; `python -m quadlet_lint` works too.

Arguments are files or directories. Directories are searched recursively for
the four extensions; hidden directories and `*.d` drop-in directories are
skipped.

| Option | Meaning |
|---|---|
| `--podman-version X.Y` | Release the units must work with. Default: the newest in the key table. Versions older than 4.4 are rejected (no Quadlet). |
| `--format text\|json\|github` | Human lines (default), a JSON document, or GitHub workflow-command annotations. |
| `--fail-on error\|warning\|info\|never` | Lowest severity that makes the exit status 1. Default `warning`. |
| `--min-severity error\|warning\|info` | Hide findings below this severity. |
| `--select IDS` / `--disable IDS` | Comma-separated rule ids to run only / skip. |
| `--list-rules`, `--explain QL304` | Print the rule list or one rule's summary. |

Exit status: 0 clean (at the `--fail-on` threshold), 1 findings, 2 usage or
I/O error. A one-line summary goes to stderr; findings go to stdout.

### JSON output

```json
{
  "version": 1,
  "tool": "quadlet-lint 0.1.0",
  "podman_version": "5.2",
  "files": ["units/web.container"],
  "findings": [
    {"rule": "QL101", "name": "image-not-pinned", "severity": "warning",
     "path": "units/web.container", "line": 6, "message": "..."}
  ],
  "summary": {"error": 0, "warning": 1, "info": 0}
}
```

`version` is the schema version and changes only on incompatible changes.

### Suppressing findings

```ini
# quadlet-lint: disable=QL201,QL601        (whole file)
[Container]
# quadlet-lint: disable-next-line=QL303
Network=host
```

## pre-commit

```yaml
repos:
  - repo: https://github.com/crew-exchange-2079/goblin-workshop
    rev: <commit sha>
    hooks:
      - id: quadlet-lint
        args: [--podman-version, "5.2"]
```

The hook (defined in the repository root's `.pre-commit-hooks.yaml`) runs
`quadlet-lint/bin/quadlet-lint` directly with `language: script`, so
pre-commit does not have to install this monorepo; it needs `python3` 3.10+
on `PATH`. Pin `rev` to a commit SHA.

## GitHub Action

```yaml
- uses: actions/checkout@<sha>
  with:
    persist-credentials: false
- uses: crew-exchange-2079/goblin-workshop/quadlet-lint@<commit sha>
  with:
    paths: |
      deploy/quadlet
    podman-version: "5.2"
    fail-on: warning
```

Inputs: `paths` (newline-separated, default `.`), `podman-version`,
`fail-on`, `min-severity`, `select`, `disable`, `python` (default `python3`,
which GitHub's Ubuntu runners provide). Findings appear as annotations on the
pull request. Inputs are passed to the script through environment
variables, never interpolated into shell code.

## What it does not do

* **Drop-ins** (`foo.container.d/*.conf`) and `[Unit]`/`[Service]` overrides
  elsewhere are not merged; each file is linted on its own.
* **Cross-file checks** are out of scope: `Network=app.network` is not checked
  for an `app.network` file, `Pod=` targets are not resolved.
* **systemd and containers.conf defaults** are assumed to be the upstream
  ones (`DefaultTimeoutStopSec=90s`, `stop_timeout=10`).
* **Image `HEALTHCHECK`s** are invisible; QL201/QL202 only see the unit.
* `.kube`, `.image`, `.build` and `.artifact` files are not linted yet,
  though their keys are in the table.

## Updating the key table

`quadlet_lint/keydb.json` is generated, not hand-edited:

```sh
mkdir -p /tmp/q && cd /tmp/q
for v in 4.4.0 4.5.0 ... 6.1.0; do
  curl -fsSo q-$v.go https://raw.githubusercontent.com/containers/podman/v$v/pkg/systemd/quadlet/quadlet.go
done
cd - && python tools/extract_keys.py /tmp/q > quadlet_lint/keydb.json
```

The script reads the supported-key maps that Quadlet itself uses to reject
unknown keys, so a key's `added` version is the first `X.Y.0` release that
accepts it. Deprecation comes from `// deprecated` comments in the newest
release.

## Tests

```sh
cd quadlet-lint && python -m pytest -q tests
```

Fixture units live in `tests/fixtures/good` (must produce no findings) and
`tests/fixtures/bad` (the first line, `# expect: QL...`, lists exactly the
rules the file must trip).

## Licence

MIT, see [LICENSE](LICENSE).
