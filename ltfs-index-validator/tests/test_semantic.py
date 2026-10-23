"""Rule-by-rule boundary tests for semantic.py, plus oracle properties."""
import base64
import calendar
import copy
import re
import sys
import unittest
import urllib.parse
from pathlib import Path

from hypothesis import given, strategies as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import semantic  # noqa: E402
import validate  # noqa: E402
import indexgen as g  # noqa: E402
import test_properties  # noqa: E402,F401  (registers and loads the hypothesis profile)


def run(idx, **opts):
    return validate.validate_bytes(g.to_xml(idx), options=validate.Options(**opts))


def diag(rep):
    return {(d["code"], d["severity"]) for d in rep["diagnostics"]}


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.idx = g.sample_index()  # location b/20, back pointer b/15, extents b/10-13, a/6

    def assertClean(self, idx, **opts):
        rep = run(idx, **opts)
        self.assertEqual((rep["status"], rep["diagnostics"]), ("VALID", []))

    def assertFlags(self, idx, code, severity="error", **opts):
        rep = run(idx, **opts)
        self.assertIn((code, severity), diag(rep))
        if severity == "error":
            self.assertEqual(rep["status"], "INVALID")
        return rep

    def test_sample_is_clean(self):
        self.assertClean(self.idx)

    def test_backptr_gap(self):
        for back, code in ((17, None), (18, "BACKPTR_NOT_BEFORE_SELF"), (19, "BACKPTR_NOT_BEFORE_SELF"),
                           (21, "BACKPTR_NOT_BEFORE_SELF")):
            idx = copy.deepcopy(self.idx)
            idx.backptr = ("b", back)
            with self.subTest(back):
                self.assertClean(idx) if code is None else self.assertFlags(idx, code)
        self.idx.backptr = ("b", 20)
        self.assertFlags(self.idx, "BACKPTR_IS_SELF", "warning")

    def test_label_area(self):
        self.idx.backptr = ("a", 5)
        self.assertClean(self.idx)
        self.idx.backptr = ("a", 4)
        self.assertFlags(self.idx, "BACKPTR_IN_LABEL_AREA")
        idx = g.sample_index()
        idx.root.children[2].extents[0].startblock = 4
        self.assertClean(idx)
        idx.root.children[2].extents[0].startblock = 3
        self.assertFlags(idx, "EXTENT_IN_LABEL_AREA")

    def test_first_index_needs_no_backptr(self):
        idx = g.Index(g.Entry("", 1, is_dir=True), location=("a", 5), backptr=None)
        self.assertClean(idx)
        idx.location = ("a", 6)
        self.assertFlags(idx, "BACKPTR_MISSING", "warning")

    def test_extent_at_index_blocks(self):
        x = self.idx.root.children[0].children[0].extents[1]  # b/11
        x.startblock = 19
        self.assertClean(self.idx)
        x.startblock = 20
        self.assertFlags(self.idx, "EXTENT_AFTER_INDEX")
        x.startblock = 15
        self.assertFlags(self.idx, "EXTENT_COVERS_INDEX")

    def test_extent_length_and_overlap_edges(self):
        a = self.idx.root.children[0].children[0]  # length 612: [0,512) [512,612)
        a.length = 611
        self.assertFlags(self.idx, "EXTENT_BEYOND_LENGTH")
        a.length = 612
        a.extents[1].fileoffset = 511
        self.assertFlags(self.idx, "EXTENT_OVERLAP")
        a.extents[1].fileoffset = 600       # gap = sparse, fine; contained overlap below
        a.length = 700
        self.assertClean(self.idx)
        a.extents.append(g.Extent("b", 12, 100, 10, 50))
        self.assertFlags(self.idx, "EXTENT_OVERLAP")

    def test_unsorted_extents_warn_only(self):
        a = self.idx.root.children[0].children[0]
        a.extents.reverse()
        rep = self.assertFlags(self.idx, "EXTENT_UNSORTED", "warning")
        self.assertEqual(rep["status"], "VALID")
        self.assertEqual(run(self.idx, strict=True)["status"], "INVALID")

    def test_blocksize_checks(self):
        self.assertClean(self.idx, blocksize=4096)
        a = self.idx.root.children[0].children[0]  # a.bin: b/10 512 bytes, b/11 100 bytes
        a.extents[1].byteoffset = 4096
        self.assertFlags(self.idx, "EXTENT_BYTEOFFSET_EXCEEDS_BLOCK", blocksize=4096)
        idx = g.sample_index()
        a = idx.root.children[0].children[0]
        a.extents[0].bytecount, a.extents[1].fileoffset, a.length = 4097, 4097, 4197
        self.assertFlags(idx, "EXTENT_TAPE_SHARED", "warning", blocksize=4096)  # spills into b/11
        self.assertClean(idx, blocksize=8192)
        idx.root.children[0].children[1].extents[1].bytecount = 3 * 4096  # b/13..15 covers back ptr
        self.assertFlags(idx, "EXTENT_COVERS_INDEX", blocksize=4096)

    def test_volume_partition_facts(self):
        self.assertClean(self.idx, partitions=("a", "b"))
        self.assertFlags(self.idx, "BACKPTR_NOT_DATA_PARTITION", partitions=("b", "a"))
        self.assertFlags(self.idx, "LOCATION_PARTITION_UNKNOWN", partitions=("a", "c"))
        self.assertFlags(self.idx, "EXTENT_PARTITION_UNKNOWN", partitions=("c", "b"))

    def test_uids(self):
        self.idx.highestfileuid = 7
        self.assertClean(self.idx)
        self.idx.highestfileuid = 6
        self.assertFlags(self.idx, "HIGHESTFILEUID_TOO_LOW")
        idx = g.sample_index()
        idx.root.uid = 0
        self.assertFlags(idx, "ROOT_UID_NOT_ONE")
        idx = g.sample_index()
        idx.root.children[1].uid = 1
        self.assertFlags(idx, "UID_DUPLICATE")

    def test_name_lengths(self):
        e = self.idx.root.children[1]
        for name, ok in (("x" * 255, True), ("x" * 256, False), ("\U0001F600" * 255, True),
                         ("é" * 200, True)):  # NFC: 200 code points, raw 400
            e.name = name
            with self.subTest(len(name)):
                rep = run(self.idx)
                self.assertEqual(("NAME_TOO_LONG", "error") in diag(rep), not ok)
        self.idx.root.name = "x" * 256
        self.assertFlags(self.idx, "NAME_TOO_LONG")

    def test_names(self):
        e = self.idx.root.children[1]
        cases = [("a%2Fb", True, "NAME_FORBIDDEN_CHAR"), ("%00", True, "NAME_FORBIDDEN_CHAR"),
                 ("%ef%bf%bf", True, "NAME_FORBIDDEN_CHAR"), ("%41%4", True, "NAME_PERCENT_ENCODING_MALFORMED"),
                 ("%e9", True, "NAME_NOT_UTF8"), ("%ED%B0%80", True, "NAME_NOT_UTF8"),
                 ("..", False, "NAME_DOT_ENTRY"), ("SUB", False, "NAME_CASE_COLLISION"),
                 ("sub", False, "NAME_DUPLICATE")]
        for name, raw, code in cases:
            with self.subTest(name):
                e.name, e.raw_name = name, raw
                self.assertIn(code, {c for c, _ in diag(run(self.idx))})
        e.name, e.raw_name = "%41%c3%a9%20", True  # "Aé " is fine
        self.assertClean(self.idx)

    def test_symlink_target_may_contain_slash_not_nul(self):
        link = self.idx.root.children[1]
        link.symlink = "/abs/" + "x" * 300
        self.assertClean(self.idx)
        link.symlink = ""
        self.assertFlags(self.idx, "NAME_EMPTY")

    def test_root_name_may_be_empty(self):
        self.idx.root.name = ""
        self.assertClean(self.idx)

    def test_xattrs(self):
        x = self.idx.root.xattrs
        x.append(g.Xattr("a", "a" * 4096))
        x.append(g.Xattr("b", base64.b64encode(b"\0" * 4096).decode(), "base64"))
        x.append(g.Xattr("c", "AAEC\n /w==\t", "base64"))  # XML whitespace inside base64 is fine
        x.append(g.Xattr("d", ""))
        self.assertClean(self.idx)
        x.append(g.Xattr("e", base64.b64encode(b"\0" * 4097).decode(), "base64"))
        self.assertFlags(self.idx, "XATTR_VALUE_LARGE", "warning")
        x.append(g.Xattr("f", "AAE=AAE=", "base64"))
        self.assertFlags(self.idx, "XATTR_BASE64_INVALID")

    def test_comment_and_policy(self):
        self.idx.comment = "é" * 32768  # exactly 65536 UTF-8 bytes
        self.assertClean(self.idx)
        self.idx.comment += "x"
        self.assertFlags(self.idx, "COMMENT_TOO_LONG")
        idx = g.sample_index()
        idx.policy = ["ok", "a/b"]
        self.assertFlags(idx, "NAME_FORBIDDEN_CHAR")

    def test_datetimes(self):
        for t, ok in (("2024-02-29T23:59:60.000000000Z", True), ("2025-02-29T00:00:00.000000000Z", False),
                      ("2000-02-29T00:00:00.000000000Z", True), ("1900-02-29T00:00:00.000000000Z", False),
                      ("0000-01-01T00:00:00.000000000Z", True), ("2026-04-31T00:00:00.000000000Z", False)):
            with self.subTest(t):
                self.idx.updatetime = t
                rep = run(self.idx)
                self.assertEqual(rep["status"] == "VALID", ok, rep["diagnostics"])

    def test_partition_count(self):
        self.idx.backptr = ("c", 5)
        self.assertFlags(self.idx, "PARTITION_COUNT_EXCEEDED")

    def test_errata_schema_vs_annex_b(self):
        rep = run(self.idx, schema_variant="annex-b")
        self.assertEqual((rep["status"], rep["semantic_checks"]), ("INVALID", "SKIPPED_SCHEMA_INVALID"))
        for x in self.idx.entries():
            x.xattrs = [g.Xattr(a.key, "") for a in x.xattrs]
        rep = run(self.idx, schema_variant="annex-b")
        self.assertEqual(rep["status"], "VALID")
        self.assertEqual(rep["schema"]["pinned_sha256"], validate.SCHEMAS["annex-b"][1])

    def test_diagnostics_truncated_but_counted(self):
        root = g.Entry("", 1, is_dir=True, children=[
            g.Entry("f%d" % i, 0, length=0, extents=[]) for i in range(300)])
        idx = g.Index(root, location=("a", 5), backptr=None, highestfileuid=1)
        rep = run(idx)
        self.assertEqual(rep["counts"]["errors"], 300 + 299)  # UID_ZERO each, UID_DUPLICATE after first
        self.assertTrue(rep["diagnostics_truncated"])
        self.assertEqual(len(rep["diagnostics"]), validate.MAX_DIAGNOSTICS)


def _oracle_datetime(y, mo, d, h, mi, s):
    if not (1 <= mo <= 12 and h <= 23 and mi <= 59 and s <= 60):
        return False
    leap = calendar.isleap(y) if y > 0 else True  # year 0000 is a proleptic leap year
    days = calendar.monthrange(2000 if leap else 2001, mo)[1]
    return 1 <= d <= days


class Oracles(unittest.TestCase):
    @given(st.integers(0, 9999), st.integers(0, 99), st.integers(0, 99), st.integers(0, 99),
           st.integers(0, 99), st.integers(0, 99))
    def test_datetime_matches_calendar(self, y, mo, d, h, mi, s):
        text = "%04d-%02d-%02dT%02d:%02d:%02d.000000000Z" % (y, mo, d, h, mi, s)
        self.assertEqual(semantic._valid_datetime(text), _oracle_datetime(y, mo, d, h, mi, s))

    @given(st.text(st.sampled_from("%4142aFgG\u00e9/ "), max_size=12))
    def test_percent_decoding_matches_urllib(self, raw):
        from lxml import etree
        el = etree.Element("name", percentencoded="true")
        el.text = raw
        name, problem = semantic.decode_name(el)
        malformed = re.fullmatch(r"(?:[^%]|%[0-9A-Fa-f]{2})*", raw) is None
        if malformed:
            self.assertEqual(problem, "NAME_PERCENT_ENCODING_MALFORMED")
            return
        try:
            expected = urllib.parse.unquote_to_bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            self.assertEqual(problem, "NAME_NOT_UTF8")
            return
        self.assertEqual((name, problem), (expected, None))


if __name__ == "__main__":
    unittest.main()
