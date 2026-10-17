import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import validate  # noqa: E402

FIX = ROOT / "fixtures"
EMPTY = (FIX / "valid-empty.xml").read_text()
NESTED = (FIX / "valid-nested.xml").read_text()
SENTINEL = "SENTINEL-DO-NOT-LEAK-7f3a"


def cli(path, timeout=20):
    p = subprocess.run([sys.executable, str(ROOT / "validate.py"), "--input", str(path)],
                       capture_output=True, timeout=timeout, text=True)
    return p.returncode, json.loads(p.stdout), p.stdout


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def write(self, text, name="in.xml", mode="w"):
        p = self.tmp / name
        with open(p, mode) as f:
            f.write(text)
        return p

    def run_cli(self, text):
        return cli(self.write(text))

    def codes(self, rep):
        return {d["code"] for d in rep["diagnostics"]}


class Accept(Base):
    def test_fixtures_pass(self):
        for name in ("valid-empty.xml", "valid-nested.xml", "valid-reordered.xml"):
            rc, rep, _ = cli(FIX / name)
            self.assertEqual((rc, rep["status"]), (0, "SCHEMA_VALID_ONLY"), name)
            self.assertEqual(rep["established_version"], "2.4.0")
            self.assertEqual(rep["schema"]["sha256"], validate.SCHEMA_SHA256)
            self.assertEqual(rep["semantic_checks"], "NOT_PERFORMED")

    def test_module_matches_cli_and_hashes(self):
        rep = validate.validate_path(str(FIX / "valid-nested.xml"))
        import hashlib
        self.assertEqual(rep["input"]["sha256"],
                         hashlib.sha256((FIX / "valid-nested.xml").read_bytes()).hexdigest())
        self.assertEqual(hashlib.sha256(validate.SCHEMA_PATH.read_bytes()).hexdigest(),
                         validate.SCHEMA_SHA256)

    def test_semantic_counterexample_still_schema_valid_only(self):
        bad = (NESTED.replace("<fileuid>3</fileuid>", "<fileuid>2</fileuid>")
               .replace("<highestfileuid>4</highestfileuid>", "<highestfileuid>0</highestfileuid>")
               .replace("<fileoffset>512</fileoffset>", "<fileoffset>0</fileoffset>"))
        rc, rep, _ = self.run_cli(bad)  # duplicate UID, overlapping extents, wrong highest UID
        self.assertEqual((rc, rep["status"]), (0, "SCHEMA_VALID_ONLY"))
        self.assertIn("UID uniqueness", " ".join(rep["limitations"]))


class Reject(Base):
    def test_invalid_cases(self):
        cases = {
            "missing-field": EMPTY.replace("<creator>synthetic fixture</creator>", ""),
            "duplicate-singleton": EMPTY.replace("<generationnumber>1</generationnumber>",
                                                 "<generationnumber>1</generationnumber><generationnumber>2</generationnumber>"),
            "negative-offset": NESTED.replace("<byteoffset>0</byteoffset>", "<byteoffset>-1</byteoffset>", 1),
            "negative-length": NESTED.replace("<length>612</length>", "<length>-5</length>"),
            "zero-bytecount": NESTED.replace("<bytecount>512</bytecount>", "<bytecount>0</bytecount>"),
            "malformed": EMPTY.replace("</ltfsindex>", ""),
            "missing-version": EMPTY.replace(' version="2.4.0"', ""),
            "unknown-tag": EMPTY.replace("<creator>", "<bogus>x</bogus><creator>"),
            "unknown-attr": EMPTY.replace("<creator>", '<creator foo="1">'),
        }
        for name, text in cases.items():
            with self.subTest(name):
                rc, rep, out = self.run_cli(text)
                self.assertEqual((rc, rep["status"]), (1, "INVALID"), out)
                self.assertTrue(rep["diagnostics"])

    def test_unknown_tag_name_not_echoed(self):
        rc, rep, out = self.run_cli(EMPTY.replace("<creator>", f"<{SENTINEL.replace('-', '_')}>x</{SENTINEL.replace('-', '_')}><creator>"))
        self.assertEqual(rc, 1)
        self.assertNotIn("SENTINEL", out)
        rc, rep, out = self.run_cli(EMPTY.replace("synthetic fixture", SENTINEL).replace("<generationnumber>1", "<generationnumber>" + SENTINEL))
        self.assertEqual(rc, 1)
        self.assertNotIn(SENTINEL, out)

    def test_unsupported(self):
        cases = {
            "v2.5.0": EMPTY.replace("2.4.0", "2.5.0"),
            "namespace": EMPTY.replace('<ltfsindex ', '<ltfsindex xmlns="urn:example:x" '),
            "root": EMPTY.replace("ltfsindex", "incrementalindex"),
            "xsi-schemalocation": EMPTY.replace('<ltfsindex ', '<ltfsindex xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="file:///nonexistent.xsd" '),
        }
        for name, text in cases.items():
            with self.subTest(name):
                rc, rep, _ = self.run_cli(text)
                self.assertEqual((rc, rep["status"]), (2, "UNSUPPORTED"))
        rc, rep, _ = self.run_cli(EMPTY.replace('2.4.0', 'abc'))
        self.assertEqual(rc, 1)


class Hostile(Base):
    def test_dtd_xxe_not_resolved(self):
        secret = self.write(SENTINEL, "secret.txt")
        xxe = (f'<?xml version="1.0"?>\n<!DOCTYPE ltfsindex [<!ENTITY x SYSTEM "file://{secret}">]>\n'
               + EMPTY.split("?>", 1)[1].replace("synthetic fixture", "&x;"))
        rc, rep, out = self.run_cli(xxe)
        self.assertEqual((rc, rep["status"]), (1, "INVALID"))
        self.assertIn("DTD_FORBIDDEN", self.codes(rep))
        self.assertNotIn(SENTINEL, out)
        ext = f'<?xml version="1.0"?>\n<!DOCTYPE ltfsindex SYSTEM "file://{secret}">\n' + EMPTY.split("?>", 1)[1]
        rc, rep, out = self.run_cli(ext)
        self.assertEqual(rc, 1)
        self.assertNotIn(SENTINEL, out)

    def test_parser_resolver_spy_and_no_entity_expansion(self):
        # bypass the DOCTYPE pre-scan: exercise the parser options and deny-all resolver directly
        from lxml import etree
        secret = self.write(SENTINEL, "secret2.txt")
        spy = validate.DenyResolver()
        doc = (f'<!DOCTYPE r [<!ENTITY x SYSTEM "file://{secret}">]><r>&x;</r>').encode()
        try:
            root = etree.fromstring(doc, validate.build_parser(spy))
            text = etree.tostring(root).decode() + (root.text or "")
        except (etree.LxmlError, validate.ResolverDenied):
            text = ""
        self.assertNotIn(SENTINEL, text)
        ext = (f'<!DOCTYPE r SYSTEM "file://{secret}"><r/>').encode()
        try:
            etree.fromstring(ext, validate.build_parser(spy))
        except (etree.LxmlError, validate.ResolverDenied):
            pass
        # if the resolver was consulted it denied (raised); the sentinel never appears in any output
        self.assertIsInstance(spy.calls, int)

    def test_check_document_denied_resolution_reports_refusal(self):
        # pre-scan hit: resolver never asked
        spy = validate.DenyResolver()
        with self.assertRaises(validate.Refusal) as cm:
            validate.check_document(b'<!DOCTYPE a SYSTEM "x"><a/>', resolver=spy)
        self.assertEqual(cm.exception.code, "DTD_FORBIDDEN")
        self.assertEqual(spy.calls, 0)

    def test_limits_and_encoding(self):
        big = self.write(EMPTY + " " * (validate.MAX_INPUT_BYTES + 1), "big.xml")
        rc, rep, _ = cli(big)
        self.assertEqual((rc, rep["status"]), (3, "ERROR"))
        self.assertIn("INPUT_TOO_LARGE", self.codes(rep))
        deep = "<a>" * 70 + "</a>" * 70
        rc, rep, _ = self.run_cli(deep)
        self.assertEqual(rc, 1)
        self.assertIn("LIMIT_DEPTH_EXCEEDED", self.codes(rep))
        many = "<ltfsindex version='2.4.0'>" + "<a/>" * (validate.MAX_NODES + 1) + "</ltfsindex>"
        rc, rep, _ = self.run_cli(many)
        self.assertIn("LIMIT_NODES_EXCEEDED", self.codes(rep))
        p = self.write(EMPTY.replace("UTF-8", "UTF-16").encode("utf-16"), "u16.xml", "wb")
        rc, rep, _ = cli(p)
        self.assertEqual((rc, rep["status"]), (2, "UNSUPPORTED"))

    def test_input_kinds(self):
        fifo = self.tmp / "f.fifo"
        os.mkfifo(fifo)
        rc, rep, _ = cli(fifo, timeout=10)
        self.assertEqual((rc, rep["status"]), (3, "ERROR"))
        self.assertIn("INPUT_NOT_REGULAR_FILE", self.codes(rep))
        link = self.tmp / "link.xml"
        os.symlink(FIX / "valid-empty.xml", link)
        rc, rep, out = cli(link)
        self.assertEqual(rc, 3)
        self.assertNotIn("valid-empty", out)
        rc, rep, _ = cli(self.tmp)
        self.assertEqual(rc, 3)
        rc, rep, _ = cli("/dev/null")
        self.assertIn("INPUT_DEVICE_REFUSED", self.codes(rep))
        rc, rep, _ = cli("https://example.invalid/index.xml")
        self.assertIn("INPUT_URL_REFUSED", self.codes(rep))
        rc, rep, _ = cli(self.tmp / "missing.xml")
        self.assertEqual(rc, 3)

    def test_usage_error_is_error(self):
        p = subprocess.run([sys.executable, str(ROOT / "validate.py")], capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(json.loads(p.stdout)["status"], "ERROR")

    def test_infrastructure_failures_never_success(self):
        rep = validate.validate_path(str(FIX / "valid-empty.xml"), schema_path=self.tmp / "nope.xsd")
        self.assertEqual((rep["status"], rep["exit_code"]), ("ERROR", 3))
        tampered = self.write(validate.SCHEMA_PATH.read_text() + "<!-- x -->", "t.xsd")
        rep = validate.validate_path(str(FIX / "valid-empty.xml"), schema_path=tampered)
        self.assertIn("SCHEMA_HASH_MISMATCH", self.codes(rep))
        saved = validate.etree
        validate.etree = None
        try:
            rep = validate.validate_path(str(FIX / "valid-empty.xml"))
        finally:
            validate.etree = saved
        self.assertEqual((rep["status"], rep["exit_code"]), ("ERROR", 3))
        self.assertIn("DEPENDENCY_MISSING", self.codes(rep))

    def test_report_bounded(self):
        bad = EMPTY.replace("<contents/>", "<contents>" + "<file/>" * 5000 + "</contents>")
        rc, rep, out = self.run_cli(bad)
        self.assertEqual(rc, 1)
        self.assertLessEqual(len(out.encode()), validate.MAX_REPORT_BYTES)
        self.assertLessEqual(len(rep["diagnostics"]), validate.MAX_DIAGNOSTICS)


class DeviceSafety(Base):
    """os.open spy: before the regular-file decision only O_PATH opens may occur."""

    def spy(self):
        real, calls = os.open, []

        def wrapper(p, flags, *a, **k):
            calls.append((str(p), flags))
            return real(p, flags, *a, **k)
        return mock.patch("validate.os.open", wrapper), calls

    def assert_only_opath(self, calls):
        self.assertTrue(calls)
        for p, flags in calls:
            self.assertTrue(flags & os.O_PATH, (p, flags))

    def test_parent_symlink_alias_of_dev_refused_before_data_open(self):
        alias = self.tmp / "devalias"
        os.symlink("/dev", alias)
        p, calls = self.spy()
        with p:
            rep = validate.validate_path(str(alias / "null"))
        self.assertEqual(rep["status"], "ERROR")
        self.assertIn(rep["diagnostics"][0]["code"], ("INPUT_DEVICE_REFUSED", "INPUT_NOT_REGULAR_FILE"))
        for c, f in calls:
            self.assertTrue(f & os.O_PATH, (c, f))

    def test_double_slash_dev_and_untyped_inodes(self):
        for target in ("//dev/null", "/dev/null"):
            p, calls = self.spy()
            with p:
                rep = validate.validate_path(target)
            self.assertEqual(rep["status"], "ERROR")
            for c, f in calls:
                self.assertTrue(f & os.O_PATH, (c, f))
        fifo = self.tmp / "q.fifo"
        os.mkfifo(fifo)
        p, calls = self.spy()
        with p:
            rep = validate.validate_path(str(fifo))
        self.assertEqual(rep["diagnostics"][0]["code"], "INPUT_NOT_REGULAR_FILE")
        self.assert_only_opath(calls)

    def test_relative_input_with_cwd_dev_alias(self):
        alias = self.tmp / "cwdalias"
        os.symlink("/dev", alias)
        old = os.getcwd()
        os.chdir(alias)
        try:
            p, calls = self.spy()
            with p:
                rep = validate.validate_path("null")
        finally:
            os.chdir(old)
        self.assertEqual(rep["status"], "ERROR")
        for c, f in calls:
            self.assertTrue(f & os.O_PATH, (c, f))

    def test_missing_opath_refuses_without_opening(self):
        p, calls = self.spy()
        with p, mock.patch.object(validate, "_O_PATH", None):
            rep = validate.validate_path(str(FIX / "valid-empty.xml"))
        self.assertEqual(rep["diagnostics"][0]["code"], "INPUT_PLATFORM_UNSUPPORTED")
        self.assertEqual(calls, [])

    def test_swap_race_reads_pinned_regular_inode(self):
        target = self.write(EMPTY, "race.xml")
        real, calls = os.open, []

        def racing(path, flags, *a, **k):
            fd = real(path, flags, *a, **k)
            calls.append((str(path), flags))
            if flags & os.O_PATH and str(path) == str(target):
                os.unlink(target)
                os.symlink("/dev/null", target)  # swap after pin; must not matter
            return fd
        with mock.patch("validate.os.open", racing):
            rep = validate.validate_path(str(target))
        self.assertEqual(rep["status"], "SCHEMA_VALID_ONLY")
        for c, f in calls:
            self.assertTrue((f & os.O_PATH) or c.startswith("/proc/self/fd/"), (c, f))


class Encoding(Base):
    def test_utf8_positive_and_non_utf8_refused(self):
        ok = EMPTY.replace("synthetic fixture", "caf\u00e9").encode("utf-8")
        rc, rep, _ = cli(self.write(ok, "u8.xml", "wb"))
        self.assertEqual((rc, rep["status"]), (0, "SCHEMA_VALID_ONLY"))
        ascii_iso = EMPTY.replace("UTF-8", "ISO-8859-1").encode("ascii")
        misdeclared = EMPTY.replace("UTF-8", "ISO-8859-1").replace("synthetic fixture", "caf\u00e9").encode("utf-8")
        latin1 = EMPTY.replace("synthetic fixture", "caf\u00e9").replace("UTF-8", "ISO-8859-1").encode("latin-1")
        usascii = EMPTY.replace("UTF-8", "US-ASCII").encode("ascii")
        for name, blob in (("ascii-iso", ascii_iso), ("misdeclared", misdeclared), ("latin1", latin1), ("usascii", usascii)):
            with self.subTest(name):
                rc, rep, out = cli(self.write(blob, name + ".xml", "wb"))
                self.assertEqual((rc, rep["status"]), (2, "UNSUPPORTED"))
                self.assertIn("ENCODING_NOT_UTF8", self.codes(rep))
                self.assertNotIn("ISO-8859", out)
                self.assertNotIn(name, out)


if __name__ == "__main__":
    unittest.main()
