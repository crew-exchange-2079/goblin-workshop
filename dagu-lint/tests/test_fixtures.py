"""Every fixture declares the exact rule codes it must produce."""

import json
import os

import pytest

from conftest import NOW, POLICY, ROOT, expected_codes, fixture_files

GOOD = fixture_files("good")
BAD = fixture_files("bad")


def test_corpus_size():
    assert len(GOOD) >= 20 and len(BAD) >= 30 and len(GOOD) + len(BAD) >= 50


@pytest.mark.parametrize("path", GOOD + BAD, ids=lambda p: os.path.relpath(p, ROOT))
def test_fixture(path, run_cli):
    code, out, _ = run_cli("-c", POLICY, "--format", "json", "--now", NOW, path)
    findings = json.loads(out)["files"][0]["findings"]
    got = sorted(f["code"] for f in findings)
    assert got == expected_codes(path), [f["message"] for f in findings]
    has_error = any(f["severity"] == "error" for f in findings)
    assert code == (1 if has_error else 0)
    for f in findings:
        assert f["line"] > 0, f"{f['code']} has no line number"


def test_every_rule_has_a_bad_fixture():
    from dagu_lint.rules import RULES

    covered = set()
    for path in BAD:
        covered.update(expected_codes(path))
    missing = set(RULES) - covered - {"DL004"}  # unreadable files are covered in test_cli.py
    assert not missing, f"rules without a bad fixture: {sorted(missing)}"


def test_good_fixtures_together_pass(run_cli):
    code, _, _ = run_cli("-c", POLICY, "--now", NOW, os.path.join(ROOT, "fixtures", "good"))
    assert code == 0
