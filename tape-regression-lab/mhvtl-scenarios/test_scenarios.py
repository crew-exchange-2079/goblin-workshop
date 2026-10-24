"""Unit tests for the mhvtl scenario driver, without mhvtl.

A small fake of the emulated drive (the mhvtl semantics the scenarios rely
on, each cited in README.md) produces sg_logs text in sg3_utils 1.46 format.
The REAL lto-exporter Collector scrapes it through a fake runner, so these
tests run the actual scenario code and assertions end to end, and check that
the assertions fail when the exporter or the emulator misbehaves.
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scenarios as sc  # noqa: E402

from lto_exporter.commands import RunResult  # noqa: E402
from lto_exporter.config import parse_config  # noqa: E402
from lto_exporter.exporter import Collector  # noqa: E402

SG = "/dev/sg7"
MiB = 1024 * 1024
EW = 2 * MiB
TA_CLEANING_MEDIA = 0x400
OFFLINE_RC = 99   # observed on mhvtl in CI: sg_lib "other" -> "error"

SENSE = ("    SCSI Status: Check Condition \n\nSense Information:\n"
         "Fixed format, current; Sense key: {key}\nAdditional sense: {asc}\n"
         " Raw sense data (in hex), 18 bytes:\n       70 00 0d 00 00 00 00 0a\n")


def sense(key, asc):
    return SENSE.format(key=key, asc=asc)


def ec_page(direction):
    code = {"write": 2, "read": 3}[direction]
    names = ["Errors corrected without substantial delay", "Errors corrected with possible delays",
             "Total rewrites or rereads", "Total errors corrected",
             "Total times correction algorithm processed", "Total bytes processed",
             "Total uncorrected errors"]
    return "\n".join([f"{direction.capitalize()} error counter page  [0x{code}]"]
                     + [f"  {n} = 0" for n in names]) + "\n"


def sa_page(c, cleaning):
    def gb(n):
        return f"{n // 1_000_000_000} GB [{n} bytes]"
    return ("Sequential access device page (ssc-3)\n"
            f"  Data bytes received with WRITE commands: {gb(c['wi'])}\n"
            f"  Data bytes written to media by WRITE commands: {gb(c['wm'])}\n"
            f"  Data bytes read from media by READ commands: {gb(c['rm'])}\n"
            f"  Data bytes transferred by READ commands: {gb(c['ri'])}\n"
            "  Native capacity from BOP to EOD: 4294967295 MB\n"
            + ("  Cleaning action required\n" if cleaning
               else "  Cleaning action not required (or completed)\n")
            + f"    cleaning value: {cleaning}\n")


class FakeDrive:
    """The mhvtl behaviour the scenarios assert, and nothing more."""

    def __init__(self, unload_resets=False, tapealert_clears_on_unload=False):
        self.media, self.loaded, self.pos, self.state = {}, None, 0, "running"
        self.tapealert = 0
        self.zero()
        self.unload_resets = unload_resets
        self.tapealert_clears_on_unload = tapealert_clears_on_unload

    def zero(self):
        self.c = {"wi": 0, "wm": 0, "rm": 0, "ri": 0}

    def sg_logs(self, page):
        if self.state != "running":
            return OFFLINE_RC, ""
        if page == "0x2":
            return 0, ec_page("write")
        if page == "0x3":
            return 0, ec_page("read")
        return 0, sa_page(self.c, self.tapealert)


class FakeRig:
    """Same interface as scenarios.Rig, backed by FakeDrive and the real Collector."""

    def __init__(self, drive=None, exporter_runner=None, cache_seconds=0):
        self.d = drive or FakeDrive()
        self.log = []
        config = parse_config({"port": 0, "cache_seconds": cache_seconds,
                               "devices": [{"name": "mhvtl_drive0", "path": SG}]},
                              resolver=lambda p: p)
        self.collector = Collector(config, runner=exporter_runner or self.runner,
                                   device_check=lambda dev: None)

    def runner(self, argv, timeout, max_bytes):
        page = [a for a in argv if a.startswith("--page=")][0].split("=", 1)[1]
        rc, out = self.d.sg_logs(page)
        return RunResult("ok", rc, out, 0.01)

    # harness actions
    def mktape(self, barcode, kind, size_mb):
        self.d.media[barcode] = {"kind": kind, "cap": size_mb * MiB, "blocks": []}

    def load(self, barcode, cleaning=False):
        m = self.d.media[barcode]
        self.d.loaded, self.d.pos = barcode, 0
        self.d.zero()   # mhvtl loadTape(): byte counters are per load
        if m["kind"] == "clean":
            self.d.tapealert |= TA_CLEANING_MEDIA
        if cleaning != (m["kind"] == "clean"):
            raise sc.HarnessError("load wait mismatch")

    def unload(self):
        self.d.loaded = None
        if self.d.unload_resets:
            self.d.zero()
        if self.d.tapealert_clears_on_unload:
            self.d.tapealert = 0

    def _m(self):
        return self.d.media[self.d.loaded] if self.d.loaded else None

    def write(self):
        m = self._m()
        if m is None or m["kind"] == "clean":
            return "not_ready"
        blocks = m["blocks"]
        if m["kind"] == "WORM" and self.d.pos != len(blocks):
            return "data_protect"
        used = sum(b for b in blocks[:self.d.pos] if b)
        if used >= m["cap"]:
            return "overflow"
        del blocks[self.d.pos:]
        blocks.append(sc.BLOCK)
        self.d.pos += 1
        self.d.c["wi"] += sc.BLOCK
        self.d.c["wm"] += sc.BLOCK
        return "ok_eom" if used + sc.BLOCK >= m["cap"] - EW else "ok"

    def write_filemark(self):
        m = self._m()
        del m["blocks"][self.d.pos:]
        m["blocks"].append(0)
        self.d.pos += 1
        return "ok"

    def read(self):
        m = self._m()
        if self.d.pos >= len(m["blocks"]) or not m["blocks"][self.d.pos]:
            return "blank_check"
        self.d.pos += 1
        self.d.c["ri"] += sc.BLOCK
        self.d.c["rm"] += sc.BLOCK
        return "ok"

    def rewind(self):
        self.d.pos = 0
        return "ok"

    def space_eod(self):
        self.d.pos = len(self._m()["blocks"])
        return "ok"

    def tur(self):
        if self.d.loaded is None:
            return "not_ready", sense("Not Ready", "Medium not present")
        if self._m()["kind"] == "clean":
            return "not_ready", sense("Not Ready", "Cleaning cartridge installed")
        return "ok", ""

    def wait_for(self, what, pred, timeout=90):
        last = self.tur()
        if not pred(*last):
            raise sc.HarnessError(f"timed out waiting for {what}; last TUR: {last}")
        return last

    def set_state(self, state):
        self.d.state = state

    def ack_tapealert(self):
        self.d.tapealert = 0

    def harness_reads(self):
        return {p: self.d.sg_logs(p) for p in sc.PAGES}

    def scrape(self):
        return self.collector.collect()

    def recover(self):
        self.d.state, self.d.loaded, self.d.tapealert = "running", None, 0


def run(rig, names=None):
    checks, outcome = sc.run_scenarios(rig, None, names)
    return checks, outcome


def failed(checks):
    return [(c["scenario"], c["check"]) for c in checks if c["result"] == "FAIL"]


class ScenarioTests(unittest.TestCase):
    def test_all_scenarios_pass_against_faithful_fake(self):
        checks, outcome = run(FakeRig())
        self.assertEqual(failed(checks), [])
        self.assertEqual(set(outcome), set(sc.SCENARIOS))
        self.assertTrue(all(o["result"] == "PASS" and o["checks"] > 0 for o in outcome.values()))

    def test_each_scenario_has_metric_assertions_not_just_agreement(self):
        checks, _ = run(FakeRig())
        for name in sc.SCENARIOS:
            names = [c["check"] for c in checks if c["scenario"] == name]
            self.assertTrue(any("agrees with harness" in n for n in names), name)
            self.assertTrue(any("tape_data_bytes_total" in n or "status ==" in n for n in names), name)

    def test_stale_exporter_cache_is_caught(self):
        checks, outcome = run(FakeRig(cache_seconds=3600))
        self.assertEqual(outcome["load_unload"]["result"], "FAIL")
        self.assertIn(("load_unload", "written: exporter agrees with harness sg_logs"), failed(checks))

    def test_exporter_reporting_zero_for_unknown_is_caught(self):
        rig = FakeRig()

        def zeros_when_offline(argv, timeout, max_bytes):
            res = rig.runner(argv, timeout, max_bytes)
            if res.returncode == OFFLINE_RC:   # a broken exporter: pretend the page read fine
                page = [a for a in argv if a.startswith("--page=")][0][7:]
                text = sa_page({"wi": 0, "wm": 0, "rm": 0, "ri": 0}, 0) if page == "0xc" else (
                    ec_page("write" if page == "0x2" else "read"))
                return RunResult("ok", 0, text, 0.01)
            return res
        rig.collector.runner = zeros_when_offline
        checks, outcome = run(rig, ["drive_offline"])
        self.assertEqual(outcome["drive_offline"]["result"], "FAIL")
        self.assertIn(("drive_offline", "offline: no counters, flags or present series exported"),
                      failed(checks))

    def test_exporter_dropping_cleaning_flag_is_caught(self):
        rig = FakeRig()

        def no_cleaning_line(argv, timeout, max_bytes):
            res = rig.runner(argv, timeout, max_bytes)
            text = "\n".join(l for l in res.stdout.splitlines() if "leaning" not in l) + "\n"
            return RunResult(res.status, res.returncode, text, res.duration)
        rig.collector.runner = no_cleaning_line
        checks, outcome = run(rig, ["cleaning_cartridge"])
        self.assertIn(("cleaning_cartridge", "cleaning_cart_mounted: tape_cleaning_required == 1"),
                      failed(checks))

    def test_emulator_semantics_drift_is_caught(self):
        _, outcome = run(FakeRig(FakeDrive(unload_resets=True)), ["load_unload"])
        self.assertEqual(outcome["load_unload"]["result"], "FAIL")
        checks, _ = run(FakeRig(FakeDrive(tapealert_clears_on_unload=True)), ["cleaning_cartridge"])
        self.assertIn(("cleaning_cartridge", "unloaded_flag_sticky: tape_cleaning_required == 1"),
                      failed(checks))

    def test_worm_that_allows_overwrite_is_caught(self):
        rig = FakeRig()
        orig = rig.write

        def lax_write():
            m = rig._m()
            if m and m["kind"] == "WORM":
                m["kind"] = "data"
                try:
                    return orig()
                finally:
                    m["kind"] = "WORM"
            return orig()
        rig.write = lax_write
        checks, _ = run(rig, ["worm"])
        self.assertIn(("worm", "harness: overwrite at BOT refused with DATA PROTECT"), failed(checks))

    def test_harness_error_fails_one_scenario_and_the_rest_still_run(self):
        rig = FakeRig()
        rig.mktape = lambda *a: (_ for _ in ()).throw(sc.HarnessError("mktape broke"))
        checks, outcome = run(rig)
        self.assertEqual(outcome["baseline"]["result"], "PASS")
        for name in ("load_unload", "full_tape", "drive_offline", "worm"):
            self.assertEqual(outcome[name]["result"], "FAIL", name)
            self.assertIn("mktape broke", outcome[name]["error"])

    def test_scenario_names_are_deduplicated(self):
        _, outcome = run(FakeRig(), ["baseline", "worm", "baseline"])
        self.assertEqual(list(outcome), ["baseline", "worm"])

    def test_full_tape_counts_only_accepted_writes(self):
        checks, _ = run(FakeRig(), ["full_tape"])
        summary = [c for c in checks if c["check"].startswith("harness: WRITEs accepted")][0]
        self.assertEqual(summary["result"], "PASS")
        d = summary["detail"]
        self.assertEqual(d["last"], "overflow")
        self.assertEqual(d["accepted"], d["writes"] - 1)
        self.assertEqual(d["accepted"] * sc.BLOCK, sc.FULL_TAPE_MB * MiB)


class RigCommandTests(unittest.TestCase):
    """Argv the real rig sends; mhvtl's vtlcmd rejects other arities."""

    def rig(self):
        rig = sc.Rig.__new__(sc.Rig)
        rig.bin, rig.drive, rig.loaded, rig.calls = "/m/usr/bin", 11, None, []
        rig.run = lambda argv, timeout=60, check=False: (rig.calls.append(argv), (0, ""))[1]
        rig.wait_for = lambda what, pred, timeout=90: ("ok", "")
        return rig

    def test_load_then_unload_passes_the_barcode(self):
        rig = self.rig()
        rig.load("GWS001L8")
        rig.unload()
        self.assertEqual([c[1:] for c in rig.calls],
                         [["11", "load", "GWS001L8"], ["11", "unload", "GWS001L8"]])
        self.assertIsNone(rig.loaded)

    def test_unload_without_known_media_still_has_an_id(self):
        rig = self.rig()
        rig.unload()
        self.assertEqual(len(rig.calls[0]), 4)

    def test_set_state_writes_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            rig = sc.Rig.__new__(sc.Rig)
            rig.state_path = os.path.join(d, "state")
            rig.log = open(os.devnull, "w")
            self.addCleanup(rig.log.close)

            rig.set_state("offline")
            with open(rig.state_path) as fh:
                self.assertEqual(fh.read(), "offline\n")


class RunShSafetyTests(unittest.TestCase):
    """run.sh can't be run here (it loads a kernel module); pin its cleanup
    ownership rules so a refusal never tears down someone else's mhvtl."""

    def setUp(self):
        with open(os.path.join(HERE, "run.sh")) as fh:
            self.src = fh.read()

    def test_dirty_host_refused_before_trap_and_before_creating_anything(self):
        refuse = self.src.index("already exists")
        self.assertLess(refuse, self.src.index("trap cleanup EXIT"))
        self.assertLess(refuse, self.src.index("mktemp -d"))
        self.assertLess(refuse, self.src.index("insmod "))

    def test_rmmod_only_for_a_module_this_run_inserted(self):
        self.assertIn('[ "$INSERTED_MODULE" = 1 ] && grep -q \'^mhvtl \' /proc/modules', self.src)
        self.assertLess(self.src.index('insmod "$MHVTL_SRC'), self.src.index("INSERTED_MODULE=1"))

    def test_processes_killed_by_pid_tree_not_by_user(self):
        self.assertNotIn("pkill", self.src)
        self.assertNotIn("killall", self.src)


class ClassifyTests(unittest.TestCase):
    def test_outcomes(self):
        cases = [
            (0, "", "ok"),
            (0, "    SCSI Status: Good \n\n", "ok"),
            (99, sense("No Sense", "End-of-partition/medium detected"), "ok_eom"),
            (99, sense("No Sense", "No additional sense information"), "ok_sense"),
            (99, sense("Volume Overflow", "End-of-partition/medium detected"), "overflow"),
            (99, sense("Data Protect", "Medium overwrite attempted"), "data_protect"),
            (2, sense("Not Ready", "Medium not present"), "not_ready"),
            (6, sense("Unit Attention", "Power on, reset, or bus device reset occurred"), "unit_attention"),
            (99, sense("Medium Error", "Write error"), "error:medium error"),
            (56, "sg_raw: failed to open /dev/sg7: No such device or address\n", "rc56"),
        ]
        for rc, text, want in cases:
            self.assertEqual(sc.classify_scsi(rc, text), want, text)

    def test_be24(self):
        self.assertEqual(sc.be24(65536), ["01", "00", "00"])


class AgreementTests(unittest.TestCase):
    def _snap(self, drive):
        rig = FakeRig(drive)
        return sc.snapshot(rig.scrape()), sc.harness_view(rig.harness_reads())

    def test_agree(self):
        s, h = self._snap(FakeDrive())
        self.assertEqual(sc.agreement_problems(s, h), [])
        self.assertEqual(h["0xc"]["values"]["cleaning_action_required"], 0)

    def test_failed_read_must_not_have_values(self):
        d = FakeDrive()
        s, _ = self._snap(d)
        d.state = "offline"
        h = sc.harness_view(FakeRig(d).harness_reads())
        self.assertEqual(h["0x2"]["status"], "error")
        problems = sc.agreement_problems(s, h)
        self.assertTrue(any("but exporter has values" in p for p in problems), problems)

    def test_unreaped_children_reported(self):
        s, h = self._snap(FakeDrive())
        s["unreaped"] = ["1"]
        self.assertIn("tape_exporter_unreaped_children=['1']", sc.agreement_problems(s, h))


class VerdictTests(unittest.TestCase):
    def test_missing_scenario_results_is_fail(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "strace.txt"), "w").close()
            rc = sc.verdict(["--evidence", d, "--sg", SG, "--sg-logs", "/usr/bin/sg_logs"])
            self.assertEqual(rc, 1)
            with open(os.path.join(d, "summary.json")) as fh:
                self.assertEqual(json.load(fh)["overall"], "FAIL")

    def test_passing_scenarios_but_empty_strace_is_fail(self):
        # Without exporter activity in strace there is no evidence it stayed read-only.
        with tempfile.TemporaryDirectory() as d:
            checks, outcome = run(FakeRig(), ["baseline"])
            with open(os.path.join(d, "scenario_checks.json"), "w") as fh:
                json.dump({"scenarios": outcome, "checks": checks}, fh)
            open(os.path.join(d, "strace.txt"), "w").close()
            self.assertEqual(sc.verdict(["--evidence", d, "--sg", SG, "--sg-logs", "/usr/bin/sg_logs"]), 1)
            with open(os.path.join(d, "summary.md")) as fh:
                self.assertIn("| baseline | PASS |", fh.read())


if __name__ == "__main__":
    unittest.main()
