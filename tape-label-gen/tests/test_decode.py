"""Round trip: build a PDF, rasterise it with PDFium, read the bars back with ZXing.

Needs requirements-test.txt (pypdfium2, zxing-cpp). Skips without them unless
TAPELABEL_REQUIRE_DECODE=1 (set in CI), in which case a missing package fails:
a skipped decode never counts as a pass there.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import tapelabel as tl  # noqa: E402

REQUIRE = os.environ.get("TAPELABEL_REQUIRE_DECODE") == "1"
try:
    import pypdfium2 as pdfium
    import zxingcpp
except ImportError:
    if REQUIRE:
        raise
    pdfium = zxingcpp = None

# Six volsers covering all 36 characters, across every media ID.
ALL_CHARS = ["ABCDEF", "GHIJKL", "MNOPQR", "STUVWX", "YZ0123", "456789"]


def render(pdf, dpi):
    doc = pdfium.PdfDocument(pdf)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        bm = page.render(scale=dpi / 72, grayscale=True)
        out.append((page, bm))
    return doc, out


def decode(bm):
    view = zxingcpp.ImageView(memoryview(bm.buffer), bm.width, bm.height, zxingcpp.ImageFormat.Lum, bm.stride)
    return zxingcpp.read_barcodes(view, formats=zxingcpp.BarcodeFormat.Code39)


def centre(r):
    p = r.position
    pts = (p.top_left, p.top_right, p.bottom_left, p.bottom_right)
    return sum(q.x for q in pts) / 4, sum(q.y for q in pts) / 4


@unittest.skipIf(zxingcpp is None, "pypdfium2/zxing-cpp not installed (pip install -r requirements-test.txt)")
class Decode(unittest.TestCase):
    def roundtrip(self, labels, stock, dpi=300, **kw):
        s = tl.STOCKS[stock] if isinstance(stock, str) else stock
        pdf = tl.build_pdf(labels, s, **kw)
        doc, pages = render(pdf, dpi)
        px = dpi / 25.4
        per = s["cols"] * s["rows"]
        slots = [None] * kw.get("skip", 0) + list(labels)
        self.assertEqual(len(pages), -(-len(slots) // per))
        for n, (page, bm) in enumerate(pages):
            want = {}
            for i, lab in enumerate(slots[n * per:(n + 1) * per]):
                if lab is not None:
                    want[lab] = divmod(i, s["cols"])
            got = decode(bm)
            self.assertEqual(sorted(r.text for r in got), sorted(want), f"page {n}")
            for r in got:
                # Plain Code 39, all 8 characters transmitted. ZXing reports ]A1
                # (check character validated and transmitted) exactly when the
                # last character happens to be the mod-43 check of the rest.
                aim = "]A1" if tl.mod43(r.text[:7]) == r.text[7] else "]A0"
                self.assertEqual(r.symbology_identifier, aim, r.text)
                row, col = want[r.text]
                x, y = centre(r)
                left = s["left"] + col * s["pitch_x"] + kw.get("offset_x", 0)
                top = s["top"] + row * s["pitch_y"] + kw.get("offset_y", 0)
                self.assertTrue(left * px < x < (left + s["label_w"]) * px, r.text)
                self.assertTrue(top * px < y < (top + s["label_h"]) * px, r.text)
            # The human-readable line says the same thing as the bars.
            text = page.get_textpage().get_text_range()
            self.assertEqual(sorted(text.split()), sorted(want), f"page {n}")
        doc.close()

    def test_all_characters_and_media(self):
        media = [m for m in tl.MEDIA if m != "CU"]
        labels = [v + m for v in ALL_CHARS for m in media] + ["CLN001CU", "CLN999CU"]
        self.roundtrip(labels, "letter-2x15")

    def test_every_stock(self):
        for name in tl.STOCKS:
            with self.subTest(name):
                self.roundtrip([v + "L9" for v in tl.volser_range("TAP000", 33)], name)

    def test_skip_and_offset(self):
        self.roundtrip([v + "L7" for v in tl.volser_range("BK0100", 20)], "a4-2x16",
                       skip=25, offset_x=1.5, offset_y=-2.0)

    def test_outline_does_not_break_decode(self):
        self.roundtrip([v + "LZ" for v in tl.volser_range("WRM000", 4)], "letter-2x15", outline=True)

    def test_decodes_at_150_dpi(self):
        self.roundtrip(["ABCDEFL9", "YZ0123LZ", "456789L5"], "letter-2x15", dpi=150)

    def test_printed_element_widths(self):
        # Decoding proves the pattern, not the size: measure the bars at 1200 dpi
        # across the middle of a single label and compare with X and 2.75X.
        label = "MNOPQRL6"
        s = tl.STOCKS["single"]
        dpi = 1200
        px = dpi / 25.4
        doc, [(page, bm)] = render(tl.build_pdf([label], s), dpi)
        y = int((tl.PAD_MM + tl.TEXT_BAND_MM + (s["label_h"] - tl.TEXT_BAND_MM - 2 * tl.PAD_MM) / 2) * px)
        row = bytes(memoryview(bm.buffer)[y * bm.stride:y * bm.stride + bm.width])
        runs, cur, n = [], row[0] < 128, 0
        for v in row:
            if (v < 128) == cur:
                n += 1
            else:
                runs.append((cur, n))
                cur, n = v < 128, 1
        runs.append((cur, n))
        self.assertFalse(runs[0][0])
        self.assertFalse(runs[-1][0])
        quiet_l, quiet_r = runs[0][1], runs[-1][1]
        elements = [n for _, n in runs[1:-1]]
        expect = tl.modules(label)
        self.assertEqual(len(elements), len(expect))
        for got, want in zip(elements, expect):
            self.assertLessEqual(abs(got - want * tl.NARROW_MM * px), 1.5)
        self.assertLessEqual(abs(sum(elements) - tl.symbol_width_mm(label) * px), 2)
        self.assertGreaterEqual(min(quiet_l, quiet_r), tl.QUIET_MIN_MM * px)
        doc.close()


if __name__ == "__main__":
    unittest.main()
