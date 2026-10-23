# dagu-lint

Policy linter for [Dagu](https://github.com/dagu-org/dagu) DAG YAML files, as a CLI and a GitHub Action.
It checks things Dagu's own JSON schema cannot express: which queues are allowed, whether a schedule
parses the way Dagu's scheduler parses it (and when it will actually run), that failure handlers exist,
that no credentials are inline, that timeouts and retries are bounded, that `worker_selector` stays at
DAG level, and that `action: exec` steps name one executable. It is meant to run **next to** schema
validation, not instead of it.

Targets the Dagu v2 (snake_case) DAG format. Rules were written against
`dagu-org/dagu@f609cae2b21fdd3cb4f72376e2edb9a8c9e35c45` (2026-10-24): `schemas/dag.schema.json` and the
schedule parser in `internal/ir/schedule.go`. Nothing from Dagu (GPL-3.0) is vendored; CI downloads the
schema at that commit, SHA-256 verified, only to check that the good fixtures are schema-valid DAGs.
Not tested against a running Dagu instance.

```sh
python3 -m venv .venv && .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt
.venv/bin/python -m dagu_lint -c dagu-lint.toml dags/
```

or `pip install .` for a `dagu-lint` command. Python 3.11+ (uses `tomllib`); one dependency, PyYAML.

## GitHub Action

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    persist-credentials: false
- uses: crew-exchange-2079/goblin-workshop/dagu-lint@<commit-sha>
  with:
    paths: |
      dags
    config: dagu-lint.toml      # optional; ./dagu-lint.toml is used when present
    queues: etl, reports        # optional; added to the config's allowlist
    strict: "false"             # "true": warnings fail too
    format: github              # annotations on the PR diff (default); or text / json
```

The action installs Python 3.12 with `update-environment: false` (later steps keep their own Python),
creates a venv under `$RUNNER_TEMP`, installs the hash-pinned PyYAML wheel from
[`requirements.txt`](requirements.txt), and passes inputs to the script only through environment variables.
Pin it to a commit SHA, like any third-party action. The wheels are pinned for manylinux x86_64 and aarch64;
other runners (macOS, Windows) are not supported.

## Output and exit codes

```
dags/etl.yaml:4:1: error DL101 queue 'reprots' is not in the allowlist; did you mean 'reports'?
dags/etl.yaml:3: info schedule 'CRON_TZ=Europe/Berlin 30 2 * * *': at 02:30, every day (Europe/Berlin); next: 2026-10-24T02:30:00+02:00, ...
```

`--format json` gives findings plus a `schedules` list (description and next runs) per file;
`--format github` prints `::error` / `::warning` / `::notice` workflow commands.

| Exit | Meaning |
|------|---------|
| 0 | no errors (warnings allowed unless `--strict`) |
| 1 | at least one error, or a warning under `--strict` |
| 2 | usage or config problem (bad flag, unknown rule code, invalid TOML, unknown `--tz`, no files found) |

Values that trip a secret rule are never printed in any format: findings carry the location, the signal
(key name, credential type, or token length and entropy) and nothing of the value.

## Rules

| Code | Severity | Check |
|------|----------|-------|
| `DL001` | error | YAML syntax error |
| `DL002` | error | duplicate mapping key (the earlier value would be silently dropped) |
| `DL003` | error | document is not a mapping (includes an empty file) |
| `DL004` | error | file cannot be read, is over 1 MiB, or is not UTF-8 |
| `DL101` | error | `queue` not in the allowlist (with a "did you mean") |
| `DL102` | error | `queue` missing; Dagu would fall back to the DAG name |
| `DL103` | error | `queue` is not a non-empty string |
| `DL201` | error | a schedule entry does not parse the way Dagu parses it |
| `DL202` | error | a cron schedule has no fire time in the next five years (e.g. `0 0 30 2 *`) |
| `DL203` | warning | `*/N` on minute or hour where N does not divide 60/24, so it resets at the wrap (Dagu warns too) |
| `DL204` | warning | both day-of-month and day-of-week are restricted, so they are ORed (`0 9 1-7 * MON` is not "first Monday") |
| `DL205` | warning | a one-off `at:` entry is in the past |
| `DL301` | error | `handler_on` missing |
| `DL302` | error | a required `handler_on` event missing (default: `failure`) |
| `DL303` | error | `handler_on` is not a mapping, has an unknown event, or an event that is not a step |
| `DL401` | error | literal value under a secret-looking key (`*_PASSWORD`, `*_TOKEN`, `api_key`, `smtp.password`, ...) |
| `DL402` | error | a known credential format (AWS key id, GitHub/GitLab/Slack/Stripe/Google tokens, PEM private key, JWT, password in a URL) |
| `DL403` | error | a high-entropy token (default ≥ 20 chars, mixed case and digits, ≥ 4.0 bits/char) |
| `DL501` | error | no DAG-level `timeout_sec` |
| `DL502` | error | a DAG or step `timeout_sec` that is not a positive integer, or above the configured maximum |
| `DL503` | warning | a step `timeout_sec` longer than the DAG's |
| `DL601` | error | retry `limit` not a non-negative integer, above the maximum, or missing on a DAG-level `retry_policy` |
| `DL602` | warning | retries with no `interval_sec` |
| `DL603` | warning | `backoff` without `max_interval_sec` (unbounded growth) |
| `DL604` | error | `interval_sec` / `max_interval_sec` / `backoff` invalid, above the maximum, or cap below the interval |
| `DL605` | error | `exit_code` includes 0, so success is retried |
| `DL606` | warning | worst-case retry waits (plus attempts, when the step has a timeout) exceed the DAG `timeout_sec` |
| `DL701` | error | `worker_selector` on a step (including parallel groups, `foreach` bodies, handlers, sub-DAG steps) |
| `DL801` | error | `action: exec` without `with.command`, or `with.command` not a single non-empty string (also the deprecated `exec:` block) |
| `DL802` | error | `with.command` contains whitespace: exec does not split it, arguments go in `with.args` |
| `DL803` | error | `with.command` contains shell syntax (pipes, redirects, `;`, `&&`, globs, `$(...)`): exec has no shell |

Scope per document: in a multi-document file the first document is the DAG and later ones are sub-DAGs.
Queue, schedule, `handler_on` and DAG timeout rules apply to the first document only; step, retry, exec,
`worker_selector` and secret rules apply to every document.

### Schedules

Mirrors Dagu's parser: exactly five fields (no seconds), robfig/cron syntax (`*`, `?`, lists, ranges,
`/step`, `JAN`-`DEC`, `SUN`-`SAT`; day-of-week 0-6, so `7` is an error), the `@yearly` / `@annually` /
`@monthly` / `@weekly` / `@daily` / `@midnight` / `@hourly` descriptors, an optional leading `CRON_TZ=` or
`TZ=` field, and `@every <go duration>` in whole minutes, aligned to the Unix epoch, with no timezone prefix.
Accepts the string, list, `start`/`stop`/`restart` map, `{expression, profile}` and one-off
`{at: RFC3339}` (start only, minute precision, explicit offset) forms. Quartz extensions (`L`, `W`, `#`)
are rejected, as Dagu rejects them.

Schedules without `CRON_TZ=` are evaluated in `--tz` / `[schedule] timezone` (default UTC), which should be
the Dagu server's timezone. Next runs are computed from `--now` (default: the current time).
Known approximation: around DST changes a nonexistent local time is skipped and a repeated one fires once;
robfig's exact behaviour there is not reproduced.

### Secrets

Scans every string value in the file (including `KEY=value` list entries, multi-line `run:` scripts and
`with:` parameters). References are fine: `${VAR}`, `$VAR`, `` `cmd` ``, `$(cmd)`, and anything under
`secrets:`. Keys ending in `_file`, `_path`, `_env`, `_var`, `_ref`, `_name`, `_type` or `_url`, and
flag-like values (`none`, `true`, `disabled`, ...) are not treated as secrets by `DL401`.

This is a heuristic. Hex digests and UUIDs are deliberately not flagged by `DL403`, so a hex-encoded key
under an innocuous name is missed; a long random-looking identifier that is not a secret will be flagged.
Suppress a known false positive on its own line or the line above:

```yaml
params:
  # dagu-lint: disable=DL403  (public test vector)
  - TEST_VECTOR: Qm9vS2xHq7Zp4WxT2vN8rL5yC1dF6jE3
```

`# dagu-lint: disable-file=DL101,DL102` anywhere in a file disables codes for that file. Suppression
comments are found by a text search, so the same text inside a string value also counts.

## Configuration

`dagu-lint.toml` (all keys optional; defaults shown):

```toml
[queues]
allow = []            # empty: queues are required but not checked against a list (a warning says so)
require = true

[handler_on]
require = ["failure"] # any of init, success, failure, abort, exit, wait

[timeouts]
require_dag = true
max_dag_sec = 86400
max_step_sec = 86400

[retry]
max_limit = 5
max_interval_sec = 3600

[schedule]
timezone = "UTC"      # the Dagu server's zone, for schedules without CRON_TZ=
next_runs = 3

[secrets]
entropy_threshold = 4.0
min_length = 20
allow = []            # regexes; a matching token is not reported by DL403

[files]
exclude = []          # globs matched against the path and the file name, e.g. ["base.yaml"]

[rules]
disable = []          # e.g. ["DL204"]
```

CLI flags: `--queue` (repeatable, adds to `allow`), `--disable`, `--exclude`, `--tz`, `--next-runs`,
`--now`, `--strict`, `--format`. Unknown sections, keys or rule codes are a usage error (exit 2).
Directories are searched recursively for `*.yaml` / `*.yml`, skipping dot-directories. Dagu's `base.yaml`
is not a DAG; exclude it.

## YAML details

Files are parsed as YAML 1.2 booleans (only `true`/`false`), matching goccy/go-yaml in Dagu, so `on`, `yes`
and `no` stay strings. Merge keys (`<<`) work and may be overridden; a key repeated explicitly is `DL002`.
Only safe YAML tags are accepted. Files over 1 MiB are refused.

## Tests

```sh
pip install --require-hashes --only-binary=:all: -r requirements.txt && pip install pytest
python -m pytest -q tests
```

[`fixtures/`](fixtures/) holds synthetic DAGs: `good/` must produce no findings, and each file in `bad/`
declares the exact codes it must produce in an `# expect:` header; every rule has at least one bad
fixture. With `DAGU_SCHEMA` pointing at Dagu's `dag.schema.json`, `tests/test_schema.py` also checks
every good fixture against it (CI does this). Provider-format tokens used in tests are assembled at run
time so the repository contains no string a secret scanner would treat as live; the AWS key id in
`fixtures/bad/secret-aws-key.yaml` is AWS's documented example.

## Not checked

Anything the schema already covers (unknown keys, types, enums) beyond what a rule needs; step
dependency graphs (cycles, unknown `depends`); whether commands, images, sub-DAGs or queues exist on the
server; `params` / `env` expansion; global `config.yaml` queue definitions. A clean run means these
policies hold, not that the DAG will run.

MIT licensed; see [LICENSE](LICENSE).
