"""End-to-end tests against real restic, PostgreSQL and pgBackRest.

Skipped unless BACKUP_VERIFY_INTEGRATION=1. Needs a docker daemon and a
restic binary; CI provides both (see .github/workflows/backup-verify-ci.yml).

    BACKUP_VERIFY_RESTIC        restic binary            (default: restic on PATH)
    BACKUP_VERIFY_PG_IMAGE      official postgres image  (pg_dump restores)
    BACKUP_VERIFY_PGBR_IMAGE    image with postgres + pgbackrest (ci/pgbackrest)
    BACKUP_VERIFY_REST_IMAGE    restic rest-server image

Every case runs the CLI as a subprocess and checks its exit status, the JSON
report and the textfile. Each healthy case has a broken twin (corrupted
pack, truncated dump, missing WAL, wrong assertion) that must fail: a
verifier that only ever says "ok" is worse than none.
"""

import gzip
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("BACKUP_VERIFY_INTEGRATION") != "1",
                                reason="set BACKUP_VERIFY_INTEGRATION=1 (needs docker + restic)")

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESTIC = os.environ.get("BACKUP_VERIFY_RESTIC") or shutil.which("restic") or "restic"
PG_IMAGE = os.environ.get("BACKUP_VERIFY_PG_IMAGE", "postgres:16-bookworm")
PGBR_IMAGE = os.environ.get("BACKUP_VERIFY_PGBR_IMAGE", "backup-verify-pgbackrest:ci")
REST_IMAGE = os.environ.get("BACKUP_VERIFY_REST_IMAGE", "restic/rest-server:0.14.0")
LABEL = "io.goblin-workshop.backup-verify"
ROWS = 5000


def sh(*argv, check=True, **kw):
    kw.setdefault("capture_output", True)
    res = subprocess.run(list(argv), **kw)
    if check and res.returncode != 0:
        raise AssertionError(f"{argv} -> {res.returncode}\n{res.stdout!r}\n{res.stderr!r}")
    return res


def run_tool(tmp, cfg):
    """Run the CLI; return (exit status, report dict, textfile text)."""
    cfg = dict(cfg)
    cfg.setdefault("work_dir", tmp)
    cfg["textfile_path"] = os.path.join(tmp, "backup_verify.prom")
    cfg["report_path"] = os.path.join(tmp, "report.json")
    path = os.path.join(tmp, f"config-{uuid.uuid4().hex[:6]}.json")
    with open(path, "w") as fh:
        json.dump(cfg, fh)
    res = subprocess.run([sys.executable, "-m", "backup_verify", "run", "--config", path],
                         cwd=PROJECT, capture_output=True, text=True, timeout=1800)
    sys.stderr.write(res.stderr)
    with open(cfg["report_path"]) as fh:
        rep = json.load(fh)
    with open(cfg["textfile_path"]) as fh:
        prom = fh.read()
    return res.returncode, rep, prom


def checks(rep, target=None):
    t = rep["targets"][0] if target is None else next(x for x in rep["targets"] if x["name"] == target)
    return {c["name"]: c for c in t["checks"]}


def status(rep, target=None):
    return {k: v["status"] for k, v in checks(rep, target).items()}


def metric(prom, name, **labels):
    want = ",".join(f'{k}="{v}"' for k, v in labels.items())
    prefix = f"backup_verify_{name}" + (f"{{{want}" if labels else "")
    for line in prom.splitlines():
        if line.startswith(prefix) and (labels or line[len(prefix)] == " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{prefix} not in textfile:\n{prom}")


@pytest.fixture(scope="session")
def tmp():
    d = tempfile.mkdtemp(prefix="bv-it-")
    os.chmod(d, 0o755)
    yield d
    # Some files are written by container users; remove them from inside.
    subprocess.run(["docker", "run", "--rm", f"--mount=type=bind,source={d},target=/x",
                    "--entrypoint=sh", PGBR_IMAGE, "-c", "rm -rf /x/* /x/.[!.]*"],
                   capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def no_leftover_containers():
    yield
    left = sh("docker", "ps", "-a", "--quiet", f"--filter=label={LABEL}").stdout.split()
    if left:
        sh("docker", "rm", "-f", *[c.decode() for c in left], check=False)
    assert not left, "backup-verify left containers behind"


# --- restic ---

def make_tree(root, n=120, seed=1):
    rng = random.Random(seed)
    names = ["plain.txt", "we[ir]d dir/f*?.txt", "back\\slash", "new\nline", "ünïcode.bin",
             "-starts-with-dash", "empty"]
    for i in range(n):
        names.append(f"d{i % 7}/file-{i}.dat")
    for name in names:
        p = os.path.join(root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        size = 0 if name == "empty" else rng.choice([1, 100, 4096, 70000, 300000])
        with open(p, "wb") as fh:
            fh.write(rng.randbytes(size))
    return names


@pytest.fixture(scope="session")
def restic_env(tmp):
    pw = os.path.join(tmp, "restic-password")
    with open(pw, "w") as fh:
        fh.write("integration-test-password\n")
    env = dict(os.environ, RESTIC_PASSWORD_FILE=pw, RESTIC_PROGRESS_FPS="0.0167")
    return pw, env


@pytest.fixture(scope="session")
def rest_server(tmp):
    data = os.path.join(tmp, "rest-data")
    os.makedirs(data)
    name = f"bv-it-rest-{uuid.uuid4().hex[:6]}"
    sh("docker", "run", "-d", "--rm", f"--name={name}", "-p", "127.0.0.1::8000",
       "-e", "DISABLE_AUTHENTICATION=1", f"--mount=type=bind,source={data},target=/data",
       REST_IMAGE)
    try:
        port = sh("docker", "port", name, "8000/tcp").stdout.decode().splitlines()[0].rsplit(":", 1)[1]
        yield f"rest:http://127.0.0.1:{port}/"
    finally:
        sh("docker", "rm", "-f", name, check=False)


def restic(env, repo, *args):
    return sh(RESTIC, f"--repo={repo}", "--quiet", *args, env=env)


@pytest.fixture(scope="session")
def rest_repo(tmp, restic_env, rest_server):
    pw, env = restic_env
    src = os.path.join(tmp, "src-rest")
    make_tree(src)
    repo = rest_server + "healthy"
    deadline = time.time() + 30
    while sh(RESTIC, f"--repo={repo}", "init", env=env, check=False).returncode != 0:
        assert time.time() < deadline, "rest-server did not come up"
        time.sleep(1)
    restic(env, repo, "backup", "--host=it", "--tag=nightly", src)
    # An old snapshot under another tag, for the freshness case.
    restic(env, repo, "backup", "--host=it", "--tag=ancient", "--time=2020-01-01 00:00:00", src)
    return repo, src, pw


def restic_target(repo, pw, **kw):
    t = {"name": "files", "repository": repo, "password_file": pw, "restic_path": RESTIC,
         "host": "it", "tags": ["nightly"], "read_data_subset": "100%",
         "sample_restore": {"files": 200, "compare_to_source": True}, "retry_lock_minutes": 0}
    t.update(kw)
    return t


def test_restic_healthy_rest_server(tmp, rest_repo):
    repo, src, pw = rest_repo
    rc, rep, prom = run_tool(tmp, {"restic": [restic_target(repo, pw)]})
    assert rc == 0, rep
    assert status(rep) == {"freshness": "ok", "check": "ok", "sample_restore": "ok"}
    sample = checks(rep)["sample_restore"]["details"]
    paths = {f["path"] for f in sample["files"]}
    # Every file (files=200 > 127) was sampled, the awkward names included,
    # and each was compared byte-for-byte against the untouched source.
    assert os.path.join(src, "we[ir]d dir/f*?.txt") in paths
    assert os.path.join(src, "new\nline") in paths
    assert sample["source_compared"] == len(sample["files"]) == 127
    assert all(f["status"] == "source_match" for f in sample["files"])
    assert metric(prom, "run_success") == 1
    assert metric(prom, "check_success", target="files", kind="restic", check="check") == 1
    assert metric(prom, "sample_files", target="files", kind="restic") == 127
    assert metric(prom, "sample_mismatches", target="files", kind="restic") == 0
    ts = metric(prom, "latest_backup_timestamp_seconds", target="files", kind="restic")
    assert abs(ts - time.time()) < 3600


def test_restic_stale_snapshot(tmp, rest_repo):
    repo, _, pw = rest_repo
    t = restic_target(repo, pw, tags=["ancient"], check=False, read_data_subset=None,
                      sample_restore={"files": 3})
    rc, rep, prom = run_tool(tmp, {"restic": [t]})
    assert rc == 1
    assert status(rep) == {"freshness": "failed", "sample_restore": "ok"}
    assert "old (limit 26" in checks(rep)["freshness"]["error"]
    assert metric(prom, "latest_backup_timestamp_seconds", target="files", kind="restic") == 1577836800


def test_restic_wrong_password(tmp, rest_repo):
    repo, _, _ = rest_repo
    bad = os.path.join(tmp, "wrong-password")
    with open(bad, "w") as fh:
        fh.write("nope\n")
    rc, rep, _ = run_tool(tmp, {"restic": [restic_target(repo, bad)]})
    assert rc == 1
    assert status(rep) == {"freshness": "failed", "check": "failed", "sample_restore": "skipped"}


def test_restic_source_changed_in_place(tmp, restic_env):
    """Same size, same mtime, different bytes: only the hash comparison catches it."""
    pw, env = restic_env
    src = os.path.join(tmp, "src-local")
    make_tree(src, n=10, seed=2)
    repo = os.path.join(tmp, "repo-local")
    restic(env, repo, "init")
    restic(env, repo, "backup", "--host=it", "--tag=nightly", src)
    victim = os.path.join(src, "plain.txt")
    st = os.stat(victim)
    with open(victim, "r+b") as fh:
        b = fh.read(1)
        fh.seek(0)
        fh.write(bytes([b[0] ^ 0xFF]))
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    t = restic_target(repo, pw, check=False, read_data_subset=None)
    rc, rep, prom = run_tool(tmp, {"restic": [t]})
    assert rc == 1
    files = {f["path"]: f["status"] for f in checks(rep)["sample_restore"]["details"]["files"]}
    assert files[victim] == "source_mismatch"
    assert metric(prom, "sample_mismatches", target="files", kind="restic") == 1


def test_restic_corrupted_pack(tmp, restic_env):
    pw, env = restic_env
    src = os.path.join(tmp, "src-corrupt")
    make_tree(src, n=20, seed=3)
    repo = os.path.join(tmp, "repo-corrupt")
    restic(env, repo, "init")
    restic(env, repo, "backup", "--host=it", "--tag=nightly", src)
    # Flip 64 bytes a third of the way into every pack file.
    packs = []
    for d, _, names in os.walk(os.path.join(repo, "data")):
        packs += [os.path.join(d, n) for n in names]
    assert packs
    for p in packs:
        os.chmod(p, 0o644)
        size = os.path.getsize(p)
        with open(p, "r+b") as fh:
            fh.seek(size // 3)
            chunk = fh.read(64)
            fh.seek(size // 3)
            fh.write(bytes(c ^ 0x5A for c in chunk))
    rc, rep, prom = run_tool(tmp, {"restic": [restic_target(repo, pw)]})
    assert rc == 1
    st = status(rep)
    assert st["freshness"] == "ok"   # snapshot files are not packs
    assert st["check"] == "failed" and st["sample_restore"] == "failed", rep
    assert metric(prom, "check_success", target="files", kind="restic", check="check") == 0
    assert metric(prom, "run_success") == 0


# --- PostgreSQL: pg_dump ---

SEED_SQL = f"""
CREATE ROLE app;
CREATE TABLE users (id int PRIMARY KEY, email text NOT NULL, created timestamptz NOT NULL);
INSERT INTO users SELECT g, 'user' || g || '@example.invalid', now() - g * interval '1 minute'
  FROM generate_series(1, {ROWS}) g;
CREATE INDEX users_email ON users (email);
CREATE VIEW recent AS SELECT * FROM users WHERE created > now() - interval '1 day';
CREATE FUNCTION user_count() RETURNS bigint LANGUAGE sql AS 'SELECT count(*) FROM users';
ALTER TABLE users OWNER TO app;
CREATE TABLE schema_migrations (version text);
INSERT INTO schema_migrations VALUES ('20261020');
"""


def pg_wait(name, user="postgres", host="127.0.0.1", timeout=120):
    deadline = time.time() + timeout
    while sh("docker", "exec", name, "pg_isready", "-q", f"--host={host}", check=False).returncode:
        assert time.time() < deadline, f"{name} did not become ready"
        time.sleep(1)


@pytest.fixture(scope="session")
def source_pg():
    name = f"bv-it-src-{uuid.uuid4().hex[:6]}"
    sh("docker", "run", "-d", "--rm", f"--name={name}", "-e", "POSTGRES_PASSWORD=src",
       "-e", "POSTGRES_DB=app", PG_IMAGE)
    try:
        pg_wait(name)
        sh("docker", "exec", "-i", name, "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres",
           "-d", "app", input=SEED_SQL.encode())
        yield name
    finally:
        sh("docker", "rm", "-f", name, check=False)


def pg_dump_to(name, path, *flags):
    with open(path, "wb") as fh:
        sh("docker", "exec", name, "pg_dump", "-U", "postgres", "-d", "app", *flags,
           stdout=fh, capture_output=False, stderr=subprocess.PIPE)


@pytest.fixture(scope="session")
def dumps(tmp, source_pg):
    d = os.path.join(tmp, "dumps")
    os.makedirs(d)
    out = {"custom": os.path.join(d, "app-20261023.dump"),
           "plain": os.path.join(d, "app-20261023.sql")}
    pg_dump_to(source_pg, out["custom"], "-Fc")
    pg_dump_to(source_pg, out["plain"], "-Fp")
    out["plain_gzip"] = out["plain"] + ".gz"
    with open(out["plain"], "rb") as src, gzip.open(out["plain_gzip"], "wb") as dst:
        shutil.copyfileobj(src, dst)
    dirfmt = os.path.join(d, "app-dir")
    sh("docker", "exec", source_pg, "pg_dump", "-U", "postgres", "-d", "app", "-Fd", "-j", "2",
       "-f", "/tmp/app-dir")
    sh("docker", "cp", f"{source_pg}:/tmp/app-dir", dirfmt)
    out["directory"] = dirfmt
    return out


ASSERTIONS = [
    {"name": "user_rows", "sql": "SELECT count(*) FROM users", "min": ROWS, "max": ROWS},
    {"name": "function_works", "sql": f"SELECT user_count() = {ROWS}"},
    {"name": "view_works", "sql": "SELECT count(*) > 0 FROM recent"},
    {"name": "index_present",
     "sql": "SELECT count(*) FROM pg_indexes WHERE indexname = 'users_email'", "expect": "1"},
    {"name": "schema_version", "sql": "SELECT max(version) FROM schema_migrations",
     "expect": "20261020"},
]


def pg_target(path, **kw):
    t = {"name": "app", "method": "pg_dump", "dump": path, "image": PG_IMAGE, "database": "app",
         "assertions": ASSERTIONS, "startup_timeout_seconds": 180,
         "restore_timeout_seconds": 600}
    t.update(kw)
    return t


@pytest.mark.parametrize("fmt", ["custom", "plain", "plain_gzip", "directory"])
def test_pg_dump_formats_restore(tmp, dumps, fmt):
    extra = {"create_roles": ["app"]} if fmt.startswith("plain") else {"jobs": 2}
    rc, rep, prom = run_tool(tmp, {"postgres": [pg_target(dumps[fmt], **extra)]})
    assert rc == 0, json.dumps(rep, indent=1)
    assert status(rep) == {"freshness": "ok", "restore": "ok", "assertions": "ok"}
    assert checks(rep)["freshness"]["details"]["format"] == fmt
    for a in ASSERTIONS:
        assert metric(prom, "assertion_success", target="app", kind="postgres",
                      assertion=a["name"]) == 1


def test_pg_dump_glob_picks_newest(tmp, dumps):
    old = os.path.join(os.path.dirname(dumps["custom"]), "app-20200101.dump")
    with open(old, "wb") as fh:
        fh.write(b"PGDMP garbage that would fail to restore")
    os.utime(old, (0, 0))
    pattern = os.path.join(os.path.dirname(dumps["custom"]), "app-*.dump")
    rc, rep, _ = run_tool(tmp, {"postgres": [pg_target(pattern)]})
    assert rc == 0, rep
    assert checks(rep)["freshness"]["details"]["dump"] == dumps["custom"]


def test_pg_dump_failing_assertion(tmp, dumps):
    bad = ASSERTIONS + [{"name": "too_many_users", "sql": "SELECT count(*) FROM users",
                         "min": ROWS + 1},
                        {"name": "missing_table", "sql": "SELECT count(*) FROM nope", "min": 0}]
    rc, rep, prom = run_tool(tmp, {"postgres": [pg_target(dumps["custom"], assertions=bad)]})
    assert rc == 1
    assert status(rep) == {"freshness": "ok", "restore": "ok", "assertions": "failed"}
    by_name = {a["name"]: a for a in checks(rep)["assertions"]["details"]["assertions"]}
    assert by_name["user_rows"]["passed"] and by_name["user_rows"]["value"] == str(ROWS)
    assert not by_name["too_many_users"]["passed"]
    assert "does not exist" in by_name["missing_table"]["reason"]
    assert metric(prom, "assertion_success", target="app", kind="postgres",
                  assertion="too_many_users") == 0
    assert metric(prom, "assertion_success", target="app", kind="postgres",
                  assertion="user_rows") == 1


def test_pg_dump_truncated_custom(tmp, dumps):
    bad = os.path.join(tmp, "truncated.dump")
    with open(dumps["custom"], "rb") as src, open(bad, "wb") as dst:
        data = src.read()
        dst.write(data[: len(data) * 2 // 3])
    rc, rep, _ = run_tool(tmp, {"postgres": [pg_target(bad)]})
    assert rc == 1
    assert status(rep) == {"freshness": "ok", "restore": "failed", "assertions": "skipped"}


def test_pg_dump_truncated_plain_at_statement_boundary(tmp, dumps):
    """psql would happily "restore" this; the completion-marker check must not."""
    with open(dumps["plain"], "rb") as fh:
        data = fh.read()
    cut = data.rfind(b";\n", 0, len(data) // 2) + 2
    bad = os.path.join(tmp, "truncated.sql.gz")
    with gzip.open(bad, "wb") as fh:
        fh.write(data[:cut])
    rc, rep, _ = run_tool(tmp, {"postgres": [pg_target(bad, create_roles=["app"])]})
    assert rc == 1
    assert "truncated" in checks(rep)["restore"]["error"]


def test_pg_dump_stale_but_restorable(tmp, dumps):
    stale = os.path.join(tmp, "stale.dump")
    shutil.copy(dumps["custom"], stale)
    os.utime(stale, (time.time() - 72 * 3600,) * 2)
    rc, rep, _ = run_tool(tmp, {"postgres": [pg_target(stale)]})
    assert rc == 1
    assert status(rep) == {"freshness": "failed", "restore": "ok", "assertions": "ok"}


# --- PostgreSQL: pgBackRest ---

PGBR_CONF = """[global]
repo1-path=/var/lib/pgbackrest
repo1-retention-full=2
start-fast=y
log-level-file=off
lock-path=/tmp/pgbackrest

[main]
pg1-path=/var/lib/postgresql/source
pg1-socket-path=/var/run/postgresql
"""


@pytest.fixture(scope="session")
def pgbackrest_repo(tmp):
    """A source cluster archiving to a pgBackRest repo on the host, one full backup."""
    root = os.path.join(tmp, "pgbr")
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    os.chmod(root, 0o755)
    os.chmod(repo, 0o777)   # written by the container's postgres user
    conf = os.path.join(root, "pgbackrest.conf")
    with open(conf, "w") as fh:
        fh.write(PGBR_CONF)
    os.chmod(conf, 0o644)
    name = f"bv-it-pgbr-{uuid.uuid4().hex[:6]}"
    sh("docker", "run", "-d", "--rm", f"--name={name}", "--entrypoint=sleep",
       f"--mount=type=bind,source={repo},target=/var/lib/pgbackrest",
       f"--mount=type=bind,source={conf},target=/etc/pgbackrest/pgbackrest.conf,readonly",
       PGBR_IMAGE, "infinity")

    def as_pg(*argv, **kw):
        return sh("docker", "exec", "-i", "--user=postgres", name, *argv, **kw)

    try:
        as_pg("initdb", "--pgdata=/var/lib/postgresql/source", "--auth=trust", "--username=postgres",
              "--data-checksums")
        opts = ("-c archive_mode=on -c wal_level=replica "
                "-c archive_command='pgbackrest --stanza=main archive-push %p' "
                "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql")
        as_pg("pg_ctl", "start", "--pgdata=/var/lib/postgresql/source", "--wait",
              "--log=/tmp/source.log", f"--options={opts}")
        as_pg("pgbackrest", "--stanza=main", "stanza-create")
        as_pg("createdb", "app")
        as_pg("psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", "app", input=SEED_SQL.encode())
        as_pg("pgbackrest", "--stanza=main", "--type=full", "backup")
        # Written after the backup: an immediate-target restore must not see it.
        as_pg("psql", "-q", "-d", "app", "-c", "INSERT INTO users VALUES (999999, 'late', now())")
        as_pg("psql", "-q", "-d", "app", "-c", "SELECT pg_switch_wal()")
        yield {"name": name, "repo": repo, "conf": conf, "as_pg": as_pg}
    finally:
        sh("docker", "rm", "-f", name, check=False)


def pgbr_target(r, **kw):
    t = {"name": "app_pgbr", "method": "pgbackrest", "image": PGBR_IMAGE, "stanza": "main",
         "config_file": r["conf"],
         "mounts": [{"source": r["repo"], "target": "/var/lib/pgbackrest"}],
         "database": "app", "assertions": ASSERTIONS,
         "startup_timeout_seconds": 180, "restore_timeout_seconds": 600}
    t.update(kw)
    return t


def test_pgbackrest_restore(tmp, pgbackrest_repo):
    rc, rep, prom = run_tool(tmp, {"postgres": [pgbr_target(pgbackrest_repo)]})
    assert rc == 0, json.dumps(rep, indent=1)
    assert status(rep) == {"freshness": "ok", "restore": "ok", "assertions": "ok"}
    fresh = checks(rep)["freshness"]["details"]
    assert fresh["latest_type"] == "full" and fresh["backups"] == 1
    assert metric(prom, "target_success", target="app_pgbr", kind="postgres") == 1
    # The source repo must be untouched by the verification: the restored
    # server ran with archive_mode=off, so no new WAL or history was pushed.
    listing = pgbackrest_repo["as_pg"]("find", "/var/lib/pgbackrest/archive", "-type", "f").stdout
    assert b".history" not in listing


def test_pgbackrest_missing_wal_fails(tmp, pgbackrest_repo):
    """Remove the archived WAL: the backup alone can't reach consistency."""
    r = pgbackrest_repo
    r["as_pg"]("sh", "-c", "mkdir -p /tmp/saved && cp -a /var/lib/pgbackrest/archive /tmp/saved/ "
                           "&& find /var/lib/pgbackrest/archive -name '0000*' -type f -delete")
    try:
        rc, rep, _ = run_tool(tmp, {"postgres": [pgbr_target(r, restore_timeout_seconds=120)]})
    finally:
        r["as_pg"]("sh", "-c", "rm -rf /var/lib/pgbackrest/archive && "
                               "cp -a /tmp/saved/archive /var/lib/pgbackrest/")
    assert rc == 1
    assert status(rep) == {"freshness": "ok", "restore": "failed", "assertions": "skipped"}, rep


def test_everything_in_one_run(tmp, rest_repo, dumps, pgbackrest_repo):
    repo, _, pw = rest_repo
    cfg = {"restic": [restic_target(repo, pw, read_data_subset="10%",
                                    sample_restore={"files": 5})],
           "postgres": [pg_target(dumps["custom"]), pgbr_target(pgbackrest_repo)]}
    rc, rep, prom = run_tool(tmp, cfg)
    assert rc == 0, json.dumps(rep, indent=1)
    assert rep["status"] == "ok" and len(rep["targets"]) == 3
    assert metric(prom, "run_success") == 1
    for t in ("files", "app", "app_pgbr"):
        kind = "restic" if t == "files" else "postgres"
        assert metric(prom, "target_success", target=t, kind=kind) == 1
