"""Every tape-exporter parser against every sg_logs fixture in manifest.json.

Runs from this directory (``python -m pytest -q test_corpus.py``); the
tape-exporter package is imported from the repository checkout. Expected
results come from the manifest, which generate.py derives from the page
specification, not from these parsers.
"""

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tape-exporter"))

from lto_exporter import parsers  # noqa: E402

MANIFEST = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
FIXTURES = MANIFEST["fixtures"]
OUTPUTS = [(fx, out) for fx in FIXTURES for out in fx["outputs"]]

PARSERS = {
    "error_counters.write": lambda text: parsers.parse_sg_logs_error_counters(text, "write"),
    "error_counters.read": lambda text: parsers.parse_sg_logs_error_counters(text, "read"),
    "sequential_access": parsers.parse_sg_logs_sequential_access,
}
# Each public parse_* function in lto_exporter.parsers and the PARSERS
# entries that drive it. A new parser fails test_every_parser_is_covered
# until it is added here and to generate.py.
COVERS = {
    "parse_sg_logs_error_counters": {"error_counters.write", "error_counters.read"},
    "parse_sg_logs_sequential_access": {"sequential_access"},
}
REQUIRED_PAGES = {"0x02", "0x03", "0x0c", "0x2e", "0x31", "0x17"}
OLD, NEWER = "1.46", {v for v in MANIFEST["sg_logs_versions"] if v != "1.46"}


def _id(pair):
    return pair[1]["file"]


def _text(out):
    return (HERE / out["file"]).read_text(encoding="utf-8")


def test_every_parser_is_covered():
    public = {name for name, obj in inspect.getmembers(parsers, inspect.isfunction)
              if name.startswith("parse_") and obj.__module__ == parsers.__name__}
    assert public == set(COVERS)
    assert set().union(*COVERS.values()) == set(PARSERS) == set(MANIFEST["parsers"])


def test_corpus_shape():
    pages = {fx["page"] for fx in FIXTURES}
    assert REQUIRED_PAGES <= pages
    assert NEWER, "need at least one sg3_utils release newer than 1.46"
    for fx in FIXTURES:
        versions = {o["sg3_utils"] for o in fx["outputs"]}
        assert versions == {OLD} | NEWER, fx["id"]
        # TapeAlert is the read-may-clear page, and the only one here.
        assert fx["read_may_clear"] == (fx["page"] == "0x2e"), fx["id"]
        assert (HERE / fx["page_hex"]).is_file()


def test_no_unlisted_files():
    listed = {out["file"] for _, out in OUTPUTS} | {fx["page_hex"] for fx in FIXTURES}
    on_disk = {str(p.relative_to(HERE)) for pattern in ("sg3_utils-*/*", "pages/*")
               for p in HERE.glob(pattern)}
    assert on_disk == listed


@pytest.mark.parametrize("pair", OUTPUTS, ids=_id)
def test_fixture_is_intact(pair):
    _, out = pair
    data = (HERE / out["file"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == out["sha256"]
    assert out["exit_status"] == 0 and out["stderr"] == ""


@pytest.mark.parametrize("pair", OUTPUTS, ids=_id)
@pytest.mark.parametrize("parser", sorted(PARSERS))
def test_parser_handles_fixture(pair, parser):
    _, out = pair
    result = PARSERS[parser](_text(out))
    want = out["expect"][parser]
    assert (result.status, result.values) == (want["status"], want["values"])


@pytest.mark.parametrize("pair", OUTPUTS, ids=_id)
@pytest.mark.parametrize("parser", sorted(PARSERS))
def test_parser_survives_truncation(pair, parser):
    # Every prefix must give a well-formed result without raising. A prefix
    # ending on a line boundary must only report values of the full page.
    # A cut inside a number can shorten it (": 21990" for ": 2199023255553");
    # the exporter never parses such output, as it requires sg_logs exit 0.
    _, out = pair
    text = _text(out)
    want = out["expect"][parser]["values"]
    for cut in range(len(text) + 1):
        result = PARSERS[parser](text[:cut])
        assert result.status in ("ok", "parse_error")
        assert all(type(v) is int and 0 <= v < 2**64 for v in result.values.values())
        if cut == len(text) or text[cut - 1:cut] == "\n":
            assert all(want.get(k) == v for k, v in result.values.items()), cut
