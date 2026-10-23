import datetime as dt
import glob
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "fixtures")
POLICY = os.path.join(FIXTURES, "dagu-lint.toml")
NOW = "2026-10-23T00:00:00Z"
NOW_DT = dt.datetime(2026, 10, 23, tzinfo=dt.timezone.utc)

sys.path.insert(0, ROOT)


def fixture_files(kind):
    return sorted(glob.glob(os.path.join(FIXTURES, kind, "*.yaml")))


def expected_codes(path):
    with open(path, encoding="utf-8") as fh:
        m = re.search(r"^# expect: (.+)$", fh.read(), re.M)
    assert m, f"{path} has no '# expect:' header"
    value = m.group(1).strip()
    return [] if value == "clean" else sorted(c.strip() for c in value.split(","))


@pytest.fixture
def run_cli(capsys):
    from dagu_lint.cli import main

    def run(*argv):
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run
