"""Parser regression tests: leading BOM (G1) and whitespace in section headers (G2).

G1: a file starting with a UTF-8 BOM parses as if the BOM were not there;
systemd's own parser skips a leading BOM (from memory, UNVERIFIED - no network
in this lane). A BOM anywhere else is NOT stripped.

G2: whitespace inside the brackets (`[ Container ]`) is not trimmed by
systemd, so the linter must report it as a malformed section header with its
line number instead of silently dropping the header and its keys.

The fixture sweep mirrors tests/test_fixtures.py as unittest so the corpus is
covered without pytest: good fixtures lint clean, bad fixtures produce the
rule ids taken from the leading QLxxx segments of their file names.
"""

import re
import unittest
from pathlib import Path

from helpers import ids
from quadlet_lint.unitfile import parse

BOM = "\ufeff"

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOOD = sorted((FIXTURES / "good").iterdir())
BAD = sorted((FIXTURES / "bad").iterdir())


def _header(text, name):
    m = re.search(rf"^# {name}: (.*)$", text, re.M)
    return m.group(1).strip() if m else None


def _expected_ids(path):
    out = []
    for part in path.stem.split("-"):
        if re.fullmatch(r"QL\d{3}", part):
            out.append(part)
        else:
            break
    return sorted(out)


def _required_ids(path, text):
    header = _header(text, "expect")
    if not header:
        return _expected_ids(path)
    return sorted(set(_expected_ids(path)) | set(header.split()))


class LeadingBomTests(unittest.TestCase):
    def test_bom_file_parses_with_section_and_keys(self):
        u = parse(BOM + "[Container]\nImage=quay.io/a/b\n")
        self.assertEqual(u.errors, [])
        self.assertEqual(u.sections, {"Container": 1})
        self.assertEqual(u.get("Container", "Image"), "quay.io/a/b")
        self.assertEqual(u.last("Container", "Image").line, 2)

    def test_bom_before_comment_keeps_line_numbers(self):
        u = parse(BOM + "# c\n[Container]\nImage=x\n")
        self.assertEqual(u.errors, [])
        self.assertEqual(u.sections, {"Container": 2})
        self.assertEqual(u.last("Container", "Image").line, 3)


class BomScopeTests(unittest.TestCase):
    def test_bom_in_the_middle_is_not_stripped(self):
        u = parse("[Container]\n" + BOM + "Image=x\n")
        self.assertEqual(u.entries, [])
        self.assertEqual(len(u.errors), 1)
        self.assertEqual(u.errors[0][0], 2)


class SectionHeaderTests(unittest.TestCase):
    def test_whitespace_inside_brackets_is_reported_on_line_1(self):
        u = parse("[ Container ]\nImage=quay.io/a/b\n")
        self.assertEqual([line for line, _ in u.errors], [1, 2])
        self.assertIn("[ Container ]", u.errors[0][1])
        self.assertNotIn("Container", u.sections)
        self.assertEqual(u.entries, [])

    def test_tab_inside_brackets_is_reported_too(self):
        u = parse("[\tContainer\t]\nImage=x\n")
        self.assertEqual(u.errors[0][0], 1)
        self.assertNotIn("Container", u.sections)

    def test_plain_header_unchanged(self):
        u = parse("[Container]\nImage=x\n")
        self.assertEqual(u.errors, [])
        self.assertEqual(u.sections, {"Container": 1})
        self.assertEqual(u.get("Container", "Image"), "x")

    def test_broken_header_still_reported(self):
        u = parse("[Container\nImage=x\n")
        self.assertEqual(u.errors[0][0], 1)


class FixtureSweepTests(unittest.TestCase):
    def test_good_fixtures_lint_clean(self):
        for path in GOOD:
            with self.subTest(fixture=path.name):
                self.assertEqual(ids(path.read_text(), path.suffix[1:]), [])

    def test_bad_fixtures_produce_expected_ids(self):
        for path in BAD:
            with self.subTest(fixture=path.name):
                text = path.read_text()
                version = _header(text, "podman-version")
                self.assertEqual(ids(text, path.suffix[1:], version), _required_ids(path, text))


if __name__ == "__main__":
    unittest.main()
