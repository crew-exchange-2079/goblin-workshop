"""Standard-library tests: symbology tables, volser rules, layouts, PDF structure, CLI."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tapelabel as tl  # noqa: E402


def cli(*args):
    p = subprocess.run([sys.executable, str(ROOT / "tapelabel.py"), *args],
                       capture_output=True, timeout=30)
    return p.returncode, p.stdout, p.stderr.decode()


class Symbology(unittest.TestCase):
    def test_every_pattern_is_nine_elements_three_wide_and_unique(self):
        for ch, pat in tl.CODE39.items():
            self.assertEqual(len(pat), 9, ch)
            self.assertEqual(pat.count("w"), 3, ch)
            self.assertLessEqual(set(pat), {"n", "w"}, ch)
        self.assertEqual(len(set(tl.CODE39.values())), len(tl.CODE39))
        self.assertEqual(set(tl.CODE39) - {"*"}, set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"))

    def test_symbol_width(self):
        # 10 characters (start + 8 + stop) of 6X + 3*2.75X, plus 9 one-X gaps.
        self.assertAlmostEqual(tl.symbol_width_mm("TAP001L9"), 151.5 * 0.423, places=9)

    def test_bars_alternate_and_end_on_a_bar(self):
        mods = tl.modules("TAP001L9")
        self.assertEqual(len(mods), 10 * 9 + 9)
        self.assertEqual(len(mods) % 2, 1)  # starts and ends with a bar
        bars = tl.bars_mm("TAP001L9")
        self.assertEqual(len(bars), 50)
        last_x, last_w = bars[-1]
        self.assertAlmostEqual(last_x + last_w, tl.symbol_width_mm("TAP001L9"))

    def test_unencodable(self):
        with self.assertRaises(ValueError):
            tl.modules("tap")

    def test_golden_matches_module(self):
        # label.js is checked against the same file by label.test.js.
        golden = json.loads((ROOT / "tests" / "golden.json").read_text())
        self.assertEqual(golden, json.loads(json.dumps(tl.spec())))


class Validate(unittest.TestCase):
    def test_good(self):
        for lab in ("TAP001L9", "000000L5", "ZZZZZZLZ", "CLN001CU", "A1B2C3LV"):
            self.assertEqual(tl.check(lab), [], lab)

    def test_bad(self):
        cases = {
            "TAP01L9": "6-char volser",
            "TAP0001L9": "6-char volser",
            "tap001L9": "uppercase only",
            "TAP 01L9": "A-Z 0-9",
            "TAP-01L9": "A-Z 0-9",
            "TAP001L4": "unknown media ID",
            "TAP001LA": "unknown media ID",
            "TAP001l9": "unknown media ID",
            "TAP001CU": "CLN-prefixed",
            "CLN001L9": "cleaning",
            "TAPÉ01L9": "A-Z 0-9",
        }
        for lab, why in cases.items():
            with self.assertRaisesRegex(tl.LabelError, why, msg=lab):
                tl.check(lab)

    def test_ambiguous_warns_and_strict_refuses(self):
        self.assertEqual(len(tl.check("O0AAAAL9")), 1)
        self.assertEqual(len(tl.check("I1AAAAL9")), 1)
        self.assertEqual(tl.check("OOAAAAL9"), [])
        with self.assertRaises(tl.LabelError):
            tl.check("I1AAAAL9", strict=True)

    def test_mod43_lookalike(self):
        # ZXing reads STUVWXLW as ]A1 (check character validated): S T U V W X L
        # are 28+29+30+31+32+33+21 = 204 = 32 (mod 43) -> 'W'.
        self.assertEqual(tl.mod43("STUVWXL"), "W")
        self.assertTrue(tl.looks_checked("STUVWXLW"))
        self.assertFalse(tl.looks_checked("TAP001L9"))
        # A note, not a warning: --strict still accepts it.
        self.assertEqual(tl.check("STUVWXLW", strict=True), [])

    def test_range(self):
        self.assertEqual(tl.volser_range("TAP098", 3), ["TAP098", "TAP099", "TAP100"])
        self.assertEqual(tl.volser_range("A1B009", 2), ["A1B009", "A1B010"])
        self.assertEqual(len(tl.volser_range("000000", 1000)), 1000)

    def test_range_refusals(self):
        for first, n in (("TAP998", 3), ("TAPEAA", 1), ("TAP01", 1), ("TAP001", 0), ("tap001", 1), ("ABCDE9", 2)):
            with self.assertRaises(tl.LabelError, msg=(first, n)):
                tl.volser_range(first, n)


class Stocks(unittest.TestCase):
    def test_builtin_stocks_meet_minimums(self):
        sym = tl.symbol_width_mm("AAAAAAL9")
        for name, s in tl.STOCKS.items():
            tl.check_stock(s)
            self.assertGreaterEqual((s["label_w"] - sym) / 2, tl.QUIET_MIN_MM, name)
            self.assertGreaterEqual(s["label_h"] - tl.TEXT_BAND_MM - 2 * tl.PAD_MM, tl.BAR_MIN_MM, name)

    def test_refusals(self):
        base = dict(tl.STOCKS["letter-2x15"])
        cases = [
            (dict(base, rows=16), "off the page"),
            (dict(base, pitch_x=70.0), "overlap"),
            (dict(base, label_w=72.0, pitch_x=88.0), "quiet zones"),
            (dict(base, label_h=15.0), "shorter than"),
            (dict(base, cols=0), "positive integers"),
            (dict(base, cols=1.5), "positive integers"),
            (dict(base, left=-1), "non-negative"),
            (dict(base, left="1"), "non-negative"),
            ({k: v for k, v in base.items() if k != "top"}, "exactly keys"),
        ]
        for stock, why in cases:
            with self.assertRaisesRegex(tl.LabelError, why):
                tl.check_stock(stock)


class Pdf(unittest.TestCase):
    def labels(self, n):
        return [v + "L9" for v in tl.volser_range("TAP000", n)]

    def contents(self, pdf):
        out = []
        i = 0
        while (i := pdf.find(b"stream\n", i)) != -1:
            j = pdf.index(b"\nendstream", i)
            out.append(zlib.decompress(pdf[i + 7:j]).decode())
            i = j + len(b"\nendstream")
        return out

    def test_structure_and_page_count(self):
        pdf = tl.build_pdf(self.labels(31), tl.STOCKS["letter-2x15"])
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.endswith(b"%%EOF\n"))
        self.assertIn(b"/Count 2", pdf)
        pages = self.contents(pdf)
        self.assertEqual([p.count(" Tj") for p in pages], [30, 1])
        self.assertEqual(pages[0].count(" re"), 30 * 50)

    def test_xref_offsets_point_at_objects(self):
        pdf = tl.build_pdf(self.labels(3), tl.STOCKS["a4-2x16"])
        xref = int(pdf[pdf.rindex(b"startxref") + 10:].split()[0])
        lines = pdf[xref:].split(b"\n")
        n = int(lines[1].split()[1])
        for k in range(1, n):
            off = int(lines[2 + k][:10])
            self.assertTrue(pdf[off:].startswith(b"%d 0 obj" % k), k)

    def test_deterministic(self):
        a = tl.build_pdf(self.labels(5), tl.STOCKS["single"])
        self.assertEqual(a, tl.build_pdf(self.labels(5), tl.STOCKS["single"]))

    def test_skip_moves_first_label(self):
        s = tl.STOCKS["letter-2x15"]
        pdf = tl.build_pdf(self.labels(30), s, skip=29)
        self.assertEqual([p.count(" Tj") for p in self.contents(pdf)], [1, 29])
        with self.assertRaises(tl.LabelError):
            tl.build_pdf(self.labels(1), s, skip=30)
        with self.assertRaises(tl.LabelError):
            tl.build_pdf([], s)


class Cli(unittest.TestCase):
    def test_validate_exit_codes(self):
        rc, out, _ = cli("validate", "TAP001L9", "CLN001CU")
        self.assertEqual(rc, 0)
        self.assertIn(b"ok TAP001L9 (LTO-9)", out)
        rc, _, err = cli("validate", "TAP001L9", "TAP001L4")
        self.assertEqual(rc, 1)
        self.assertIn("unknown media ID", err)
        rc, _, err = cli("validate", "--strict", "O0AAAAL9")
        self.assertEqual(rc, 1)
        rc, _, err = cli("validate", "--strict", "STUVWXLW")
        self.assertEqual(rc, 0)
        self.assertIn("note: STUVWXLW", err)

    def test_pdf_range_to_file_and_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "x.pdf")
            rc, _, err = cli("pdf", "--first", "TAP000", "--count", "4", "--media", "LZ", "-o", out)
            self.assertEqual(rc, 0, err)
            data = Path(out).read_bytes()
        rc, stdout, _ = cli("pdf", "--first", "TAP000", "--count", "4", "--media", "LZ", "-o", "-")
        self.assertEqual((rc, stdout), (0, data))

    def test_pdf_explicit_labels(self):
        rc, out, err = cli("pdf", "AAA001", "AAA002", "--media", "L8", "-o", "-")
        self.assertEqual(rc, 0, err)
        rc2, out2, _ = cli("pdf", "AAA001L8", "AAA002L8", "-o", "-")
        self.assertEqual((rc2, out2), (0, out))

    def test_pdf_refusals(self):
        for args, why in (
            (["--first", "TAP998", "--count", "5", "--media", "L9"], "overflows"),
            (["--first", "TAP000"], "--media"),
            (["TAP001L9", "TAP001L9"], "duplicate"),
            (["TAP001L4"], "unknown media"),
            ([], "give --first"),
        ):
            rc, out, err = cli("pdf", *args, "-o", "-")
            self.assertEqual((rc, out), (2, b""), args)
            self.assertIn(why, err, args)

    def test_stock_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s.json")
            Path(p).write_text(json.dumps(dict(tl.STOCKS["single"], label_w=80, page_w=80, pitch_x=80)))
            rc, out, err = cli("pdf", "TAP001L9", "--stock-json", p, "-o", "-")
            self.assertEqual(rc, 0, err)
            Path(p).write_text("[1]")
            rc, _, err = cli("pdf", "TAP001L9", "--stock-json", p, "-o", "-")
            self.assertEqual(rc, 2)
            Path(p).write_text("{")
            rc, _, err = cli("pdf", "TAP001L9", "--stock-json", p, "-o", "-")
            self.assertEqual(rc, 2)

    def test_spec_json_is_golden(self):
        rc, out, _ = cli("spec-json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), json.loads((ROOT / "tests" / "golden.json").read_text()))


if __name__ == "__main__":
    unittest.main()
