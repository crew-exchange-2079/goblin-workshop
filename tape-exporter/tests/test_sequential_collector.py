"""Collector and metrics for the SSC Sequential-Access Device page (0x0c).

Synthetic runner only; no device or subprocess access.
"""

import unittest

from lto_exporter.commands import RunResult, _check
from tests.test_exporter import FakeRunner, cfg, collector, full_runner, fx

DEV = [{"name": "drive0", "path": "/dev/sg3"}]
SA = 'collector="sg_logs_sequential_access"'


def sa_runner(result):
    return FakeRunner({
        "--page=0x2": RunResult("ok", 0, fx("sg_logs_write_ok.txt"), 0.1),
        "--page=0x3": RunResult("ok", 0, fx("sg_logs_read_partial.txt"), 0.1),
        "--page=0xc": result,
    })


class SequentialAccessCollectorTests(unittest.TestCase):
    def test_metrics_from_real_146_output(self):
        out = collector(cfg(), full_runner()).collect()
        for line in (
            f'tape_exporter_collector_success{{device="drive0",{SA}}} 1',
            f'tape_exporter_collector_status{{device="drive0",{SA},status="ok"}} 1',
            'tape_data_bytes_total{device="drive0",direction="write",side="host"} 2199023255553',
            'tape_data_bytes_total{device="drive0",direction="write",side="media"} 1500000000123',
            'tape_data_bytes_total{device="drive0",direction="read",side="media"} 734003200000',
            'tape_data_bytes_total{device="drive0",direction="read",side="host"} 1468006400000',
            'tape_cleaning_required{device="drive0"} 1',
            'tape_sequential_access_field_present{device="drive0",field="cleaning_action_required"} 1',
            "# TYPE tape_data_bytes_total counter",
            "# TYPE tape_cleaning_required gauge",
        ):
            self.assertIn(line, out)
        self.assertNotIn("/dev/", out)

    def test_partial_page_unknown_is_not_zero(self):
        runner = sa_runner(RunResult("ok", 0, fx("sg3_utils_1.46/seqaccess_clean_page.txt"), 0.1))
        out = collector(cfg(), runner).collect()
        self.assertIn('tape_cleaning_required{device="drive0"} 0', out)
        self.assertIn('tape_data_bytes_total{device="drive0",direction="write",side="host"} 999999999', out)
        self.assertIn('tape_data_bytes_total{device="drive0",direction="write",side="media"} 0', out)
        self.assertNotIn('direction="read",side=', out)
        self.assertIn('tape_sequential_access_field_present{device="drive0",field="bytes_read_media"} 0', out)

    def test_nonverbose_output_exports_no_bytes(self):
        runner = sa_runner(RunResult("ok", 0, fx("sg3_utils_1.46/seqaccess_page_nonverbose.txt"), 0.1))
        out = collector(cfg(), runner).collect()
        self.assertNotIn("tape_data_bytes_total", out)
        self.assertIn('tape_cleaning_required{device="drive0"} 1', out)

    def test_failures_export_no_values(self):
        cases = (
            (RunResult("ok", 5, "", 0.01), "unsupported"),
            (RunResult("ok", 2, "", 0.01), "not_ready"),
            (RunResult("timeout", None, "", 10.0), "timeout"),
            (RunResult("ok", 0, fx("sg3_utils_1.46/seqaccess_page_as_disk.txt"), 0.01), "parse_error"),
            (RunResult("ok", 99, fx("sg3_utils_1.46/seqaccess_page.txt"), 0.01), "error"),
        )
        for result, status in cases:
            with self.subTest(status=status):
                out = collector(cfg(), sa_runner(result)).collect()
                self.assertIn(f'{SA},status="{status}"}} 1', out)
                self.assertIn(f'tape_exporter_collector_success{{device="drive0",{SA}}} 0', out)
                self.assertNotIn("tape_data_bytes_total", out)
                self.assertNotIn("tape_cleaning_required", out)
                self.assertNotIn("tape_sequential_access_field_present", out)
                # the error-counter collectors are unaffected
                self.assertIn('tape_bytes_processed_total{device="drive0",direction="write"}', out)

    def test_device_check_failure_runs_nothing(self):
        runner = full_runner()
        out = collector(cfg(), runner, device_check=lambda d: "device_missing").collect()
        self.assertEqual(runner.calls, [])
        self.assertIn(f'{SA},status="device_missing"}} 1', out)
        self.assertNotIn("tape_cleaning_required", out)

    def test_device_check_runs_once_per_device(self):
        seen = []
        collector(cfg(), full_runner(), device_check=lambda d: seen.append(d.name)).collect()
        self.assertEqual(seen, ["drive0"])

    def test_collector_selection(self):
        for cols, pages in ((["sg_logs"], {"--page=0x2", "--page=0x3"}),
                            (["sequential_access"], {"--page=0xc"}),
                            (["sequential_access", "sg_logs"], {"--page=0x2", "--page=0x3", "--page=0xc"})):
            with self.subTest(cols=cols):
                runner = full_runner()
                out = collector(cfg(devices=[{**DEV[0], "collectors": cols}]), runner).collect()
                self.assertEqual({a[-2] for a in runner.calls}, pages)
                for argv in runner.calls:
                    _check(argv)
                self.assertEqual("sg_logs_sequential_access" in out, "--page=0xc" in pages)

    def test_parser_exception_is_internal_error(self):
        from unittest import mock
        with mock.patch("lto_exporter.exporter.parse_sg_logs_sequential_access",
                        side_effect=RuntimeError("secret")):
            out = collector(cfg(), full_runner()).collect()
        self.assertIn(f'{SA},status="internal_error"}} 1', out)
        self.assertNotIn("secret", out)
        self.assertIn('collector="sg_logs_write_errors",status="ok"} 1', out)
