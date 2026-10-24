"""Golden tests: every directory under golden/ is one timer + service pair.

Inputs: one *.timer, one *.service, and optionally options.json (keyword
arguments for convert()). Expected outputs: expected.yaml, expected.report and,
when the service sets Environment=, expected.env.

Regenerate after an intended change with
    TIMER2DAGU_REGEN=1 python -m pytest -q tests/test_golden.py
and review the diff before committing it.
"""

import glob
import json
import os

import pytest

import timer2dagu

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
CASES = sorted(os.path.basename(d) for d in glob.glob(os.path.join(GOLDEN, "*")) if os.path.isdir(d))
REGEN = os.environ.get("TIMER2DAGU_REGEN") == "1"


def _one(case, pattern):
    found = glob.glob(os.path.join(GOLDEN, case, pattern))
    assert len(found) == 1, "%s: expected exactly one %s" % (case, pattern)
    return found[0]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _check(path, actual):
    if REGEN:
        if actual is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(actual)
        return
    if actual is None:
        assert not os.path.exists(path), "%s exists but no output was produced" % path
    else:
        assert os.path.exists(path), "missing %s (regenerate with TIMER2DAGU_REGEN=1)" % path
        assert actual == _read(path)


def test_corpus_is_large_enough():
    assert len(CASES) >= 40


@pytest.mark.parametrize("case", CASES)
def test_golden(case):
    timer = _one(case, "*.timer")
    service = _one(case, "*.service")
    opts_path = os.path.join(GOLDEN, case, "options.json")
    opts = json.loads(_read(opts_path)) if os.path.exists(opts_path) else {}
    timer_name = os.path.basename(timer)
    yaml_text, env_text, report_text, _ = timer2dagu.convert(
        _read(timer), timer_name, _read(service), os.path.basename(service),
        env_file=timer_name.rsplit(".", 1)[0] + ".env", **opts)
    _check(os.path.join(GOLDEN, case, "expected.yaml"), yaml_text)
    _check(os.path.join(GOLDEN, case, "expected.env"), env_text)
    _check(os.path.join(GOLDEN, case, "expected.report"), report_text)


@pytest.mark.parametrize("case", CASES)
def test_golden_yaml_parses(case):
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(_read(os.path.join(GOLDEN, case, "expected.yaml")))
    assert isinstance(doc, dict) and doc["steps"]
