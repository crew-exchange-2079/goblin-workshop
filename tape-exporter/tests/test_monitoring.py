import json
import os
import re
import sys
import unittest

from lto_exporter.exporter import COLLECTOR_STATUSES, Collector, HELP, METRIC_LABELS
from lto_exporter.parsers import SG_EXIT_STATUS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ci"))
import check_monitoring as cm  # noqa: E402


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class MonitoringAssetTests(unittest.TestCase):
    def test_dashboard_and_rules_match_exporter(self):
        errors, nd, nr = cm.run()
        self.assertEqual(errors, [])
        self.assertGreaterEqual(nd, 10)
        self.assertEqual(nr, 8)

    def test_checker_catches_mismatches(self):
        bad = {
            'tape_log_errors_total{parameter="total_uncorrected_errors"}': "never emitted",
            'tape_exporter_collector_status{status="open_failed"}': "never emitted",
            'tape_bytes_processed_total{parameter="bytes_processed"}': "no label",
            'tape_drive_temperature_celsius': "unknown metric",
            'sum by (serial) (tape_exporter_collector_success)': "grouping label",
            'tape_exporter_unreaped_children{device="d"}': "no label",
            'tape_data_bytes_total{side="compressed"}': "never emitted",
            'tape_sequential_access_field_present{field="tapealert"}': "never emitted",
            'tape_cleaning_required{direction="write"}': "no label",
        }
        for expr, msg in bad.items():
            errors = []
            cm.check_expr("t", expr, errors)
            self.assertTrue(any(msg in e for e in errors), (expr, errors))

    def test_label_table_matches_rendered_output(self):
        from tests.test_exporter import cfg, collector, full_runner
        out = collector(cfg(), full_runner()).collect()
        seen = {}
        for line in out.splitlines():
            m = re.match(r"([a-z_]+)(?:\{(.*)\})? ", line)
            if m and not line.startswith("#"):
                labels = tuple(re.findall(r'([a-z_]+)="', m.group(2) or ""))
                seen.setdefault(m.group(1), set()).add(labels)
        for metric, label_sets in seen.items():
            self.assertIn(metric, HELP)
            self.assertEqual(label_sets, {METRIC_LABELS[metric]}, metric)
        self.assertEqual(set(HELP), set(METRIC_LABELS))

    def test_every_status_is_documented(self):
        produced = set(SG_EXIT_STATUS.values()) | {
            "parse_error", "timeout", "output_too_large", "read_error",
            "not_found", "exec_error", "blocked_unreaped", "device_changed",
            "device_mismatch", "device_missing", "internal_error", "error"}
        self.assertEqual(produced, set(COLLECTOR_STATUSES))
        readme = read("README.md")
        for status in COLLECTOR_STATUSES:
            self.assertIn(f"`{status}`", readme, status)

    def test_dashboard_is_json_with_stable_uid(self):
        dash = json.loads(read("monitoring", "grafana", "lto-exporter-dashboard.json"))
        self.assertEqual(dash["uid"], "lto-exporter-v1")
        self.assertNotIn("serial", json.dumps(dash).lower())


class ServiceExampleTests(unittest.TestCase):
    unit = read("contrib", "systemd", "lto-exporter.service")

    def directives(self):
        out = {}
        for line in self.unit.splitlines():
            line = line.strip()
            if line and not line.startswith(("#", ";", "[")) and "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k, []).append(v)
        return out

    def test_no_blanket_disk_access_or_privilege(self):
        d = self.directives()
        groups = " ".join(d.get("SupplementaryGroups", []) + d.get("Group", []))
        self.assertNotRegex(groups, r"\b(disk|tape|root|wheel|sudo)\b")
        self.assertNotIn("User", d)                     # DynamicUser instead
        self.assertEqual(d.get("DynamicUser"), ["yes"])
        self.assertEqual(d.get("NoNewPrivileges"), ["yes"])
        self.assertEqual(d.get("CapabilityBoundingSet"), [""])
        self.assertEqual(d.get("AmbientCapabilities"), [""])
        self.assertEqual(d.get("DevicePolicy"), ["closed"])
        for allow in d.get("DeviceAllow", []):
            self.assertRegex(allow, r"^/dev/sg[0-9]+ r$")  # one node, read only
        self.assertEqual(d.get("ProtectSystem"), ["strict"])
        values = " ".join(v for vs in d.values() for v in vs)
        self.assertNotIn("CAP_", values)

    def test_udev_example_is_narrow(self):
        rule = read("contrib", "udev", "60-lto-exporter.rules.example")
        active = [l for l in rule.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertEqual(active, [])                     # fully commented example
        self.assertNotIn('GROUP="disk"', rule)
        self.assertIn('MODE="0640"', rule)


if __name__ == "__main__":
    unittest.main()
