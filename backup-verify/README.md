# backup-verify

A CLI that proves backups can be restored, not just that they exist.

- **restic**: snapshot freshness, `restic check` (optionally with
  `--read-data-subset`), and a sample restore: N files picked at random from
  the newest snapshot are restored with `restic restore --verify`, sized and
  SHA-256 hashed, and (optionally) compared byte for byte with the live
  source file when it is unchanged since the backup.
- **PostgreSQL**: the newest `pg_dump` file (custom, directory, tar, plain or
  plain `.sql.gz`) or the newest **pgBackRest** backup is restored into a
  throwaway container, and your SQL assertions are run against it.

Results go to a Prometheus textfile (for node_exporter's textfile collector)
and a JSON report. Pure Python 3.10+, standard library only. MIT licensed.

## What a green run means, and what it doesn't

A passing target means: the newest backup is younger than `max_age_hours`;
for restic, the repository passed `restic check` (structure, plus the data
subset you asked for) and every sampled file restored intact; for
PostgreSQL, the backup restored without error into a fresh server and every
assertion held.

It does **not** mean every file in a restic repository is readable (only
`read_data_subset: "100%"` reads every pack, and that is a full download),
that a pgBackRest point-in-time recovery to *any* moment works (only the
backup's own consistency point is restored), or that your assertions are
good ones. Write assertions that would fail on an empty or half-restored
database: row counts with a floor, the newest row's timestamp, a
schema-migration version.

## Install

```sh
pip install ./backup-verify
backup-verify check-config --config /etc/backup-verify/config.json
backup-verify run --config /etc/backup-verify/config.json
```

Exit status: `0` everything passed, `1` at least one check failed, `2`
usage or config error (nothing ran, nothing written). Progress goes to
stderr; `--quiet` silences it. `--report -` prints the JSON report to stdout.
`--target NAME` (repeatable) runs a subset, and the textfile then contains
only that subset, so point `--textfile` elsewhere for ad-hoc runs.

Requirements: `restic` (tested with 0.18.1) for restic targets; a
docker-compatible CLI and daemon for PostgreSQL targets (tested with Docker;
`container_runtime: "podman"` is accepted but untested).

Run it from a systemd timer or cron, once a day after the backups finish.
Alert on the metrics, not on the exit status alone, so a verifier that stops
running is noticed too (see [Alerts](#alerts)).

## Configuration

JSON. Unknown keys are rejected, so a typo can't silently disable a check.
Secrets never go in the config: restic reads `password_file`, and any other
credential is *named* in `pass_env` and taken from the environment.
See [`example-config.json`](example-config.json).

Top level:

| key | default | |
|---|---|---|
| `textfile_path` | none | must end in `.prom`; written atomically |
| `report_path` | none | JSON report, written atomically |
| `work_dir` | system temp | scratch space for restic restores and captured output |
| `container_runtime` | `docker` | `docker`, `podman`, or an absolute path |
| `restic`, `postgres` | `[]` | lists of targets; names must be unique |

### restic targets

| key | default | |
|---|---|---|
| `name`, `repository`, `password_file` | required | `repository` is anything `--repo` accepts |
| `restic_path` | `restic` | |
| `pass_env` | `[]` | env vars handed to restic, e.g. `AWS_ACCESS_KEY_ID` |
| `host`, `tags`, `paths` | none | snapshot filter; a snapshot must carry *all* `tags` |
| `max_age_hours` | `26` | |
| `check` | `true` | run `restic check` |
| `read_data_subset` | none | `"5%"`, `"1/10"`, `"500M"`; passed to `check --read-data-subset` |
| `sample_restore` | `{}` (on, defaults) | `null` disables it |
| `sample_restore.files` | `10` | how many regular files to restore |
| `sample_restore.max_file_bytes` | `256 MiB` | larger files are never picked |
| `sample_restore.compare_to_source` | `false` | also hash the live file, if size and mtime still match the snapshot's |
| `sample_restore.seed` | random | fix the sample (debugging only) |
| `timeout_seconds` | `3600` | per restic command |
| `retry_lock_minutes` | `10` | wait this long for a backup/prune holding the lock |

The sample is drawn by reservoir sampling over `restic ls --json`, so a
snapshot with millions of files is streamed, not held in memory. Restored
files go into a private temporary directory under `work_dir`, removed
afterwards. Only read-side commands are run: `snapshots`, `check`, `ls`,
`restore`.

`compare_to_source` is for a verifier running on the backed-up host. A file
changed since the snapshot is reported as `source_changed` and is not a
failure; a file with the same size and mtime but different bytes is a
`source_mismatch` and fails the check.

### PostgreSQL targets

Common keys:

| key | default | |
|---|---|---|
| `name`, `method`, `image`, `assertions` | required | `method`: `pg_dump` or `pgbackrest` |
| `database` | `verify` | database the assertions run in |
| `superuser` | `postgres` | |
| `max_age_hours` | `26` | dump file mtime, or pgBackRest backup stop time |
| `network` | `none` | container network; pgBackRest with an object-store repo needs one |
| `startup_timeout_seconds` | `300` | |
| `restore_timeout_seconds` | `3600` | pg_restore / psql / pgBackRest restore + WAL replay |
| `statement_timeout_seconds` | `300` | per assertion |
| `pass_env` | `[]` | pgbackrest only: env vars handed to the container (e.g. repo credentials) |

`method: "pg_dump"`:

| key | default | |
|---|---|---|
| `dump` | required | absolute path or glob; the newest match (by mtime) is used |
| `jobs` | `1` | `pg_restore --jobs` (custom and directory formats) |
| `create_roles` | `[]` | roles a plain-format dump's `OWNER TO`/`GRANT` statements need |

`image` must be an official-style `postgres` image at least as new as the
`pg_dump` that wrote the file. Custom, directory and tar dumps are restored
with `pg_restore --exit-on-error --no-owner --no-acl`; plain dumps with
`psql -v ON_ERROR_STOP=1`, after checking the dump ends with pg_dump's
`-- PostgreSQL database dump complete` line (a plain dump cut off at a
statement boundary would otherwise restore "successfully").

`method: "pgbackrest"`:

| key | default | |
|---|---|---|
| `stanza`, `config_file` | required | `config_file` is mounted at `/etc/pgbackrest/pgbackrest.conf` |
| `mounts` | `[]` | `[{"source": "/srv/pgbackrest", "target": "/var/lib/pgbackrest"}]`, read-only |
| `backup_set` | newest | pin a label such as `20261020-010000F` |

`image` must contain the same PostgreSQL major version as the source, plus
`pgbackrest`, `pg_ctl` and `psql` on `PATH` and a `postgres` user that can
read the repository (match its uid to the repository's owner).
[`ci/pgbackrest/Dockerfile`](ci/pgbackrest/Dockerfile) is the one CI uses.
The restore runs
`pgbackrest restore --type=immediate --target-action=promote --archive-mode=off`
into an empty directory inside the container, then starts the server with a
socket-only, trust-auth `pg_hba.conf`, `archive_mode=off` and no TCP
listener, and waits for it to leave recovery. Tablespaces are remapped under
the restore directory. Server settings in the source's `postgresql.conf`
that the image can't satisfy (say a `shared_preload_libraries` entry it
doesn't have) will make the restore fail; build the image to match.

### Assertions

```json
{"name": "users_present", "sql": "SELECT count(*) FROM users", "min": 1000}
{"name": "orders_recent", "sql": "SELECT max(created_at) > now() - interval '2 days' FROM orders"}
{"name": "schema_version", "sql": "SELECT max(version) FROM schema_migrations", "expect": "20261020"}
```

Each query must return exactly one row with one column. With `min` and/or
`max` the value must be a number in range; with `expect` it must equal that
string exactly (psql's text output: booleans are `t`/`f`); with neither it
must be `t`. Queries run as `superuser` in `database` with a statement
timeout. Use read-only SQL: it runs against the throwaway copy, but nothing
stops a `DROP` from making later assertions fail.

## Safety notes

- **Access to the container runtime is root-equivalent.** Run this as a
  dedicated user in the `docker` group (or with rootless Docker/podman) only
  on a host where that is acceptable.
- The throwaway container gets no published ports, network `none` by
  default, backup files and pgBackRest repositories mounted read-only, a
  label `io.goblin-workshop.backup-verify=1`, and is removed with
  `docker rm --force --volumes` whether the run passed, failed or timed out.
  Restored data does land on the Docker host's disk while the check runs.
- pgBackRest restores never archive: `--archive-mode=off` plus
  `archive_mode=off` on the server's command line, and the repository mount
  is read-only anyway.
- Environment variables listed in a pgbackrest target's `pass_env` become
  container environment, visible to `docker inspect` while it runs.
- Child processes get a minimal environment (`PATH`, `LC_ALL=C`, `HOME`, and
  what `pass_env` names), their own process group, and a timeout that kills
  the whole group.
- The JSON report lists sampled file paths and their hashes, and assertion
  values; treat it like the data's metadata.

## Metrics

All gauges, prefixed `backup_verify_`; labels `target`, `kind` (`restic` or
`postgres`), and `check` or `assertion` where relevant.

| metric | |
|---|---|
| `run_success` | 1 if every target passed |
| `last_run_timestamp_seconds`, `run_duration_seconds` | |
| `target_success{target,kind}` | |
| `check_success{target,kind,check}` | checks: `freshness`, `check`, `sample_restore` (restic); `freshness`, `restore`, `assertions` (postgres) |
| `check_skipped{target,kind,check}` | 1 if it could not run because an earlier step failed (counts as failed) |
| `check_duration_seconds{target,kind,check}` | |
| `latest_backup_timestamp_seconds{target,kind}` | export the time, alert on the age |
| `sample_files`, `sample_bytes`, `sample_mismatches`, `sample_source_compared` | restic sample restore |
| `assertion_success{target,kind,assertion}` | |

A stale backup is still restored and asserted: "old but restorable" and
"old and broken" are different pages.

### Alerts

```yaml
- alert: BackupVerifyFailed
  expr: backup_verify_target_success == 0
- alert: BackupVerifyNotRunning
  expr: time() - backup_verify_last_run_timestamp_seconds > 36 * 3600
- alert: BackupTooOld
  expr: time() - backup_verify_latest_backup_timestamp_seconds > 30 * 3600
```

## Development

```sh
pip install -r ../.github/ci-requirements.txt
python -m pytest -q tests/test_unit.py         # no restic, docker or postgres needed

# integration: real restic, rest-server, PostgreSQL and pgBackRest containers
docker build -t backup-verify-pgbackrest:ci ci/pgbackrest
BACKUP_VERIFY_INTEGRATION=1 BACKUP_VERIFY_RESTIC=$(command -v restic) \
  python -m pytest -v tests/integration
```

[`.github/workflows/backup-verify-ci.yml`](../.github/workflows/backup-verify-ci.yml)
runs both, with restic and every image pinned by checksum or digest. The
integration suite pairs each healthy case with a broken one that must fail:
flipped bytes in every restic pack, a file edited in place with its mtime
restored, a wrong password, an old snapshot, a truncated custom dump, a
plain dump cut at a statement boundary, a stale dump file, a failing and an
erroring assertion, and a pgBackRest repository with its WAL archive
removed. It also checks that a pgBackRest restore to the backup's
consistency point excludes a row written after the backup, and that no
container is left behind.

Tested against: restic 0.18.1 (local and REST-server repositories),
PostgreSQL 16, pgBackRest 2.50, Docker. Not tested: other PostgreSQL
majors, object-store repositories, podman, tablespaces.
