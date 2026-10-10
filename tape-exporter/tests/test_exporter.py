import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from lto_exporter.commands import (RunResult, UnsafeCommand, _check, run_command,
                                   sg_logs_argv, tapeinfo_argv)
from lto_exporter.config import ConfigError, parse_config, validate_device_path
from lto_exporter.exporter import Collector, make_server
from lto_exporter.parsers import parse_sg_logs_error_counters, parse_tapeinfo

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fx(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def identity(path):
    return path


def cfg(devices=None, **kw):
    raw = {"port": 0, "cache_seconds": 0,
           "devices": devices if devices is not None else
           [{"name": "drive0", "path": "/dev/sg3", "collectors": ["sg_logs", "tapeinfo"]}]}
    raw.update(kw)
    return parse_config(raw, resolver=identity)


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


class CommandTests(unittest.TestCase):
    def test_sg_logs_argv_is_fixed_and_readonly(self):
        self.assertEqual(sg_logs_argv("/usr/bin/sg_logs", "write", "/dev/sg3"),
                         ["/usr/bin/sg_logs", "--readonly", "--page=0x2", "/dev/sg3"])
        self.assertEqual(sg_logs_argv("/usr/bin/sg_logs", "read", "/dev/sg3")[2], "--page=0x3")
        with self.assertRaises(KeyError):
            sg_logs_argv("/usr/bin/sg_logs", "0x2e", "/dev/sg3")

    def test_tapeinfo_argv(self):
        self.assertEqual(tapeinfo_argv("/usr/sbin/tapeinfo", "/dev/sg3"),
                         ["/usr/sbin/tapeinfo", "-f", "/dev/sg3"])

    def test_forbidden_tokens_rejected(self):
        for tok in ("--reset", "-R", "--select", "-S"):
            with self.assertRaises(UnsafeCommand):
                _check(["/usr/bin/sg_logs", tok, "/dev/sg3"])

    def test_collector_only_issues_allowlisted_commands(self):
        runner = FakeRunner({})
        Collector(cfg(), runner=runner).collect()
        allowed = {("--readonly", "--page=0x2"), ("--readonly", "--page=0x3"), ("-f",)}
        self.assertEqual(len(runner.calls), 3)
        for argv in runner.calls:
            self.assertIn(tuple(argv[1:-1]), allowed)
            self.assertEqual(argv[-1], "/dev/sg3")

    def test_runner_no_shell_and_timeout(self):
        script = "import time; print('hi', flush=True); time.sleep(30)"
        res = run_command([sys.executable, "-c", script], timeout=0.5, max_bytes=4096)
        self.assertEqual(res.status, "timeout")
        self.assertIsNone(res.returncode)
        self.assertLess(res.duration, 5)

    def test_runner_caps_output(self):
        script = "import sys; sys.stdout.write('x' * 200000)"
        res = run_command([sys.executable, "-c", script], timeout=5, max_bytes=1024)
        self.assertEqual(res.status, "output_too_large")
        self.assertLessEqual(len(res.stdout), 1024)

    def test_runner_ok_and_shell_metachars_inert(self):
        res = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(id); ls"],
                          timeout=5, max_bytes=4096)
        self.assertEqual((res.status, res.returncode), ("ok", 0))
        self.assertEqual(res.stdout.strip(), "$(id); ls")

    def test_runner_missing_binary(self):
        res = run_command(["/nonexistent/sg_logs", "--readonly"], timeout=1, max_bytes=1024)
        self.assertEqual(res.status, "not_found")

    def test_runner_kwargs(self):
        seen = {}

        class P:
            def __init__(self, argv, **kw):
                seen.update(kw)
                raise FileNotFoundError

        run_command(["/x"], 1, 1024, popen=P)
        self.assertIs(seen["shell"], False)
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)


class ConfigTests(unittest.TestCase):
    def test_defaults_loopback(self):
        c = parse_config({}, resolver=identity)
        self.assertEqual(c.listen_address, "127.0.0.1")
        self.assertEqual(c.devices, ())

    def test_rejects_rewinding_and_non_sg_nodes(self):
        for p in ("/dev/st0", "/dev/nst0", "/dev/sda", "sg3", "/dev/../etc/passwd", "/tmp/sg0"):
            with self.assertRaises(ConfigError, msg=p):
                validate_device_path(p, resolver=identity)

    def test_symlink_must_resolve_to_sg(self):
        validate_device_path("/dev/tape/by-id/synthetic", resolver=lambda p: "/dev/sg7")
        with self.assertRaises(ConfigError):
            validate_device_path("/dev/tape/by-id/synthetic", resolver=lambda p: "/dev/nst0")

    def test_bad_names_collectors_and_duplicates(self):
        bad = [
            [{"name": "bad name", "path": "/dev/sg0"}],
            [{"name": "d", "path": "/dev/sg0", "collectors": ["mt"]}],
            [{"name": "d", "path": "/dev/sg0"}, {"name": "d", "path": "/dev/sg1"}],
            [{"name": f"d{i}", "path": f"/dev/sg{i}"} for i in range(17)],
        ]
        for devs in bad:
            with self.assertRaises(ConfigError):
                parse_config({"devices": devs}, resolver=identity)
        with self.assertRaises(ConfigError):
            parse_config({"port": 70000}, resolver=identity)


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
        p = parse_sg_logs_error_counters(fx("sg_logs_unknown_version.txt"), "write")
        self.assertEqual((p.status, p.values), ("ok", {"total_uncorrected": 2}))

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


class TapeinfoParserTests(unittest.TestCase):
    def test_ok(self):
        t = parse_tapeinfo(fx("tapeinfo_ok.txt"))
        self.assertEqual((t.vendor, t.product, t.ready, t.compression_enabled),
                         ("SYNTHVND", "LTO-SIM", 1, 1))
        self.assertEqual(t.tapealert_flags, (3, 20))

    def test_not_ready_partial(self):
        t = parse_tapeinfo(fx("tapeinfo_not_ready.txt"))
        self.assertEqual(t.ready, 0)
        self.assertIsNone(t.compression_enabled)
        self.assertEqual(t.tapealert_flags, ())

    def test_garbage(self):
        self.assertEqual(parse_tapeinfo("cannot open SCSI device\n").status, "parse_error")


def full_runner():
    return FakeRunner({
        "--page=0x2": RunResult("ok", 0, fx("sg_logs_write_ok.txt"), 0.12),
        "--page=0x3": RunResult("ok", 0, fx("sg_logs_read_partial.txt"), 0.08),
        "-f": RunResult("ok", 0, fx("tapeinfo_ok.txt"), 0.3),
    })


class CollectorTests(unittest.TestCase):
    def test_metrics_and_no_serial_or_path(self):
        out = Collector(cfg(), runner=full_runner()).collect()
        self.assertIn('tape_error_counter_total{device="drive0",direction="write",parameter="total_uncorrected"} 0', out)
        self.assertIn('tape_bytes_processed_total{device="drive0",direction="write"} 2199023255553', out)
        self.assertIn('tape_log_parameter_present{device="drive0",direction="read",parameter="rewrites_or_rereads"} 0', out)
        self.assertNotIn('tape_error_counter_total{device="drive0",direction="read",parameter="rewrites_or_rereads"}', out)
        self.assertIn('tape_exporter_collector_success{device="drive0",collector="tapeinfo"} 1', out)
        self.assertIn('tape_tapealert_flag{device="drive0",code="20"} 1', out)
        self.assertIn('tape_drive_ready{device="drive0"} 1', out)
        self.assertNotIn("FAKE-SERIAL", out)
        self.assertNotIn("/dev/", out)

    def test_failures_are_distinguished(self):
        runner = FakeRunner({
            "--page=0x2": RunResult("timeout", None, "", 10.0),
            "--page=0x3": RunResult("ok", 5, "", 0.01),
            "-f": RunResult("ok", 1, "", 0.01),
        })
        out = Collector(cfg(), runner=runner).collect()
        self.assertIn('tape_exporter_collector_status{device="drive0",collector="sg_logs_write_errors",status="timeout"} 1', out)
        self.assertIn('tape_exporter_collector_status{device="drive0",collector="sg_logs_read_errors",status="unsupported"} 1', out)
        self.assertIn('tape_exporter_collector_success{device="drive0",collector="tapeinfo"} 0', out)
        self.assertNotIn("tape_error_counter_total", out)
        self.assertNotIn("tape_log_parameter_present", out)

    def test_cache(self):
        t = [0.0]
        runner = full_runner()
        c = Collector(cfg(cache_seconds=30), runner=runner, clock=lambda: t[0])
        c.collect(); c.collect()
        self.assertEqual(len(runner.calls), 3)
        t[0] = 31.0
        c.collect()
        self.assertEqual(len(runner.calls), 6)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(cfg(), Collector(cfg(), runner=full_runner()))
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
        r = self.get("/metrics")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/plain; version=0.0.4"))
        body = r.read().decode()
        self.assertIn("# TYPE tape_error_counter_total counter", body)
        self.assertIn("tape_exporter_build_info", body)

    def test_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/nope")
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
            self.assertEqual(cm.exception.code, 500)
            self.assertNotIn(b"secret", cm.exception.read())
        finally:
            srv.shutdown(); srv.server_close()


class MainTests(unittest.TestCase):
    def test_example_config_parses(self):
        with open(os.path.join(os.path.dirname(FIX), "..", "example-config.json")) as fh:
            c = parse_config(json.load(fh), resolver=identity)
        self.assertEqual(c.devices[0].collectors, ("sg_logs",))

    def test_bad_config_exit_code(self):
        from lto_exporter.exporter import main
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write('{"devices": [{"name": "d", "path": "/dev/st0"}]}')
        try:
            self.assertEqual(main(["--config", fh.name]), 2)
        finally:
            os.unlink(fh.name)


if __name__ == "__main__":
    unittest.main()
