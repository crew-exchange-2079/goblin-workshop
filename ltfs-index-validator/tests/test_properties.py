"""Property-based tests (hypothesis) over synthetic LTFS indexes.

Profiles: "ci" (default; derandomised, so CI is reproducible) and "explore"
(HYPOTHESIS_PROFILE=explore; random, many more examples).
"""
import copy
import json
import os
import random
import sys
import unittest
from datetime import datetime
from pathlib import Path

from hypothesis import HealthCheck, assume, given, settings, strategies as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate  # noqa: E402
import indexgen as g  # noqa: E402

settings.register_profile("ci", derandomize=True, max_examples=120, deadline=None,
                          database=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("explore", max_examples=2000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))

SENTINEL = "SENTINEL7f3a"
STATUSES = {"VALID", "INVALID", "UNSUPPORTED", "ERROR"}

# Characters that survive XML parsing literally (no C0 controls except tab/LF,
# no CR, no surrogates, no U+FFFE/U+FFFF).
xml_chars = st.characters(blacklist_categories=("Cs",),
                          blacklist_characters="\x00\r￾￿" + "".join(
                              chr(c) for c in range(1, 32) if c not in (9, 10)))
# Name characters: anything that may be percent-encoded, minus what LTFS forbids.
name_chars = st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00/￾￿")


def _ok_name(n):
    return 1 <= len(n) <= 255 and n not in (".", "..")


names = st.text(name_chars, min_size=1, max_size=24).map(g.nfc).filter(_ok_name)
targets = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00￾￿"),
                  min_size=1, max_size=40)


@st.composite
def timestamps(draw):
    d = draw(st.datetimes(min_value=datetime(1, 1, 1), max_value=datetime(9999, 12, 31, 23, 59, 59)))
    return "%04d-%02d-%02dT%02d:%02d:%02d.%09dZ" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second, draw(st.integers(0, 999999999)))


@st.composite
def xattr_lists(draw):
    keys = draw(st.lists(names, max_size=3, unique=True))
    out = []
    for k in keys:
        if draw(st.booleans()):
            out.append(g.Xattr(k, draw(st.text(xml_chars, max_size=40))))
        else:
            import base64
            out.append(g.Xattr(k, base64.b64encode(draw(st.binary(max_size=48))).decode(), "base64"))
    return out


@st.composite
def extent_lists(draw, partitions):
    out, end = [], 0
    for _ in range(draw(st.integers(0, 4))):
        fo = end + draw(st.integers(0, 4096))
        bc = draw(st.integers(1, 1 << 20))
        out.append(g.Extent(draw(st.sampled_from(partitions)), draw(st.integers(6, 10 ** 6)),
                            draw(st.integers(0, 4095)), bc, fo))
        end = fo + bc
    return out, end


@st.composite
def indexes(draw):
    p, q = draw(st.sampled_from([("a", "b"), ("b", "a")]))
    n = draw(st.integers(0, 10))
    uids = draw(st.lists(st.integers(2, 2 ** 63), min_size=n, max_size=n, unique=True))
    root = g.Entry(draw(st.one_of(st.just(""), names)), 1, is_dir=True, xattrs=draw(xattr_lists()))
    dirs, folded = [root], {id(root): set()}
    for uid in uids:
        parent = draw(st.sampled_from(dirs))
        used = folded[id(parent)]
        name = draw(names.filter(lambda s, used=used: s.casefold() not in used))
        used.add(name.casefold())
        kind = draw(st.sampled_from(["dir", "file", "file", "symlink"]))
        e = g.Entry(name, uid, xattrs=draw(xattr_lists()),
                    times={t: draw(timestamps()) for t in draw(st.sets(st.sampled_from(
                        ["creationtime", "changetime", "modifytime", "accesstime", "backuptime"])))})
        if kind == "dir":
            e.is_dir = True
            dirs.append(e)
            folded[id(e)] = set()
        elif kind == "symlink":
            e.symlink = draw(targets)
        else:
            e.extents, end = draw(extent_lists([p, q]))
            e.length = end + draw(st.integers(0, 4096))
        parent.children.append(e)
    idx = g.Index(root, generation=draw(st.integers(0, 2 ** 64)), updatetime=draw(timestamps()),
                  volumeuuid=str(draw(st.uuids())).upper() if draw(st.booleans()) else str(draw(st.uuids())),
                  comment=draw(st.one_of(st.none(), st.text(xml_chars, max_size=60))),
                  policy=draw(st.one_of(st.none(), st.lists(names, max_size=3))),
                  lockstate=draw(st.sampled_from([None, "unlocked", "locked", "permlocked"])))
    used = {part: {x.startblock for e in idx.entries() if e.extents for x in e.extents
                   if x.partition == part} for part in (p, q)}
    if not used[p] and draw(st.booleans()):
        idx.location, idx.backptr = (p, 5), None  # first index written to this partition
    else:
        loc_b = max(used[p] | {4}) + draw(st.integers(4, 64))
        idx.location = (p, loc_b)
        if draw(st.booleans()):
            back = draw(st.integers(5, loc_b - 3).filter(lambda b: b not in used[p]))
            idx.backptr = (p, back)
        else:
            idx.backptr = (q, draw(st.integers(5, 10 ** 6).filter(lambda b: b not in used[q])))
    idx.highestfileuid = idx.max_uid() + draw(st.integers(0, 1000))
    return idx


def check(blob, **kw):
    rep = validate.validate_bytes(blob, options=validate.Options(**kw.pop("opts", {})), **kw)
    assert rep["status"] in STATUSES | {"SCHEMA_VALID_ONLY"}, rep["status"]
    assert rep["exit_code"] == validate.EXIT[rep["status"]]
    assert "INTERNAL_ERROR" not in {d["code"] for d in rep["diagnostics"]}
    assert len(json.dumps(rep).encode()) <= validate.MAX_REPORT_BYTES
    return rep


def codes(rep, severity=None):
    return {d["code"] for d in rep["diagnostics"] if severity in (None, d["severity"])}


class Properties(unittest.TestCase):
    @given(indexes())
    def test_generated_indexes_are_valid_and_clean(self, idx):
        rep = check(g.to_xml(idx))
        self.assertEqual(rep["status"], "VALID", rep["diagnostics"])
        self.assertEqual(rep["counts"], {"errors": 0, "warnings": 0})

    @given(indexes(), st.integers(0, 2 ** 32))
    def test_verdict_invariant_under_xs_all_reordering(self, idx, seed):
        code = random.Random(seed).choice(sorted(g.MUTATIONS))
        m = g.mutate(idx, code, random.Random(seed)) or idx
        a, b = check(g.to_xml(m)), check(g.to_xml(m, order_seed=seed))
        self.assertEqual((a["status"], a["counts"]), (b["status"], b["counts"]))
        self.assertEqual(sorted(d["code"] for d in a["diagnostics"]),
                         sorted(d["code"] for d in b["diagnostics"]))

    @given(indexes(), st.sampled_from(sorted(g.MUTATIONS)), st.integers(0, 2 ** 32))
    def test_each_mutation_is_detected(self, idx, code, seed):
        m = g.mutate(idx, code, random.Random(seed))
        assume(m is not None)
        severity = g.MUTATIONS[code][1]
        rep = check(g.to_xml(m))
        self.assertIn(code, codes(rep, severity), rep["diagnostics"])
        if severity == "error":
            self.assertEqual((rep["status"], rep["exit_code"]), ("INVALID", 1))
        else:
            strict = check(g.to_xml(m), opts={"strict": True})
            self.assertEqual(strict["status"], "INVALID")

    @given(indexes(), st.sampled_from(sorted(g.MUTATIONS)), st.integers(0, 2 ** 32))
    def test_names_and_values_never_echoed(self, idx, code, seed):
        m = g.mutate(idx, code, random.Random(seed)) or idx
        for e in m.entries():
            if e is not m.root and not e.raw_name:
                e.name = SENTINEL + e.name[:4]
                break
        m.creator = m.comment = SENTINEL
        m.root.xattrs = [g.Xattr(SENTINEL, SENTINEL)]
        out = json.dumps(check(g.to_xml(m)))
        self.assertNotIn(SENTINEL, out)

    @given(st.binary(max_size=2048))
    def test_arbitrary_bytes_never_crash(self, blob):
        check(blob)

    @given(indexes(), st.data())
    def test_byte_level_corruption_never_crashes(self, idx, data):
        blob = bytearray(g.to_xml(idx))
        for _ in range(data.draw(st.integers(1, 4))):
            i = data.draw(st.integers(0, len(blob)))
            j = data.draw(st.integers(i, min(len(blob), i + 64)))
            op = data.draw(st.sampled_from(["delete", "insert", "duplicate"]))
            if op == "delete":
                del blob[i:j]
            elif op == "insert":
                blob[i:i] = data.draw(st.binary(min_size=1, max_size=16))
            else:
                blob[i:i] = blob[i:j]
        check(bytes(blob))


def successor(prev, rng):
    """An index that legitimately follows `prev` (back pointer names prev.location)."""
    cur = copy.deepcopy(prev)
    p, b = prev.location
    cur.backptr = (p, b)
    cur.location = (p, b + rng.randint(3, 1000))
    cur.generation = prev.generation + rng.randint(1, 5)
    cur.highestfileuid = (prev.highestfileuid or prev.max_uid()) + rng.randint(0, 5)
    return cur


CHAIN_MUTATIONS = {
    "PREVIOUS_VOLUME_MISMATCH": lambda c, p: setattr(
        c, "volumeuuid", "ffffffff-ffff-4fff-8fff-fffffffffff%d" % (p.volumeuuid[-1] == "0")),
    "PREVIOUS_NOT_REFERENCED": lambda c, p: setattr(c, "backptr", (c.backptr[0], c.backptr[1] - 1)),
    "GENERATION_REGRESSION": lambda c, p: setattr(c, "generation", p.generation - 1),
    "GENERATION_NOT_INCREASING": lambda c, p: setattr(c, "generation", p.generation),
    "HIGHESTFILEUID_REGRESSION": lambda c, p: setattr(c, "highestfileuid", p.highestfileuid - 1),
    "UPDATETIME_REGRESSION": lambda c, p: setattr(c, "updatetime", "0001-01-01T00:00:00.000000000Z"),
}


class ChainProperties(unittest.TestCase):
    @given(indexes(), st.integers(0, 2 ** 32))
    def test_successor_is_valid_against_previous(self, prev, seed):
        cur = successor(prev, random.Random(seed))
        rep = check(g.to_xml(cur), previous_data=g.to_xml(prev))
        self.assertEqual(rep["status"], "VALID", rep["diagnostics"])
        self.assertIsNotNone(rep["previous"]["sha256"])

    @given(indexes(), st.sampled_from(sorted(CHAIN_MUTATIONS)), st.integers(0, 2 ** 32))
    def test_chain_mutation_is_detected(self, prev, code, seed):
        cur = successor(prev, random.Random(seed))
        if code == "GENERATION_REGRESSION":
            assume(prev.generation > 0)
        if code == "HIGHESTFILEUID_REGRESSION":
            assume(prev.highestfileuid > cur.max_uid())
        if code == "UPDATETIME_REGRESSION":
            assume(prev.updatetime > "0001-01-01T00:00:00.000000000Z")
        if code == "PREVIOUS_NOT_REFERENCED":
            assume(cur.backptr[1] - 1 >= 5)
        CHAIN_MUTATIONS[code](cur, prev)
        rep = check(g.to_xml(cur), previous_data=g.to_xml(prev))
        self.assertIn(code, codes(rep), rep["diagnostics"])

    @given(indexes(), st.sampled_from(sorted(g.MUTATIONS)), st.integers(0, 2 ** 32))
    def test_invalid_previous_is_error_not_invalid(self, prev, code, seed):
        assume(g.MUTATIONS[code][1] == "error")
        bad_prev = g.mutate(prev, code, random.Random(seed))
        assume(bad_prev is not None)
        cur = successor(prev, random.Random(seed))
        rep = check(g.to_xml(cur), previous_data=g.to_xml(bad_prev))
        self.assertEqual((rep["status"], rep["exit_code"]), ("ERROR", 3))
        self.assertIn("PREVIOUS_INDEX_REJECTED", codes(rep))


if __name__ == "__main__":
    unittest.main()
