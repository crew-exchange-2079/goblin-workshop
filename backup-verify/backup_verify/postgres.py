"""PostgreSQL checks: restore a pg_dump or pgBackRest backup into a
throwaway container and run SQL assertions against it.

The container is started with no published ports, on network "none" unless
the target says otherwise (a pgBackRest repository in object storage needs
one), with backup files bind-mounted read-only. It is always removed
afterwards, including after a timeout or an error. pgBackRest restores run
with --archive-mode=off and --type=immediate, so the restored server never
pushes WAL into the production repository and stops replaying at the first
consistent point.
"""

import glob
import gzip
import json
import os
import re
import secrets
import threading
import time

from . import runner
from .report import FAILED, OK, SKIPPED, Check, Target, timed

PG_PORT = 5432
SOCKET_DIR = "/var/run/postgresql"
DUMP_MOUNT = "/backup/dump"
# pg_dump writes this as the last line of every plain-format dump; a dump
# cut short at a statement boundary would otherwise restore "successfully".
PLAIN_TRAILER = b"-- PostgreSQL database dump complete"
PGBR_BASE = "/var/lib/postgresql/backup-verify"
PGBR_DATA = PGBR_BASE + "/data"
PGBR_TBLSPC = PGBR_BASE + "/tablespaces"
PGBR_HBA = PGBR_BASE + "/pg_hba.conf"
PGBR_LOG = PGBR_BASE + "/server.log"
PGBR_CONF = "/etc/pgbackrest/pgbackrest.conf"
POLL_SECONDS = 1.0
EXEC_TIMEOUT = 60.0


class Failure(Exception):
    """A step failed; the message goes into the check's error."""


def _quote_ident(name):
    # Names are validated against [a-z_][a-z0-9_]* by config; quoting anyway.
    return '"' + name.replace('"', '""') + '"'


class Container:
    def __init__(self, runtime, image, name, network, mounts=(), work_dir=None,
                 run=runner.run, environ=None, sleep=time.sleep, clock=time.monotonic):
        self.runtime, self.image, self.name, self.network = runtime, image, name, network
        self.mounts = mounts
        self.work_dir = work_dir
        self._run = run
        self.sleep, self.clock = sleep, clock
        self.env = runner.build_env(runner.CONTAINER_ENV, environ=environ)
        self.started = False

    def call(self, args, timeout=EXEC_TIMEOUT, env_extra=None, **kw):
        env = dict(self.env, **(env_extra or {}))
        return self._run([self.runtime, *args], timeout, env=env, work_dir=self.work_dir, **kw)

    def start(self, command=(), entrypoint=None, env_names=(), env_extra=None, timeout=600):
        argv = ["run", "--detach", "--rm", f"--name={self.name}", f"--network={self.network}",
                "--label=io.goblin-workshop.backup-verify=1"]
        for src, dst in self.mounts:
            if "," in src or '"' in src:
                raise Failure(f"cannot bind-mount {src!r}: path contains ',' or '\"'")
            argv.append(f"--mount=type=bind,source={src},target={dst},readonly")
        if entrypoint:
            argv.append(f"--entrypoint={entrypoint}")
        # "-e NAME" without a value: the CLI copies it from its own
        # environment, so the value never shows up in a process listing.
        for n in env_names:
            argv += ["-e", n]
        argv += [self.image, *command]
        # Set before running: a "docker run" that times out may still have
        # created the container, and "rm --force" of a missing name is harmless.
        self.started = True
        res = self.call(argv, timeout=timeout, env_extra=env_extra)
        if not res.ok:
            raise Failure(f"could not start container: {res.describe()}")

    def exec(self, args, user=None, env=None, stdin=None, timeout=EXEC_TIMEOUT, **kw):
        argv = ["exec"]
        if stdin is not None:
            argv.append("--interactive")
        if user:
            argv.append(f"--user={user}")
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        argv += [self.name, *args]
        return self.call(argv, timeout=timeout, stdin=stdin, **kw)

    def running(self):
        res = self.call(["inspect", "--format={{.State.Running}}", self.name], timeout=30)
        return res.ok and res.stdout.strip() == b"true"

    def logs_tail(self, lines=20):
        res = self.call(["logs", f"--tail={lines}", self.name], timeout=30)
        return (res.stdout.decode("utf-8", "replace") + res.stderr_tail)[-3000:]

    def remove(self):
        if self.started:
            self.call(["rm", "--force", "--volumes", self.name], timeout=120)
            self.started = False


def new_container(cfg, target, mounts, run, environ, created):
    """A Container, registered in created so verify() always removes it."""
    name = f"backup-verify-{target.name.lower()}-{secrets.token_hex(4)}"
    name = re.sub(r"[^a-z0-9_.-]", "-", name)
    c = Container(cfg.container_runtime, target.image, name, target.network, mounts,
                  cfg.work_dir, run, environ)
    created.append(c)
    return c


# --- dump selection and format ---

def select_dump(pattern):
    """Newest file or directory matching pattern (a path, or a glob)."""
    if glob.has_magic(pattern):
        matches = [p for p in glob.glob(pattern) if os.path.isfile(p) or os.path.isdir(p)]
    else:
        matches = [pattern] if os.path.exists(pattern) else []
    if not matches:
        return None, None
    best = max(matches, key=lambda p: os.stat(p).st_mtime_ns)
    return best, os.stat(best).st_mtime


def dump_format(path):
    if os.path.isdir(path):
        return "directory" if os.path.isfile(os.path.join(path, "toc.dat")) else None
    with open(path, "rb") as fh:
        head = fh.read(512)
    if head.startswith(b"PGDMP"):
        return "custom"
    if head.startswith(b"\x1f\x8b"):
        return "plain_gzip"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return "plain"


def _tail_has_trailer(path):
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 4096))
        return PLAIN_TRAILER in fh.read()


class _GzipFeeder:
    """Decompress a .sql.gz into a pipe for psql, noting whether it ended cleanly."""

    def __init__(self, path):
        self.path = path
        self.rfd, self.wfd = os.pipe()
        self.error = None
        self.tail = b""
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def _pump(self):
        try:
            with gzip.open(self.path, "rb") as src, os.fdopen(self.wfd, "wb") as dst:
                while chunk := src.read(1 << 20):
                    self.tail = (self.tail + chunk)[-4096:]
                    dst.write(chunk)
        except BrokenPipeError:
            self.error = "psql stopped reading"
        except (OSError, EOFError, gzip.BadGzipFile) as e:
            self.error = f"{type(e).__name__}: {e}"

    def __enter__(self):
        self.reader = os.fdopen(self.rfd, "rb")
        self.thread.start()
        return self.reader

    def __exit__(self, *exc):
        # Closing our read end first unblocks a writer whose reader never
        # started or died early (it gets EPIPE instead of hanging).
        self.reader.close()
        self.thread.join(timeout=30)
        return False


# --- psql helpers ---

FIELD_SEP = "\x1f"


def psql(container, sql, user, db, timeout, statement_timeout=None):
    """Run one SQL string; return rows as lists of strings. Raises Failure.

    Rows are NUL-terminated and fields separated by US (0x1f), so values with
    newlines still parse.
    """
    env = {"PGOPTIONS": f"-c statement_timeout={int(statement_timeout * 1000)}"} \
        if statement_timeout else None
    res = container.exec(
        ["psql", "--no-psqlrc", "--quiet", "--no-align", "--tuples-only",
         f"--field-separator={FIELD_SEP}", "--record-separator-zero",
         "--set=ON_ERROR_STOP=1", f"--host={SOCKET_DIR}", f"--port={PG_PORT}",
         f"--username={user}", f"--dbname={db}", f"--command={sql}"],
        env=env, timeout=timeout)
    if not res.ok:
        raise Failure(res.describe())
    if res.stdout_truncated:
        raise Failure("query output too large")
    return parse_rows(res.stdout.decode("utf-8", "replace"))


def parse_rows(out):
    """psql -A -t --record-separator-zero output: every record ends in NUL."""
    if not out:
        return []
    if out.endswith("\x00"):
        out = out[:-1]
    return [rec.split(FIELD_SEP) for rec in out.split("\x00")]


def evaluate(assertion, rows):
    """(passed, value, reason)"""
    if len(rows) != 1 or len(rows[0]) != 1:
        return False, None, f"expected exactly one value, got {len(rows)} row(s)/field(s)"
    value = rows[0][0]
    if assertion.min is not None or assertion.max is not None:
        try:
            num = float(value)
        except ValueError:
            return False, value, f"not a number: {value[:100]!r}"
        if num != num:
            return False, value, "NaN"
        if assertion.min is not None and num < assertion.min:
            return False, value, f"{num:g} < min {assertion.min:g}"
        if assertion.max is not None and num > assertion.max:
            return False, value, f"{num:g} > max {assertion.max:g}"
        return True, value, ""
    expect = "t" if assertion.expect is None else assertion.expect
    if value != expect:
        return False, value, f"got {value[:100]!r}, expected {expect!r}"
    return True, value, ""


def run_assertions(container, target, db, check, result):
    results = []
    failed = 0
    for a in target.assertions:
        entry = {"name": a.name}
        try:
            rows = psql(container, a.sql, target.superuser, db,
                        timeout=target.statement_timeout_seconds + 30,
                        statement_timeout=target.statement_timeout_seconds)
            passed, value, reason = evaluate(a, rows)
        except Failure as e:
            passed, value, reason = False, None, str(e)
        entry["passed"] = passed
        if value is not None:
            entry["value"] = value[:200]
        if reason:
            entry["reason"] = reason
        failed += not passed
        results.append(entry)
        result.assertions.append((a.name, passed))
    check.details["assertions"] = results
    if failed:
        check.error = f"{failed} of {len(results)} assertions failed"
    else:
        check.status = OK


def wait_until(container, probe, timeout, what):
    deadline = container.clock() + timeout
    while True:
        if probe():
            return
        if not container.running():
            raise Failure(f"container exited while waiting for {what}: "
                          f"{container.logs_tail()}")
        if container.clock() >= deadline:
            raise Failure(f"timed out after {timeout:.0f}s waiting for {what}")
        container.sleep(POLL_SECONDS)


# --- targets ---

def verify(cfg, target, run=runner.run, now=time.time, environ=None):
    """Run every check for one postgres target. Never raises."""
    result = Target(target.name, "postgres")
    fresh = Check("freshness")
    restore = Check("restore")
    asserts = Check("assertions")
    result.checks += [fresh, restore, asserts]
    created = []
    steps = _pg_dump if target.method == "pg_dump" else _pgbackrest
    try:
        steps(cfg, target, run, now, environ, created, result, fresh, restore, asserts)
    except Exception as e:  # report, never crash the run
        for c in (fresh, restore, asserts):
            if c.status == FAILED and not c.error:
                c.error = f"internal error: {type(e).__name__}: {e}"
                break
    finally:
        for c in created:
            c.remove()
    for c in (restore, asserts):
        if c.status == FAILED and not c.error:
            c.status = SKIPPED
            c.error = "not run: an earlier step failed"
    return result


def _freshness_from(check, result, ts, max_age_hours, now):
    result.latest_backup_timestamp = ts
    age = now() - ts
    check.details.update({"age_hours": round(age / 3600, 3), "max_age_hours": max_age_hours})
    if age > max_age_hours * 3600:
        check.error = f"newest backup is {age / 3600:.1f}h old (limit {max_age_hours}h)"
    else:
        check.status = OK


def _pg_dump(cfg, target, run, now, environ, created, result, fresh, restore, asserts):
    with timed(fresh):
        path, mtime = select_dump(target.dump)
        if path is None:
            fresh.error = f"no dump matches {target.dump}"
            return
        fmt = dump_format(path)
        fresh.details.update({"dump": path, "format": fmt})
        if fmt is None:
            fresh.error = f"{path}: directory without toc.dat is not a pg_dump directory"
            return
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            fresh.error = f"{path}: empty file"
            return
        _freshness_from(fresh, result, mtime, target.max_age_hours, now)
    # A stale dump is still restored: "old but restorable" and "old and
    # broken" are different pages for whoever gets alerted.

    container = new_container(cfg, target, [(path, DUMP_MOUNT)], run, environ, created)
    with timed(restore):
        try:
            # POSTGRES_PASSWORD is required by the image, never used: every
            # client below connects over the local socket (trust).
            container.start(env_names=["POSTGRES_PASSWORD"],
                            env_extra={"POSTGRES_PASSWORD": secrets.token_urlsafe(24)},
                            timeout=target.startup_timeout_seconds)
            # TCP readiness, not the socket: the image's entrypoint runs a
            # temporary socket-only server during initdb, then restarts.
            wait_until(container, lambda: container.exec(
                ["pg_isready", "--quiet", "--host=127.0.0.1", f"--port={PG_PORT}"],
                timeout=30).ok, target.startup_timeout_seconds, "PostgreSQL to accept connections")
            _restore_dump(container, target, path, fmt, restore)
        except Failure as e:
            restore.error = str(e)
            return
    with timed(asserts):
        run_assertions(container, target, target.database, asserts, result)


def _restore_dump(container, target, path, fmt, check):
    su, db, t = target.superuser, target.database, target.restore_timeout_seconds
    for role in target.create_roles:
        psql(container, f"CREATE ROLE {_quote_ident(role)}", su, "postgres", EXEC_TIMEOUT)
    if db != "postgres":
        psql(container, f"CREATE DATABASE {_quote_ident(db)}", su, "postgres", EXEC_TIMEOUT)
    base = ["--no-psqlrc", "--quiet", "--set=ON_ERROR_STOP=1", f"--host={SOCKET_DIR}",
            f"--username={su}", f"--dbname={db}"]
    if fmt in ("custom", "directory", "tar"):
        jobs = [f"--jobs={target.jobs}"] if target.jobs > 1 and fmt != "tar" else []
        # Roles and grants of the source cluster don't exist here; restoring
        # them is not what this check is about.
        res = container.exec(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                              f"--host={SOCKET_DIR}", f"--username={su}", f"--dbname={db}",
                              *jobs, DUMP_MOUNT], timeout=t)
    elif fmt == "plain":
        if not _tail_has_trailer(path):
            raise Failure("plain dump does not end with pg_dump's completion marker "
                          "(truncated?)")
        res = container.exec(["psql", *base, f"--file={DUMP_MOUNT}"], timeout=t)
    else:
        feeder = _GzipFeeder(path)
        with feeder as pipe_r:
            res = container.exec(["psql", *base, "--file=-"], stdin=pipe_r, timeout=t)
        if feeder.error:
            raise Failure(f"reading {os.path.basename(path)}: {feeder.error}")
        if PLAIN_TRAILER not in feeder.tail:
            raise Failure("plain dump does not end with pg_dump's completion marker "
                          "(truncated?)")
    if not res.ok:
        check.details["stderr_tail"] = res.stderr_tail[-2000:]
        raise Failure(res.describe())
    check.status = OK


def _pgbackrest(cfg, target, run, now, environ, created, result, fresh, restore, asserts):
    mounts = [(target.config_file, PGBR_CONF)] + [(m.source, m.target) for m in target.mounts]
    container = new_container(cfg, target, mounts, run, environ, created)
    pgbr = ["pgbackrest", f"--stanza={target.stanza}", f"--config={PGBR_CONF}",
            "--log-level-console=warn", "--log-level-file=off", "--log-level-stderr=warn"]
    with timed(fresh):
        try:
            container.start(command=["infinity"], entrypoint="sleep",
                            env_names=target.pass_env, env_extra=_passed(target, environ),
                            timeout=target.startup_timeout_seconds)
            res = container.exec([*pgbr, "--output=json", "info"], user="postgres", timeout=300)
            if not res.ok:
                raise Failure(res.describe())
            label, ts, info = _latest_backup(res.stdout, target.stanza)
        except Failure as e:
            fresh.error = str(e)
            return
        fresh.details.update(info)
        _freshness_from(fresh, result, ts, target.max_age_hours, now)
        if target.backup_set:
            fresh.details["restoring_set"] = target.backup_set

    with timed(restore):
        try:
            res = container.exec(["install", "-d", "-o", "postgres", "-g", "postgres", "-m", "700",
                                  PGBR_BASE, PGBR_DATA, PGBR_TBLSPC], user="root")
            if not res.ok:
                raise Failure(res.describe())
            restore_args = [*pgbr, f"--pg1-path={PGBR_DATA}", "--type=immediate",
                            "--target-action=promote", "--archive-mode=off",
                            f"--tablespace-map-all={PGBR_TBLSPC}"]
            if target.backup_set:
                restore_args.append(f"--set={target.backup_set}")
            res = container.exec([*restore_args, "restore"], user="postgres",
                                 timeout=target.restore_timeout_seconds)
            if not res.ok:
                restore.details["stderr_tail"] = res.stderr_tail[-2000:]
                raise Failure(res.describe())
            # The source's pg_hba.conf may only allow peer/password auth; the
            # throwaway server gets a socket-only trust file instead.
            res = container.exec(["sh", "-c", 'cat > "$1"', "sh", PGBR_HBA], user="postgres",
                                 stdin=b"local all all trust\n")
            if not res.ok:
                raise Failure(res.describe())
            opts = " ".join([
                "-c listen_addresses=''", f"-c port={PG_PORT}",
                f"-c unix_socket_directories={SOCKET_DIR}", f"-c hba_file={PGBR_HBA}",
                "-c archive_mode=off", "-c hot_standby=on"])
            res = container.exec(["pg_ctl", "start", f"--pgdata={PGBR_DATA}", "--wait",
                                  f"--timeout={int(target.startup_timeout_seconds)}",
                                  f"--log={PGBR_LOG}", f"--options={opts}"],
                                 user="postgres", timeout=target.startup_timeout_seconds + 30)
            if not res.ok:
                raise Failure(f"{res.describe()}; server log: {_server_log(container)}")

            def promoted():
                try:
                    rows = psql(container, "SELECT pg_is_in_recovery()", target.superuser,
                                "postgres", 30)
                except Failure:
                    return False
                return rows == [["f"]]

            try:
                wait_until(container, promoted, target.restore_timeout_seconds,
                           "WAL replay to reach consistency and promote")
            except Failure as e:
                raise Failure(f"{e}; server log: {_server_log(container)}") from None
            restore.status = OK
        except Failure as e:
            restore.error = str(e)
            return
    with timed(asserts):
        run_assertions(container, target, target.database, asserts, result)


def _passed(target, environ):
    environ = os.environ if environ is None else environ
    return {n: environ[n] for n in target.pass_env if n in environ}


def _server_log(container):
    """The server log's FATAL/PANIC/ERROR lines (else its last lines), briefly."""
    res = container.exec(["tail", "-n", "200", PGBR_LOG], user="postgres", timeout=30)
    if not res.ok:
        return "(unavailable)"
    lines = res.stdout.decode("utf-8", "replace").splitlines()
    bad = [ln for ln in lines if re.search(r"\b(FATAL|PANIC|ERROR):", ln)]
    return " | ".join((bad or lines)[-5:])[-2000:]


def _latest_backup(raw, stanza):
    """(label, stop timestamp, details) of the newest backup. Raises Failure."""
    try:
        info = json.loads(raw)
        st = next(s for s in info if s.get("name") == stanza)
    except (ValueError, StopIteration, TypeError, AttributeError):
        raise Failure(f"pgbackrest info: no stanza {stanza!r} in output") from None
    backups = [b for b in st.get("backup") or [] if isinstance(b, dict)]
    if not backups:
        status = (st.get("status") or {}).get("message", "")
        raise Failure(f"pgbackrest info: stanza has no backups ({status})")
    try:
        newest = max(backups, key=lambda b: b["timestamp"]["stop"])
        ts = float(newest["timestamp"]["stop"])
    except (KeyError, TypeError, ValueError):
        raise Failure("pgbackrest info: backup without timestamp.stop") from None
    details = {"latest_label": newest.get("label"), "latest_type": newest.get("type"),
               "backups": len(backups)}
    if newest.get("error"):
        # pgBackRest flags backups in which it found page checksum errors.
        raise Failure(f"pgbackrest info: newest backup {newest.get('label')} is flagged "
                      "with errors (page checksum failures during backup)")
    return newest.get("label"), ts, details
