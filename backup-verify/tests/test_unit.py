"""Unit tests: no restic, no PostgreSQL, no container runtime needed."""

import gzip
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backup_verify import cli, config, postgres, report, restic, runner  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(os.path.dirname(HERE), "example-config.json")
SNAP_ID = "a" * 64


def example():
    with open(EXAMPLE) as fh:
        return json.load(fh)


# --- config ---

def test_example_config_is_valid():
    cfg = config.load(EXAMPLE)
    assert [t.name for t in cfg.targets] == ["fileserver", "app_dump", "app_pgbackrest"]
    assert cfg.targets[0].read_data_subset == "2%"
    assert cfg.targets[1].assertions[0].min == 1000


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["restic"][0].update(max_age_hour=1), "unknown key"),
    (lambda c: c["restic"][0].update(read_data_subset="all"), "invalid value"),
    (lambda c: c["restic"][0].update(password_file="relative"), "absolute path"),
    (lambda c: c["restic"][0].update(max_age_hours=float("nan")), "between"),
    (lambda c: c["restic"][0].update(tags=["a,b"]), "','"),
    (lambda c: c["restic"][0]["sample_restore"].update(files=0), "between"),
    (lambda c: c["restic"][0]["sample_restore"].update(files=True), "number"),
    (lambda c: c["postgres"][0].update(name="fileserver"), "duplicate"),
    (lambda c: c["postgres"][0].update(assertions=[]), "at least one"),
    (lambda c: c["postgres"][0]["assertions"][0].update(expect="1"), "either expect"),
    (lambda c: c["postgres"][0].update(database="App; drop"), "invalid value"),
    (lambda c: c["postgres"][0].update(method="basebackup"), "method"),
    (lambda c: c["postgres"][0].update(stanza="main"), "unknown key"),
    (lambda c: c["postgres"][1].pop("stanza"), "missing"),
    (lambda c: c["postgres"][1].update(backup_set="latest"), "invalid value"),
    (lambda c: c["postgres"][1]["mounts"][0].update(source="/a:b"), "':'"),
    (lambda c: c["postgres"][0].update(image="--privileged"), "invalid value"),
    (lambda c: c.update(textfile_path="/tmp/x.txt"), ".prom"),
    (lambda c: c.update(restic=[], postgres=[]), "no restic or postgres"),
])
def test_config_rejects(mutate, message):
    raw = example()
    mutate(raw)
    with pytest.raises(config.ConfigError, match=message):
        config.parse(raw)


def test_config_load_errors(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{nope")
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.load(str(p))
    with pytest.raises(config.ConfigError, match="No such file"):
        config.load(str(tmp_path / "missing.json"))


# --- runner ---

def test_runner_captures_and_caps(tmp_path):
    r = runner.run([sys.executable, "-c", "import sys; sys.stdout.write('x'*100); "
                    "sys.stderr.write('boom'); sys.exit(3)"], 10, max_stdout=10)
    assert r.returncode == 3 and r.stdout == b"x" * 10 and r.stdout_truncated
    assert "exit 3: boom" in r.describe()


def test_runner_timeout_kills_group():
    t0 = time.monotonic()
    r = runner.run(["sh", "-c", "sleep 30 & sleep 30"], 0.5)
    assert r.timed_out and r.returncode is None
    assert time.monotonic() - t0 < 10
    assert "timed out" in r.describe()


def test_runner_stdin_and_env():
    r = runner.run([sys.executable, "-c", "import os,sys; sys.stdout.write("
                    "sys.stdin.read() + os.environ.get('SECRET', '-') + os.environ.get('X', '-'))"],
                   10, stdin=b"in:", env=runner.build_env(["X"], environ={"X": "1", "SECRET": "s"}))
    assert r.stdout == b"in:-1"


def test_runner_missing_binary():
    r = runner.run(["/nonexistent/restic"], 5)
    assert r.returncode is None and r.start_error and not r.ok


# --- restic ---

@pytest.mark.parametrize("text, expected", [
    ("2026-10-23T21:49:04.293099505Z", 1792792144.293099505),
    ("2026-10-23T23:49:04+02:00", 1792792144.0),
    ("2026-10-23T21:49:04.5-00:30", 1792793944.5),
])
def test_parse_time(text, expected):
    assert restic.parse_time(text) == pytest.approx(expected, abs=1e-6)


def test_parse_time_ns_exact():
    assert restic.parse_time_ns("1970-01-01T00:00:01.000000001Z") == 1_000_000_001


@pytest.mark.parametrize("bad", ["", "2026-10-23", "2026-10-23T21:49:04", None, 5,
                                 "2026-10-23T21:49:04.1234567890Z"])
def test_parse_time_rejects(bad):
    with pytest.raises(ValueError):
        restic.parse_time(bad)


def test_escape_pattern():
    assert restic.escape_pattern("/a/we[ir]d/f*?.txt") == r"/a/we\[ir\]d/f\*\?.txt"
    assert restic.escape_pattern("/back\\slash") == "/back\\\\slash"


def test_pick_files_reservoir():
    import random
    lines = [json.dumps({"message_type": "snapshot", "id": SNAP_ID})]
    lines += [json.dumps({"type": "dir", "path": "/d", "message_type": "node"})]
    lines += [json.dumps({"type": "file", "path": f"/d/{i}", "size": i, "message_type": "node"})
              for i in range(100)]
    lines += [json.dumps({"type": "symlink", "path": "/d/l", "message_type": "node"}), "garbage"]
    chosen, seen = restic._pick_files(iter(lines), 5, 49, random.Random(1))
    assert seen == 50 and len(chosen) == 5
    assert all(c["size"] <= 49 for c in chosen)
    assert len({c["path"] for c in chosen}) == 5
    # Old restic: struct_type only.
    old = [json.dumps({"type": "file", "path": "/x", "size": 1, "struct_type": "node"})]
    assert restic._pick_files(iter(old), 5, 10, random.Random(1))[0][0]["path"] == "/x"


class FakeRestic:
    """Stands in for runner.run: answers restic subcommands from a table."""

    def __init__(self, snapshots, files=None, check_rc=0, restore_rc=0, corrupt=()):
        self.snapshots = snapshots
        self.files = files or {}
        self.check_rc, self.restore_rc = check_rc, restore_rc
        self.corrupt = set(corrupt)
        self.calls = []

    def __call__(self, argv, timeout, env=None, work_dir=None, stdout_sink=None, **kw):
        self.calls.append(argv)
        args = [a for a in argv[1:] if not a.startswith(("--repo=", "--password-file=",
                                                          "--quiet", "--retry-lock="))]
        sub = args[0]

        def res(rc=0, out=b"", err=""):
            return runner.Result(argv, rc, out, False, err, 0.01)

        if sub == "snapshots":
            return res(out=json.dumps(self.snapshots).encode())
        if sub == "check":
            return res(self.check_rc, err="" if not self.check_rc else "Fatal: repository contains errors")
        if sub == "ls":
            import io
            lines = [json.dumps({"message_type": "snapshot"})] + [
                json.dumps({"type": "file", "path": p, "size": len(d), "message_type": "node",
                            "mtime": "2026-10-23T00:00:00Z"}) for p, d in self.files.items()]
            stdout_sink(io.BytesIO("\n".join(lines).encode()))
            return res()
        if sub == "restore":
            if self.restore_rc:
                return res(self.restore_rc, err="Fatal: There were 1 errors")
            target = next(a.split("=", 1)[1] for a in args if a.startswith("--target="))
            for inc in (a.split("=", 1)[1] for a in args if a.startswith("--include=")):
                path = inc.replace("\\", "")
                dest = os.path.join(target, path.lstrip("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                data = self.files[path]
                with open(dest, "wb") as fh:
                    fh.write(data[:-1] if path in self.corrupt else data)
            return res()
        raise AssertionError(argv)


def restic_target(**kw):
    raw = {"name": "t", "repository": "/repo", "password_file": "/pw",
           "sample_restore": {"files": 3, "seed": 7}}
    raw.update(kw)
    return config._restic(raw, "restic[0]")


NOW = 1792800000.0


def snap(hours_ago, i="a"):
    t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - hours_ago * 3600))
    return {"id": i * 64, "time": t + ".5Z", "hostname": "h"}


def statuses(result):
    return {c.name: c.status for c in result.checks}


def test_restic_all_ok(tmp_path):
    files = {"/data/a": b"aaa", "/data/b": b"bb", "/data/c[1]": b"c"}
    fake = FakeRestic([snap(30, "b"), snap(2, "a")], files)
    res = restic.verify(restic_target(), str(tmp_path), run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "check": "ok", "sample_restore": "ok"}
    assert res.ok
    assert res.latest_backup_timestamp == pytest.approx(NOW - 7200 + 0.5)
    sample = res.checks[2].details
    assert sample["snapshot"] == "a" * 64 and sample["mismatches"] == 0
    assert {f["path"] for f in sample["files"]} == set(files)
    restore = next(c for c in fake.calls if "restore" in c)
    assert "--verify" in restore and r"--include=/data/c\[1\]" in restore
    assert os.listdir(tmp_path) == []  # restore dir cleaned up


def test_restic_stale_still_samples():
    fake = FakeRestic([snap(30)], {"/a": b"x"})
    res = restic.verify(restic_target(), run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "failed", "check": "ok", "sample_restore": "ok"}
    assert "30.0h old" in res.checks[0].error and not res.ok


def test_restic_no_snapshots_skips_sample():
    res = restic.verify(restic_target(), run=FakeRestic([]), now=lambda: NOW)
    assert statuses(res) == {"freshness": "failed", "check": "ok", "sample_restore": "skipped"}


def test_restic_filters_and_subset_in_argv():
    fake = FakeRestic([snap(1)], {"/a": b"x"})
    t = restic_target(host="h", tags=["x", "y"], paths=["/srv"], read_data_subset="1/10")
    restic.verify(t, run=fake, now=lambda: NOW)
    snaps = next(c for c in fake.calls if "snapshots" in c)
    assert ["--host=h", "--tag=x,y", "--path=/srv"] == snaps[-3:]
    assert any("--read-data-subset=1/10" in c for c in fake.calls)
    assert all(c[1] == "--repo=/repo" for c in fake.calls)


def test_restic_check_failure_and_restore_failure():
    fake = FakeRestic([snap(1)], {"/a": b"x"}, check_rc=1, restore_rc=1)
    res = restic.verify(restic_target(), run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "check": "failed", "sample_restore": "failed"}
    assert "repository contains errors" in res.checks[1].error


def test_restic_size_mismatch_detected():
    fake = FakeRestic([snap(1)], {"/a": b"xyz"}, corrupt={"/a"})
    res = restic.verify(restic_target(), run=fake, now=lambda: NOW)
    assert res.checks[2].status == "failed"
    assert res.checks[2].details["files"][0]["status"] == "size_mismatch"
    assert ("sample_mismatches", {}, 1) in res.gauges


def test_restic_compare_to_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    same, changed, edited = src / "same", src / "changed", src / "edited"
    for p, d in ((same, b"one"), (changed, b"two"), (edited, b"333")):
        p.write_bytes(d)
    mtime = "2026-10-23T00:00:00Z"
    ns = restic.parse_time_ns(mtime)
    for p in (same, edited):
        os.utime(p, ns=(ns, ns))
    files = {str(same): b"one", str(changed): b"two", str(edited): b"334"}
    fake = FakeRestic([snap(1)], files)
    t = restic_target(sample_restore={"files": 3, "compare_to_source": True})
    res = restic.verify(t, str(tmp_path), run=fake, now=lambda: NOW)
    by_path = {f["path"]: f["status"] for f in res.checks[2].details["files"]}
    # "changed" has the wrong mtime, so it is not compared; "edited" kept its
    # mtime and size but its bytes differ from the backup: a real mismatch.
    assert by_path == {str(same): "source_match", str(changed): "source_changed",
                       str(edited): "source_mismatch"}
    assert res.checks[2].status == "failed"


def test_restic_bad_json():
    class Bad(FakeRestic):
        def __call__(self, argv, *a, **kw):
            return runner.Result(argv, 0, b"not json", False, "", 0.0)
    res = restic.verify(restic_target(), run=Bad([]), now=lambda: NOW)
    assert "cannot parse" in res.checks[0].error


# --- report ---

def test_metrics_render_and_escape():
    rep = report.Report(started=100.0, finished=112.5)
    t = report.Target('we"ird\\name\n', "restic", latest_backup_timestamp=50.0)
    t.checks = [report.Check("freshness", report.OK, 1.0),
                report.Check("sample_restore", report.SKIPPED)]
    t.gauges = [("sample_files", {}, 3)]
    rep.targets.append(t)
    text = report.render_metrics(rep)
    assert 'backup_verify_run_success 0' in text
    assert 'target="we\\"ird\\\\name\\n"' in text
    assert 'check="sample_restore"} 0' in text
    assert 'backup_verify_check_skipped{target="we\\"ird\\\\name\\n",kind="restic",check="sample_restore"} 1' in text
    assert "backup_verify_run_duration_seconds 12.5" in text
    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith("#") or line.startswith("backup_verify_")


def test_report_ok_requires_all_checks():
    t = report.Target("x", "postgres", checks=[report.Check("a", report.OK)])
    rep = report.Report(0, 1, [t])
    assert rep.ok
    t.checks.append(report.Check("b", report.SKIPPED))
    assert not rep.ok
    assert not report.Report(0, 1, []).ok


def test_atomic_write(tmp_path):
    p = tmp_path / "m.prom"
    report.atomic_write(str(p), "a 1\n")
    report.atomic_write(str(p), "a 2\n")
    assert p.read_text() == "a 2\n"
    assert os.listdir(tmp_path) == ["m.prom"]


# --- postgres helpers ---

def test_parse_rows():
    assert postgres.parse_rows("") == []
    assert postgres.parse_rows("t\x00") == [["t"]]
    assert postgres.parse_rows("\x00") == [[""]]
    assert postgres.parse_rows("1\x1fa\nb\x002\x1fc\x00") == [["1", "a\nb"], ["2", "c"]]


A = config.Assertion


@pytest.mark.parametrize("assertion, rows, passed", [
    (A("a", "q"), [["t"]], True),
    (A("a", "q"), [["f"]], False),
    (A("a", "q"), [], False),
    (A("a", "q"), [["t"], ["t"]], False),
    (A("a", "q"), [["t", "t"]], False),
    (A("a", "q", expect="42"), [["42"]], True),
    (A("a", "q", expect=""), [[""]], True),
    (A("a", "q", min=10), [["10"]], True),
    (A("a", "q", min=10), [["9.99"]], False),
    (A("a", "q", max=10), [["11"]], False),
    (A("a", "q", min=1, max=2), [["1.5"]], True),
    (A("a", "q", min=1), [["NaN"]], False),
    (A("a", "q", min=1), [["lots"]], False),
])
def test_evaluate(assertion, rows, passed):
    assert postgres.evaluate(assertion, rows)[0] is passed


def test_dump_format_and_select(tmp_path):
    custom = tmp_path / "a-1.dump"
    custom.write_bytes(b"PGDMP\x01\x0e\x00" + b"\x00" * 600)
    plain = tmp_path / "a-2.sql"
    plain.write_bytes(b"-- dump\n" + postgres.PLAIN_TRAILER + b"\n")
    gz = tmp_path / "a-3.sql.gz"
    gz.write_bytes(gzip.compress(b"select 1;"))
    tar = tmp_path / "a-4.tar"
    tar.write_bytes(b"x" * 257 + b"ustar" + b"\x00" * 300)
    d = tmp_path / "dir"
    d.mkdir()
    (d / "toc.dat").write_bytes(b"PGDMP")
    assert [postgres.dump_format(str(p)) for p in (custom, plain, gz, tar, d)] == \
        ["custom", "plain", "plain_gzip", "tar", "directory"]
    assert postgres.dump_format(str(tmp_path)) is None
    for i, p in enumerate((custom, plain, gz, tar)):
        os.utime(p, (1000 + i, 1000 + i))
    os.utime(plain, (5000, 5000))
    path, mtime = postgres.select_dump(str(tmp_path / "a-*"))
    assert path == str(plain) and mtime == 5000
    assert postgres.select_dump(str(tmp_path / "zzz-*")) == (None, None)
    assert postgres._tail_has_trailer(str(plain))
    assert not postgres._tail_has_trailer(str(custom))


def info(backups, stanza="main"):
    return json.dumps([{"name": stanza, "backup": backups,
                        "status": {"code": 0, "message": "ok"}}]).encode()


def test_latest_backup():
    raw = info([{"label": "20261020-010000F", "type": "full", "timestamp": {"start": 1, "stop": 10}},
                {"label": "20261021-010000F_20261021-020000I", "type": "incr",
                 "timestamp": {"start": 20, "stop": 30}}])
    label, ts, details = postgres._latest_backup(raw, "main")
    assert label == "20261021-010000F_20261021-020000I" and ts == 30
    assert details["backups"] == 2 and details["latest_type"] == "incr"
    with pytest.raises(postgres.Failure, match="no backups"):
        postgres._latest_backup(info([]), "main")
    with pytest.raises(postgres.Failure, match="no stanza"):
        postgres._latest_backup(info([], "other"), "main")
    with pytest.raises(postgres.Failure, match="flagged"):
        postgres._latest_backup(info([{"label": "x", "error": True,
                                       "timestamp": {"stop": 5}}]), "main")


class FakeDocker:
    """Stands in for runner.run for the docker CLI."""

    def __init__(self, fail_on=None, values=None):
        self.calls = []
        self.fail_on = fail_on
        self.values = values or {}

    def __call__(self, argv, timeout, env=None, work_dir=None, stdin=None, **kw):
        self.calls.append(argv)
        joined = " ".join(argv)

        def res(rc=0, out=b""):
            return runner.Result(argv, rc, out, False, "" if rc == 0 else "error: nope", 0.0)

        if self.fail_on and self.fail_on in joined:
            return res(1)
        if argv[1] == "inspect":
            return res(out=b"true\n")
        if argv[1] == "exec" and "psql" in argv and any(a.startswith("--command=SELECT") for a in argv):
            sql = next(a for a in argv if a.startswith("--command=")).split("=", 1)[1]
            return res(out=self.values.get(sql, "t").encode() + b"\x00")
        if argv[1] == "exec" and "info" in argv:
            return res(out=info([{"label": "20261021-010000F", "type": "full",
                                  "timestamp": {"start": NOW - 3600, "stop": NOW - 1800}}]))
        return res()


def pg_target(**kw):
    raw = example()["postgres"][0]
    raw.update(kw)
    return config._postgres(raw, "postgres[0]")


def pg_cfg():
    return config.Config(targets=())


def test_pg_dump_flow(tmp_path):
    dump = tmp_path / "app-1.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 600)
    os.utime(dump, (NOW - 3600, NOW - 3600))
    fake = FakeDocker(values={
        "SELECT count(*) FROM users": "5000",
        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1": "20261020"})
    t = pg_target(dump=str(tmp_path / "app-*.dump"))
    res = postgres.verify(pg_cfg(), t, run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "restore": "ok", "assertions": "ok"}, res.to_json()
    assert res.assertions == [("users_present", True), ("orders_recent", True),
                              ("schema_version", True)]
    start = fake.calls[0]
    assert start[1] == "run" and "--network=none" in start and "--rm" in start
    assert f"--mount=type=bind,source={dump},target=/backup/dump,readonly" in start
    assert not any("-p" == a or a.startswith("--publish") for a in start)
    # The generated password is passed by name only.
    assert start[start.index("-e") + 1] == "POSTGRES_PASSWORD"
    restore = next(c for c in fake.calls if "pg_restore" in c)
    assert "--jobs=4" in restore and "--exit-on-error" in restore
    assert fake.calls[-1][1:3] == ["rm", "--force"]


def test_pg_dump_assertion_values(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 600)
    os.utime(dump, (NOW, NOW))
    fake = FakeDocker(values={"SELECT count(*) FROM users": "999"})
    res = postgres.verify(pg_cfg(), pg_target(dump=str(dump)), run=fake, now=lambda: NOW)
    assert statuses(res)["assertions"] == "failed"
    assert dict(res.assertions) == {"users_present": False, "orders_recent": True,
                                    "schema_version": False}


def test_pg_dump_restore_failure_still_removes_container(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 600)
    os.utime(dump, (NOW, NOW))
    fake = FakeDocker(fail_on="pg_restore")
    res = postgres.verify(pg_cfg(), pg_target(dump=str(dump)), run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "restore": "failed", "assertions": "skipped"}
    assert fake.calls[-1][1:3] == ["rm", "--force"]


def test_pg_dump_missing():
    res = postgres.verify(pg_cfg(), pg_target(dump="/nonexistent/x-*.dump"),
                          run=FakeDocker(), now=lambda: NOW)
    assert statuses(res) == {"freshness": "failed", "restore": "skipped", "assertions": "skipped"}


def test_plain_dump_without_trailer_fails(tmp_path):
    dump = tmp_path / "x.sql"
    dump.write_bytes(b"CREATE TABLE users (id int);\n")
    os.utime(dump, (NOW, NOW))
    res = postgres.verify(pg_cfg(), pg_target(dump=str(dump)), run=FakeDocker(), now=lambda: NOW)
    assert "truncated" in res.checks[1].error


def test_pgbackrest_flow(tmp_path):
    raw = example()["postgres"][1]
    t = config._postgres(raw, "postgres[1]")
    fake = FakeDocker(values={"SELECT pg_is_in_recovery()": "f",
                              "SELECT count(*) FROM users": "1000"})
    res = postgres.verify(pg_cfg(), t, run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "restore": "ok", "assertions": "ok"}, res.to_json()
    assert res.latest_backup_timestamp == NOW - 1800
    restore = next(c for c in fake.calls if c[-1] == "restore")
    for flag in ("--archive-mode=off", "--type=immediate", "--target-action=promote"):
        assert flag in restore
    start = fake.calls[0]
    assert "--mount=type=bind,source=/srv/pgbackrest,target=/var/lib/pgbackrest,readonly" in start
    assert "--entrypoint=sleep" in start


def test_pgbackrest_restore_failure(tmp_path):
    t = config._postgres(example()["postgres"][1], "postgres[1]")
    fake = FakeDocker(fail_on="restore")
    res = postgres.verify(pg_cfg(), t, run=fake, now=lambda: NOW)
    assert statuses(res) == {"freshness": "ok", "restore": "failed", "assertions": "skipped"}
    assert fake.calls[-1][1:3] == ["rm", "--force"]


# --- cli ---

def test_cli_config_error(tmp_path, capsys):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"restic": []}))
    assert cli.main(["run", "--config", str(p)]) == 2
    assert "config error" in capsys.readouterr().err


def test_cli_check_config(capsys):
    assert cli.main(["check-config", "--config", EXAMPLE]) == 0


def test_cli_unknown_target(capsys):
    assert cli.main(["run", "--config", EXAMPLE, "--target", "nope"]) == 2


def test_describe_prefers_error_line():
    r = runner.Result(["docker"], 1, b"", False,
                      'ERROR:  relation "nope" does not exist\nLINE 1: SELECT 1 FROM nope\n'
                      '                     ^\n', 0.1)
    assert r.describe() == 'docker: exit 1: ERROR:  relation "nope" does not exist'


def test_describe_unwraps_restic_json_error():
    r = runner.Result(["/usr/bin/restic"], 12, b"", False,
                      '{"message_type":"exit_error","code":12,"message":"Fatal: wrong password"}\n', 0.1)
    assert r.describe() == "restic: exit 12: Fatal: wrong password"


def test_container_removed_when_run_times_out(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 600)
    os.utime(dump, (NOW, NOW))
    fake = FakeDocker(fail_on=" run ")
    res = postgres.verify(pg_cfg(), pg_target(dump=str(dump)), run=fake, now=lambda: NOW)
    assert "could not start container" in res.checks[1].error
    assert fake.calls[-1][1:3] == ["rm", "--force"]


def test_container_removed_on_unexpected_exception(tmp_path, monkeypatch):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 600)
    os.utime(dump, (NOW, NOW))
    fake = FakeDocker()

    def boom(*a, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(postgres, "_restore_dump", boom)
    res = postgres.verify(pg_cfg(), pg_target(dump=str(dump)), run=fake, now=lambda: NOW)
    assert "internal error: RuntimeError: kaboom" in res.checks[1].error
    assert res.checks[2].status == "skipped"
    assert fake.calls[-1][1:3] == ["rm", "--force"]


def test_gzip_feeder_unblocks_when_reader_never_reads(tmp_path):
    p = tmp_path / "big.sql.gz"
    p.write_bytes(gzip.compress(os.urandom(4 << 20)))   # far more than a pipe buffer
    feeder = postgres._GzipFeeder(str(p))
    t0 = time.monotonic()
    with feeder:
        pass
    assert not feeder.thread.is_alive() and time.monotonic() - t0 < 10
    assert feeder.error == "psql stopped reading"


def test_gzip_feeder_reports_truncation(tmp_path):
    data = gzip.compress(b"select 1;\n" * 10000)
    p = tmp_path / "cut.sql.gz"
    p.write_bytes(data[: len(data) // 2])
    feeder = postgres._GzipFeeder(str(p))
    with feeder as r:
        r.read()
    assert feeder.error and "EOFError" in feeder.error
