"""Every fixture under fixtures/bad names, in its first line, exactly the rules it trips.

An optional second line `# podman-version: X.Y` sets the target release.
Fixtures under fixtures/good must produce no findings at all.
"""

import re
from pathlib import Path

import pytest

from helpers import ids

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BAD = sorted((FIXTURES / "bad").iterdir())
GOOD = sorted((FIXTURES / "good").iterdir())


def _header(text, name):
    m = re.search(rf"^# {name}: (.*)$", text, re.M)
    return m.group(1).strip() if m else None


@pytest.mark.parametrize("path", BAD, ids=lambda p: p.name)
def test_bad_fixture(path):
    text = path.read_text()
    expected = sorted(_header(text, "expect").split())
    kind = path.suffix[1:]
    assert ids(text, kind, _header(text, "podman-version")) == expected


@pytest.mark.parametrize("path", GOOD, ids=lambda p: p.name)
def test_good_fixture(path):
    assert ids(path.read_text(), path.suffix[1:]) == []


def test_every_rule_has_a_bad_fixture():
    from quadlet_lint.rules import RULES
    covered = {r for p in BAD for r in _header(p.read_text(), "expect").split()}
    # QL005 needs a key that upstream removed; none has been yet (see test_keydb).
    assert set(RULES) - covered == {"QL005"}


def test_good_fixtures_cover_each_kind():
    assert {p.suffix for p in GOOD} == {".container", ".volume", ".network", ".pod"}
