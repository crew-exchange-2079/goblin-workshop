import errno
import io
import json
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest import mock

from lto_exporter.commands import (ChildTracker, RunResult, UnsafeCommand,
                                   _check, _run_bounded, run_command,
                                   sg_logs_argv)
from lto_exporter.config import (ConfigError, Device, check_device,
                                 check_sg_logs_executable, parse_config,
                                 resolve_device_path, validate_device_path)
from lto_exporter.exporter import Collector, _Server6, main, make_server
from lto_exporter.parsers import (SG_EXIT_STATUS, parse_sg_logs_error_counters,
                                  sg_exit_status)
from offline.tapeinfo_text import parse_tapeinfo_text

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fx(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def identity(path):
    return path


def device_ok(dev):
    return None


def cfg(devices=None, **kw):
    raw = {"port": 0, "cache_seconds": 0,
           "devices": devices if devices is not None else
           [{"name": "drive0", "path": "/dev/sg3"}]}
    raw.update(kw)
    return parse_config(raw, resolver=identity)


def collector(config, runner, **kw):
    kw.setdefault("device_check", device_ok)
    return Collector(config, runner=runner, **kw)


def bounded(argv, timeout, max_bytes, popen=subprocess.Popen, tracker=None, kill_wait=2.0):
    """The generic runner, used with a harmless local child (never a tape tool)."""
    return _run_bounded(argv, timeout, max_bytes, popen, time.monotonic,
                        tracker or ChildTracker(), "test", kill_wait=kill_wait)


SG = ["/usr/bin/sg_logs", "--readonly", "--page=0x2", "/dev/sg3"]


class FakeRunner:
    """Maps argv tuples to RunResults and records every call."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, argv, timeout, max_bytes):
        self.calls.append(list(argv))
        for key, res in self.table.items():
            if key in argv:
                return res
        return RunResult("not_found", None, "", 0.0)


class StuckPopen:
    """A child that never exits and can't be reaped: models D state.

    stdout is a real pipe that never gets data, so the runner's select()
    times out exactly as with a hung sg_logs. kill() is recorded but has no
    effect, and wait() always times out.
    """

    instances = []

    def __init__(self, argv, **kw):
        r, self._w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self.returncode = None
        self.killed = 0
        self.alive = True
        StuckPopen.instances.append(self)

    def kill(self):
        self.killed += 1

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("sg_logs", timeout)

    def poll(self):
        if not self.alive:
            self.returncode = -9
        return self.returncode

    def release(self):
        self.alive = False
        os.close(self._w)


class CommandTests(unittest.TestCase):
    def test_sg_logs_argv_is_fixed_and_readonly(self):
        self.assertEqual(sg_logs_argv("/usr/bin/sg_logs", "write", "/dev/sg3"), SG)
        self.assertEqual(sg_logs_argv("/usr/bin/sg_logs", "read", "/dev/sg3")[2], "--page=0x3")
        with self.assertRaises(KeyError):
            sg_logs_argv("/usr/bin/sg_logs", "0x2e", "/dev/sg3")

    def test_no_tapeinfo_builder_and_non_sg_logs_exe_refused(self):
        import lto_exporter.commands as cmds
        self.assertFalse(hasattr(cmds, "tapeinfo_argv"))
        for exe in ("/usr/sbin/tapeinfo", "/usr/bin/mt", "/bin/sh", "sg_logs",
                    "/usr/bin/sg_logs\n", "/usr/bin/sg_logs ", "/usr/bin/sg_logsx"):
            with self.assertRaises(UnsafeCommand, msg=repr(exe)):
                sg_logs_argv(exe, "write", "/dev/sg3")

    def test_forbidden_tokens_rejected(self):
        for tok in ("--reset", "-R", "--select", "-S"):
            with self.assertRaises(UnsafeCommand):
                _check(["/usr/bin/sg_logs", tok, "/dev/sg3"])

    def test_forbidden_tokens_rejected_in_every_position(self):
        # Regression: argv[1:-1] used to skip the last argument.
        for tok in ("--reset", "-R", "--select", "-S", "--sp", "-s",
                    "--control=0", "--in=/x", "--page=0x2e", "--page=0x0",
                    "--all", "-A", "/dev/st0", "/dev/nst0", "/dev/sg3\n",
                    "/dev/sda", "-X"):
            for pos in (1, 2, 3):
                argv = list(SG)
                argv[pos] = tok
                with self.assertRaises(UnsafeCommand, msg=(pos, tok)):
                    _check(argv)
            with self.assertRaises(UnsafeCommand, msg=("append", tok)):
                _check(SG + [tok])

    def test_check_requires_exact_shape(self):
        for argv in ([], SG[:3], SG + ["/dev/sg4"], tuple(SG[:2]),
                     [SG[0], "--page=0x2", "--readonly", SG[3]],
                     [SG[0], "--readonly", "--page=0x3", "--page=0x2"],
                     [SG[0], "-X", "--page=0x2", SG[3]],
                     [SG[0], "--readonly", "-p", "0x2"],
                     [SG[0], "--readonly", "--page=0x2", b"/dev/sg3"],
                     "sg_logs --readonly --page=0x2 /dev/sg3", None):
            with self.assertRaises(UnsafeCommand, msg=repr(argv)):
                _check(argv)
        self.assertEqual(_check(tuple(SG)), SG)

    def test_run_command_permission_error_is_exec_error(self):
        def popen(argv, **kw):
            raise PermissionError(13, "Permission denied")
        res = run_command(SG, 1, 1024, popen=popen, tracker=ChildTracker())
        self.assertEqual(res.status, "exec_error")

    def test_run_command_cannot_bypass_check(self):
        spawned = []

        def popen(argv, **kw):
            spawned.append(argv)
            raise FileNotFoundError

        for argv in ([sys.executable, "-c", "print(1)"],
                     ["/usr/bin/sg_logs", "--readonly", "--reset", "/dev/sg3"],
                     ["/usr/bin/sg_logs", "--readonly", "--page=0x2", "/dev/sg3", "--select"],
                     ["/usr/bin/tapeinfo", "-f", "/dev/sg3"]):
            with self.assertRaises(UnsafeCommand):
                run_command(argv, 1, 1024, popen=popen, tracker=ChildTracker())
        self.assertEqual(spawned, [])

    def test_collector_only_issues_allowlisted_commands(self):
        runner = FakeRunner({})
        collector(cfg(), runner).collect()
        allowed = {("--readonly", "--page=0x2"), ("--readonly", "--page=0x3")}
        self.assertEqual(len(runner.calls), 2)
        for argv in runner.calls:
            self.assertIn(tuple(argv[1:-1]), allowed)
            self.assertEqual(argv[-1], "/dev/sg3")
            _check(argv)

    def test_runner_no_shell_and_timeout(self):
        script = "import time; print('hi', flush=True); time.sleep(30)"
        res = bounded([sys.executable, "-c", script], timeout=0.5, max_bytes=4096)
        self.assertEqual(res.status, "timeout")
        self.assertIsNone(res.returncode)
        self.assertTrue(res.reaped)
        self.assertLess(res.duration, 5)

    def test_runner_caps_output(self):
        script = "import sys; sys.stdout.write('x' * 200000)"
        res = bounded([sys.executable, "-c", script], timeout=5, max_bytes=1024)
        self.assertEqual(res.status, "output_too_large")
        self.assertLessEqual(len(res.stdout), 1024)

    def test_runner_ok_and_shell_metachars_inert(self):
        res = bounded([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(id); ls"],
                      timeout=5, max_bytes=4096)
        self.assertEqual((res.status, res.returncode), ("ok", 0))
        self.assertEqual(res.stdout.strip(), "$(id); ls")

    def test_runner_read_oserror_is_read_error_and_reaped(self):
        script = "import time; print('hi', flush=True); time.sleep(30)"
        # commands.os is the global os module: patching os.read before the
        # spawn also breaks Popen's own error-pipe read on Python < 3.13
        # (exec_error). Arm the failure only after the child has started.
        patcher = mock.patch("lto_exporter.commands.os.read", side_effect=OSError("EIO"))

        def popen(*a, **kw):
            proc = subprocess.Popen(*a, **kw)
            patcher.start()
            return proc

        try:
            res = bounded([sys.executable, "-c", script], timeout=5, max_bytes=4096,
                          popen=popen)
        finally:
            patcher.stop()
        self.assertEqual(res.status, "read_error")
        self.assertTrue(res.reaped)

    def test_runner_eof_but_child_lingers_is_timeout(self):
        # stdout closed early, process keeps running past the deadline.
        script = "import os, time; os.close(1); time.sleep(30)"
        res = bounded([sys.executable, "-c", script], timeout=0.5, max_bytes=4096)
        self.assertEqual(res.status, "timeout")
        self.assertTrue(res.reaped)

    def test_runner_missing_binary(self):
        res = run_command(["/nonexistent/sg_logs", "--readonly", "--page=0x2", "/dev/sg3"],
                          timeout=1, max_bytes=1024, tracker=ChildTracker())
        self.assertEqual(res.status, "not_found")

    def test_real_runner_path_spawns_only_sg_logs(self):
        spawned = []

        class P:
            def __init__(self, argv, **kw):
                spawned.append(list(argv))
                raise FileNotFoundError

        c = cfg(devices=[{"name": f"d{i}", "path": f"/dev/sg{i}"} for i in range(3)])
        tracker = ChildTracker()
        runner = lambda a, t, m: run_command(a, t, m, popen=P, tracker=tracker)
        out = collector(c, runner).collect()
        self.assertEqual(len(spawned), 6)
        for argv in spawned:
            self.assertEqual(argv[0], "/usr/bin/sg_logs")
            self.assertEqual(argv[1], "--readonly")
            self.assertIn(argv[2], ("--page=0x2", "--page=0x3"))
            self.assertNotIn("tapeinfo", " ".join(argv))
        self.assertNotIn("tapeinfo", out)
        self.assertNotIn("tapealert", out.lower())

    def test_exporter_never_imports_offline_parser(self):
        code = ("import sys, lto_exporter.exporter; "
                "print(any(m.startswith('offline') for m in sys.modules))")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)

    def test_runner_kwargs(self):
        seen = {}

        class P:
            def __init__(self, argv, **kw):
                seen.update(kw)
                raise FileNotFoundError

        run_command(SG, 1, 1024, popen=P, tracker=ChildTracker())
        self.assertIs(seen["shell"], False)
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(seen["stderr"], subprocess.DEVNULL)
        self.assertEqual(seen["env"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})


class UnreapedChildTests(unittest.TestCase):
    """Regression for finding 1: the post-kill wait used to raise
    TimeoutExpired out of the runner (HTTP 500 on every scrape) and a fresh
    child was spawned on every scrape."""

    def setUp(self):
        StuckPopen.instances = []

    def tearDown(self):
        for p in StuckPopen.instances:
            if p.alive:
                p.release()

    def test_unkillable_child_does_not_raise_and_pipe_is_closed(self):
        tracker = ChildTracker()
        res = run_command(SG, 0.2, 1024, popen=StuckPopen, tracker=tracker,
                          clock=time.monotonic)
        self.assertEqual(res.status, "timeout")
        self.assertFalse(res.reaped)
        self.assertIsNone(res.returncode)
        p = StuckPopen.instances[0]
        self.assertGreaterEqual(p.killed, 1)
        self.assertTrue(p.stdout.closed)
        self.assertEqual(tracker.count(), 1)

    def test_no_respawn_for_same_device_until_reaped(self):
        tracker = ChildTracker()
        run_command(SG, 0.2, 1024, popen=StuckPopen, tracker=tracker)
        for _ in range(5):
            res = run_command(SG, 0.2, 1024, popen=StuckPopen, tracker=tracker)
            self.assertEqual(res.status, "blocked_unreaped")
        self.assertEqual(len(StuckPopen.instances), 1)
        # A different device is still allowed (below the global cap).
        other = SG[:3] + ["/dev/sg4"]
        self.assertEqual(run_command(other, 0.2, 1024, popen=StuckPopen,
                                     tracker=tracker).status, "timeout")
        self.assertEqual(len(StuckPopen.instances), 2)
        # Once the kernel lets the first one go, spawning resumes.
        StuckPopen.instances[0].release()
        self.assertEqual(tracker.count(), 1)
        run_command(SG, 0.2, 1024, popen=StuckPopen, tracker=tracker)
        self.assertEqual(len(StuckPopen.instances), 3)

    def test_global_cap(self):
        tracker = ChildTracker(limit=2)
        for n in (1, 2):
            run_command(SG[:3] + [f"/dev/sg{n}"], 0.1, 1024, popen=StuckPopen, tracker=tracker)
        res = run_command(SG[:3] + ["/dev/sg9"], 0.1, 1024, popen=StuckPopen, tracker=tracker)
        self.assertEqual(res.status, "blocked_unreaped")
        self.assertEqual(len(StuckPopen.instances), 2)

    def test_http_stays_200_and_reports_truthfully(self):
        tracker = ChildTracker()
        runner = lambda a, t, m: run_command(a, t, m, popen=StuckPopen, tracker=tracker)
        c = cfg(command_timeout_seconds=0.2)
        col = collector(c, runner, tracker=tracker)
        srv = make_server(c, col)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/metrics"
            for i in range(3):
                with urllib.request.urlopen(url, timeout=15) as r:
                    self.assertEqual(r.status, 200)
                    body = r.read().decode()
            first_scrape_children = 1  # write page hung; read page blocked
            self.assertEqual(len(StuckPopen.instances), first_scrape_children)
            self.assertIn("tape_exporter_unreaped_children 1", body)
            self.assertIn('collector="sg_logs_write_errors",status="blocked_unreaped"} 1', body)
            self.assertIn('collector="sg_logs_read_errors",status="blocked_unreaped"} 1', body)
            self.assertNotIn("tape_log_errors_total", body)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_first_scrape_reports_timeout(self):
        tracker = ChildTracker()
        runner = lambda a, t, m: run_command(a, t, m, popen=StuckPopen, tracker=tracker)
        out = collector(cfg(command_timeout_seconds=0.2), runner, tracker=tracker).collect()
        self.assertIn('collector="sg_logs_write_errors",status="timeout"} 1', out)
        self.assertIn('collector="sg_logs_read_errors",status="blocked_unreaped"} 1', out)

    def test_runner_exception_becomes_status_not_500(self):
        def boom(argv, timeout, max_bytes):
            raise RuntimeError("secret")
        out = collector(cfg(), boom).collect()
        self.assertIn('status="internal_error"} 1', out)
        self.assertNotIn("secret", out)


class ConfigTests(unittest.TestCase):
    def test_defaults_loopback(self):
        c = parse_config({}, resolver=identity)
        self.assertEqual(c.listen_address, "127.0.0.1")
        self.assertEqual(c.devices, ())

    def test_rejects_rewinding_and_non_sg_nodes(self):
        for p in ("/dev/st0", "/dev/nst0", "/dev/sda", "sg3", "/dev/../etc/passwd", "/tmp/sg0"):
            with self.assertRaises(ConfigError, msg=p):
                validate_device_path(p, resolver=identity)

    def test_rejects_trailing_newline_and_whitespace(self):
        # Regression: re "$" matches before a trailing newline.
        for p in ("/dev/sg3\n", "/dev/sg3 ", "/dev/sg3\t", "/dev/sg 3", "/dev/sg3\x00"):
            with self.assertRaises(ConfigError, msg=repr(p)):
                validate_device_path(p, resolver=identity)
        with self.assertRaises(ConfigError):
            validate_device_path("/dev/tape/x", resolver=lambda p: "/dev/sg3\n")
        for name in ("drive0\n", "drive0 ", "\ndrive0"):
            with self.assertRaises(ConfigError, msg=repr(name)):
                parse_config({"devices": [{"name": name, "path": "/dev/sg3"}]}, resolver=identity)

    def test_symlink_must_resolve_to_sg(self):
        validate_device_path("/dev/tape/by-id/synthetic", resolver=lambda p: "/dev/sg7")
        with self.assertRaises(ConfigError):
            validate_device_path("/dev/tape/by-id/synthetic", resolver=lambda p: "/dev/nst0")

    def test_resolved_path_is_pinned_and_executed(self):
        c = parse_config({"devices": [{"name": "d", "path": "/dev/tape/by-id/synthetic"}]},
                         resolver=lambda p: "/dev/sg7")
        self.assertEqual((c.devices[0].path, c.devices[0].resolved),
                         ("/dev/tape/by-id/synthetic", "/dev/sg7"))
        runner = FakeRunner({})
        collector(c, runner).collect()
        self.assertEqual({a[-1] for a in runner.calls}, {"/dev/sg7"})
        with self.assertRaises(ConfigError):
            parse_config({"devices": [{"name": "a", "path": "/dev/sg7"},
                                      {"name": "b", "path": "/dev/tape/x"}]},
                         resolver=lambda p: "/dev/sg7")

    def test_bad_names_collectors_and_duplicates(self):
        bad = [
            [{"name": "bad name", "path": "/dev/sg0"}],
            [{"name": "d", "path": "/dev/sg0", "collectors": ["mt"]}],
            [{"name": "d", "path": "/dev/sg0", "collectors": ["tapeinfo"]}],
            [{"name": "d", "path": "/dev/sg0", "collectors": ["sg_logs", "tapeinfo"]}],
            [{"name": "d", "path": "/dev/sg0"}, {"name": "d", "path": "/dev/sg1"}],
            [{"name": f"d{i}", "path": f"/dev/sg{i}"} for i in range(17)],
        ]
        for devs in bad:
            with self.assertRaises(ConfigError):
                parse_config({"devices": devs}, resolver=identity)
        with self.assertRaises(ConfigError):
            parse_config({"port": 70000}, resolver=identity)
        with self.assertRaises(ConfigError):
            parse_config({"tapeinfo_path": "/usr/sbin/tapeinfo"}, resolver=identity)

    def test_ports_and_addresses(self):
        for port in (-1, 65536, 9842.5, "9842", True, None):
            with self.assertRaises(ConfigError, msg=repr(port)):
                parse_config({"port": port}, resolver=identity)
        for addr in ("", "127.0.0.1\n", "a b", "127.0.0.1;rm", 5):
            with self.assertRaises(ConfigError, msg=repr(addr)):
                parse_config({"listen_address": addr}, resolver=identity)
        self.assertEqual(parse_config({"listen_address": "::1"}, resolver=identity).listen_address, "::1")

    def test_sg_logs_path_basename_enforced(self):
        for p in ("/usr/bin/tapeinfo", "/bin/sh", "sg_logs", "/usr/bin/sg_logs\n",
                  "/usr/bin/sg_logs ", "/usr/bin/../bin/sg_logs", "/opt/sg_logs/x"):
            with self.assertRaises(ConfigError, msg=repr(p)):
                parse_config({"sg_logs_path": p}, resolver=identity)
        self.assertEqual(parse_config({"sg_logs_path": "/opt/sg3/bin/sg_logs"},
                                      resolver=identity).sg_logs_path, "/opt/sg3/bin/sg_logs")

    def test_sg_logs_executable_startup_check(self):
        with tempfile.TemporaryDirectory() as d:
            exe = os.path.join(d, "sg_logs")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            with self.assertRaises(ConfigError):   # not executable
                check_sg_logs_executable(exe)
            os.chmod(exe, 0o755)
            self.assertEqual(check_sg_logs_executable(exe), exe)
            other = os.path.join(d, "sh")
            os.symlink("/bin/sh", other)
            link = os.path.join(d, "bin")
            os.mkdir(link)
            os.symlink("/bin/sh", os.path.join(link, "sg_logs"))
            with self.assertRaises(ConfigError):   # symlink to another program
                check_sg_logs_executable(os.path.join(link, "sg_logs"))
            with self.assertRaises(ConfigError):   # missing
                check_sg_logs_executable(os.path.join(d, "nope", "sg_logs"))


def fake_stat(mode=stat.S_IFCHR | 0o640, major=21, minor=3):
    return lambda p: SimpleNamespace(st_mode=mode, st_rdev=os.makedev(major, minor))


class DeviceRecheckTests(unittest.TestCase):
    dev = Device("d", "/dev/tape/by-id/synthetic", "/dev/sg3")

    def check(self, resolver=lambda p: "/dev/sg3", stat_fn=None, sysfs="1"):
        def sysfs_type(name):
            if isinstance(sysfs, Exception):
                raise sysfs
            self.assertEqual(name, "sg3")
            return sysfs
        return check_device(self.dev, resolver=resolver, stat_fn=stat_fn or fake_stat(),
                            sysfs_type=sysfs_type)

    def test_ok(self):
        self.assertIsNone(self.check())

    def test_symlink_repointed(self):
        self.assertEqual(self.check(resolver=lambda p: "/dev/sg4"), "device_changed")
        self.assertEqual(self.check(resolver=lambda p: "/dev/nst0"), "device_changed")

    def test_missing_and_wrong_node(self):
        def gone(p):
            raise FileNotFoundError(p)
        self.assertEqual(self.check(stat_fn=gone), "device_missing")

    def test_vanished_symlink_is_missing_not_changed(self):
        # A broken symlink resolves to itself, so the resolver comparison
        # alone would report device_changed; the node check comes first.
        def gone(p):
            raise FileNotFoundError(p)
        self.assertEqual(self.check(resolver=lambda p: p, stat_fn=gone), "device_missing")
        self.assertEqual(self.check(stat_fn=fake_stat(major=9)), "device_mismatch")   # st major
        self.assertEqual(self.check(stat_fn=fake_stat(mode=stat.S_IFREG | 0o644)), "device_mismatch")
        self.assertEqual(self.check(sysfs="0"), "device_mismatch")   # disk at that sg number
        self.assertEqual(self.check(sysfs=OSError("no sysfs")), "device_mismatch")

    def test_collector_skips_commands_on_mismatch(self):
        runner = FakeRunner({})
        out = Collector(cfg(), runner=runner,
                        device_check=lambda d: "device_changed").collect()
        self.assertEqual(runner.calls, [])
        self.assertIn('collector="sg_logs_write_errors",status="device_changed"} 1', out)
        self.assertIn('tape_exporter_collector_success{device="drive0",collector="sg_logs_read_errors"} 0', out)


class SgLogsParserTests(unittest.TestCase):
    def test_full_page(self):
        p = parse_sg_logs_error_counters(fx("sg_logs_write_ok.txt"), "write")
        self.assertEqual(p.status, "ok")
        self.assertEqual(p.values["corrected_with_delay"], 3)
        self.assertEqual(p.values["corrected_without_delay"], 0)
        self.assertEqual(p.values["bytes_processed"], 2199023255553)
        self.assertEqual(len(p.values), 7)

    def test_partial_and_malformed(self):
        p = parse_sg_logs_error_counters(fx("sg_logs_read_partial.txt"), "read")
        self.assertEqual(p.status, "ok")
        self.assertEqual(p.values, {"corrected_without_delay": 5, "bytes_processed": 1073741825})
        self.assertNotIn("rewrites_or_rereads", p.values)  # 'banana' -> missing, not 0

    def test_unknown_version_without_header(self):
        # Fail closed: without the expected header we can't tell which page
        # the numbers belong to.
        p = parse_sg_logs_error_counters(fx("sg_logs_unknown_version.txt"), "write")
        self.assertEqual((p.status, p.values), ("parse_error", {}))

    def test_headerless_param_lines_fail_closed(self):
        text = "  Total uncorrected errors = 2\n  Total bytes processed = 5\n"
        for d in ("write", "read"):
            self.assertEqual(parse_sg_logs_error_counters(text, d).status, "parse_error")

    def test_other_page_headers_not_attributed(self):
        verify = fx("sg3_utils_1.46/verify_page.txt")
        self.assertEqual(parse_sg_logs_error_counters(verify, "write").status, "parse_error")
        rr = "Read Reverse error counter page  [0x4]\n  Total uncorrected errors = 7\n"
        self.assertEqual(parse_sg_logs_error_counters(rr, "read").status, "parse_error")
        # Wrong page code under a matching name.
        bad = "Write error counter log page  [0x3]\n  Total uncorrected errors = 7\n"
        self.assertEqual(parse_sg_logs_error_counters(bad, "write").status, "parse_error")
        # Values after a foreign header are not added to the expected page.
        mixed = (fx("sg_logs_write_ok.txt")
                 + "Verify error counter page  [0x5]\n  Total rewrites or rereads = 999\n")
        p = parse_sg_logs_error_counters(mixed, "write")
        self.assertEqual(p.values["rewrites_or_rereads"], 17)

    def test_real_sg3_utils_1_46_output(self):
        p = parse_sg_logs_error_counters(fx("sg3_utils_1.46/write_page.txt"), "write")
        self.assertEqual(p.status, "ok")
        self.assertEqual(p.values, {
            "corrected_without_delay": 0, "corrected_with_delay": 3,
            "rewrites_or_rereads": 17, "total_corrected": 3,
            "correction_algorithm_processed": 21, "bytes_processed": 2199023255553,
            "total_uncorrected": 0})
        p = parse_sg_logs_error_counters(fx("sg3_utils_1.46/read_page.txt"), "read")
        self.assertEqual(p.values, {"corrected_without_delay": 5,
                                    "bytes_processed": 1073741825, "total_uncorrected": 2})

    def test_garbage_and_empty(self):
        for text in (fx("sg_logs_garbage.txt"), "", "\x00\xff"):
            self.assertEqual(parse_sg_logs_error_counters(text, "write").status, "parse_error")

    def test_wrong_page_ignored(self):
        p = parse_sg_logs_error_counters(fx("sg_logs_write_ok.txt"), "read")
        self.assertEqual(p.status, "parse_error")

    def test_overflow_dropped(self):
        text = "Write error counter log page  [0x2]\n  Total uncorrected errors = 99999999999999999999\n"
        p = parse_sg_logs_error_counters(text, "write")
        self.assertEqual((p.status, p.values), ("ok", {}))

    def test_duplicate_parameters_first_wins(self):
        text = ("Write error counter log page  [0x2]\n"
                "  Total uncorrected errors = 5\n"
                "  Total uncorrected errors = 10\n")
        p = parse_sg_logs_error_counters(text, "write")
        self.assertEqual(p.status, "ok")
        self.assertEqual(p.values["total_uncorrected"], 5)

    def test_malformed_first_value_blocks_later_duplicate(self):
        text = ("Write error counter log page  [0x2]\n"
                "  Total uncorrected errors = 99999999999999999999\n"
                "  Total uncorrected errors = 10\n")
        p = parse_sg_logs_error_counters(text, "write")
        self.assertEqual(p.status, "ok")
        self.assertNotIn("total_uncorrected", p.values)

    def test_exit_status_mapping_matches_sg_lib(self):
        # include/sg_lib.h + sg_convert_errno() in lib/sg_lib.c
        expect = {0: "ok", 1: "syntax_error", 2: "not_ready", 5: "unsupported",
                  9: "unsupported", 12: "not_ready", 13: "not_ready",
                  15: "file_error", 33: "device_timeout",
                  50: "os_error", 52: "os_error", 63: "os_error", 66: "os_error",
                  96: "os_error", 97: "error", 99: "error", 3: "error", 6: "unit_attention"}
        for rc, status in expect.items():
            self.assertEqual(sg_exit_status(rc), status, rc)
        self.assertNotIn(None, SG_EXIT_STATUS)


class OfflineTapeinfoParserTests(unittest.TestCase):
    """Offline text parser only; never connected to the exporter."""

    def test_ok(self):
        t = parse_tapeinfo_text(fx("tapeinfo_ok.txt"))
        self.assertEqual((t["vendor"], t["product"], t["ready"], t["compression_enabled"]),
                         ("SYNTHVND", "LTO-SIM", True, True))
        self.assertEqual(t["tapealert_codes_listed"], [3, 20])
        self.assertNotIn("FAKE-SERIAL", json.dumps(t))

    def test_no_alert_lines_is_unknown_not_zero(self):
        t = parse_tapeinfo_text(fx("tapeinfo_not_ready.txt"))
        self.assertIs(t["ready"], False)
        self.assertIsNone(t["compression_enabled"])
        self.assertEqual(t["tapealert_codes_listed"], [])
        self.assertEqual(t["tapealert_support"], "unknown")
        self.assertFalse(any("count" in k or "active" in k for k in t))

    def test_garbage(self):
        t = parse_tapeinfo_text("cannot open SCSI device\n")
        self.assertFalse(t["parsed"])
        self.assertIsNone(t["ready"])


def full_runner():
    return FakeRunner({
        "--page=0x2": RunResult("ok", 0, fx("sg_logs_write_ok.txt"), 0.12),
        "--page=0x3": RunResult("ok", 0, fx("sg_logs_read_partial.txt"), 0.08),
    })


class CollectorTests(unittest.TestCase):
    def test_metrics_and_no_serial_or_path(self):
        out = collector(cfg(), full_runner()).collect()
        self.assertIn('tape_log_errors_total{device="drive0",direction="write",parameter="total_uncorrected"} 0', out)
        self.assertIn('tape_bytes_processed_total{device="drive0",direction="write"} 2199023255553', out)
        self.assertIn('tape_log_parameter_present{device="drive0",direction="read",parameter="rewrites_or_rereads"} 0', out)
        self.assertNotIn('tape_log_errors_total{device="drive0",direction="read",parameter="rewrites_or_rereads"}', out)
        self.assertIn("tape_exporter_unreaped_children 0", out)
        self.assertNotIn("tapeinfo", out)
        self.assertNotIn("tapealert", out)
        self.assertNotIn("/dev/", out)

    def test_failures_are_distinguished(self):
        runner = FakeRunner({
            "--page=0x2": RunResult("timeout", None, "", 10.0),
            "--page=0x3": RunResult("ok", 5, "", 0.01),
        })
        out = collector(cfg(), runner).collect()
        self.assertIn('tape_exporter_collector_status{device="drive0",collector="sg_logs_write_errors",status="timeout"} 1', out)
        self.assertIn('tape_exporter_collector_status{device="drive0",collector="sg_logs_read_errors",status="unsupported"} 1', out)
        self.assertNotIn("tape_log_errors_total", out)
        self.assertNotIn("tape_log_parameter_present", out)

    def test_open_failure_is_os_error_not_zero(self):
        # sg_logs exits 50+errno when open() fails (52 = ENOENT, 63 = EACCES).
        runner = FakeRunner({"--page=0x2": RunResult("ok", 63, "", 0.01),
                             "--page=0x3": RunResult("ok", 52, "", 0.01)})
        out = collector(cfg(), runner).collect()
        self.assertIn('collector="sg_logs_write_errors",status="os_error"} 1', out)
        self.assertIn('collector="sg_logs_read_errors",status="os_error"} 1', out)
        self.assertNotIn("tape_log_errors_total", out)

    def test_stdout_ignored_on_nonzero_exit(self):
        runner = FakeRunner({"--page=0x2": RunResult("ok", 99, fx("sg_logs_write_ok.txt"), 0.01)})
        out = collector(cfg(), runner).collect()
        self.assertIn('collector="sg_logs_write_errors",status="error"} 1', out)
        self.assertNotIn("tape_log_errors_total", out)

    def test_cache(self):
        t = [0.0]
        runner = full_runner()
        c = collector(cfg(cache_seconds=30), runner, clock=lambda: t[0])
        c.collect(); c.collect()
        self.assertEqual(len(runner.calls), 2)
        t[0] = 31.0
        c.collect()
        self.assertEqual(len(runner.calls), 4)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(cfg(), collector(cfg(), full_runner()))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5)

    def test_binds_loopback_port0(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.port, 0)

    def test_metrics(self):
        with self.get("/metrics") as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
            body = r.read().decode()
        self.assertIn("# TYPE tape_log_errors_total counter", body)
        self.assertIn("tape_exporter_build_info", body)

    def test_ipv6_serves_metrics(self):
        c = cfg(listen_address="::1")
        try:
            srv = make_server(c, collector(c, full_runner()))
        except OSError:
            self.skipTest("no IPv6 loopback")
        self.assertIsInstance(srv, _Server6)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://[::1]:{srv.server_address[1]}/metrics",
                                        timeout=5) as r:
                self.assertEqual(r.status, 200)
                body = r.read().decode()
            self.assertIn("tape_exporter_build_info", body)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_head_metrics(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/metrics", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertTrue(r.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
            self.assertGreater(int(r.headers["Content-Length"]), 0)
            self.assertEqual(r.read(), b"")

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
        with cm.exception:
            self.assertEqual(cm.exception.code, 404)

    def test_500_hides_details(self):
        class Boom:
            def collect(self):
                raise RuntimeError("secret detail")
        srv = make_server(cfg(), Boom())
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/metrics", timeout=5)
            with cm.exception:
                self.assertEqual(cm.exception.code, 500)
                self.assertNotIn(b"secret", cm.exception.read())
        finally:
            srv.shutdown(); srv.server_close()

    def test_client_disconnect_is_handled(self):
        class Slow:
            def collect(self):
                time.sleep(0.3)
                return "x 1\n" * 50000
        srv = make_server(cfg(), Slow())
        errors = []
        srv.handle_error = lambda req, addr: errors.append(sys.exc_info()[1])
        t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
        port = srv.server_address[1]
        try:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                for _ in range(3):
                    s = socket.create_connection(("127.0.0.1", port), timeout=5)
                    s.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
                    # RST on close so the server's write fails immediately.
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    s.close()
                time.sleep(1.2)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                self.assertEqual(r.status, 200)
            self.assertEqual(errors, [])
            self.assertEqual(stderr.getvalue(), "")
        finally:
            srv.shutdown(); srv.server_close()


class MainTests(unittest.TestCase):
    def test_example_config_parses(self):
        with open(os.path.join(ROOT, "example-config.json")) as fh:
            c = parse_config(json.load(fh), resolver=lambda p: "/dev/sg3")
        self.assertEqual(c.devices[0].collectors, ("sg_logs",))

    def write_cfg(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with fh:
            fh.write(text)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_bad_config_exit_code(self):
        path = self.write_cfg('{"devices": [{"name": "d", "path": "/dev/st0"}]}')
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--config", path]), 2)

    def test_missing_sg_logs_fails_at_startup(self):
        path = self.write_cfg('{"sg_logs_path": "/nonexistent/bin/sg_logs"}')
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["--config", path]), 2)
        self.assertIn("sg_logs_path", err.getvalue())

    def test_invalid_cli_ports_and_addresses(self):
        path = self.write_cfg("{}")
        for args in (["--port", "-1"], ["--port", "65536"], ["--port", "80x"],
                     ["--port", ""], ["--port", "1e3"], ["--listen-address", ""],
                     ["--listen-address", "127.0.0.1\n"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                main(["--config", path] + args, sg_logs_check=lambda p: p)
            self.assertEqual(cm.exception.code, 2, args)

    def test_cli_entrypoint_version(self):
        r = subprocess.run([sys.executable, "-m", "lto_exporter", "--version"], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        from lto_exporter import __version__
        self.assertEqual(r.stdout.strip(), f"lto-exporter {__version__}")

    def test_cli_overrides_win_and_bind_failure_exit_1(self):
        path = self.write_cfg('{"port": 9842, "listen_address": "127.0.0.1"}')
        recorded = {}

        def fake_make_server(config, collector=None):
            recorded["port"] = config.port
            recorded["listen_address"] = config.listen_address
            raise OSError(errno.EADDRINUSE, "in use")

        err = io.StringIO()
        with mock.patch("lto_exporter.exporter.make_server", fake_make_server), \
                redirect_stderr(err):
            rc = main(["--config", path, "--port", "19842", "--listen-address", "::1"],
                      sg_logs_check=lambda p: p)
        self.assertEqual(rc, 1)
        self.assertEqual(recorded["port"], 19842)
        self.assertEqual(recorded["listen_address"], "::1")

    def test_sigterm_exits_zero(self):
        path = self.write_cfg("{}")
        code = (
            "import sys; sys.argv=['lto-exporter','--config',%r];"
            "from lto_exporter.exporter import main; sys.exit(main(sg_logs_check=lambda p: p))" % path
        )
        proc = subprocess.Popen([sys.executable, "-u", "-c", code], cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        line = b""
        deadline = time.time() + 10
        while time.time() < deadline:
            line = proc.stderr.readline()
            if not line or b"listening on" in line:
                break
        self.assertIn(b"listening on", line)
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("exporter still running 5 s after SIGTERM")
        finally:
            proc.stderr.close()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
