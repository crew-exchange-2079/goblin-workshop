#!/usr/bin/env python3
"""Regenerate corpus/ (synthetic good and bad LTFS 2.4.0 indexes) deterministically.

    python tests/make_corpus.py            # rewrite corpus/
    python tests/make_corpus.py --check    # exit 1 if corpus/ differs from what would be written

Every case is built from the model in indexgen.py. corpus/manifest.json records
what each file is meant to demonstrate: its expected status and the diagnostic
code(s) it must raise. The expectation comes from the mutation that built the
file, never from running the validator.
"""
import argparse
import copy
import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import indexgen as g  # noqa: E402

CORPUS = HERE.parent / "corpus"


def _first_index():
    root = g.Entry("", 1, is_dir=True)
    return g.Index(root, generation=1, location=("a", 5), backptr=None, highestfileuid=1)


def _index_partition_copy():
    idx = g.sample_index()
    idx.location, idx.backptr = ("a", 40), ("b", 20)  # IP copy pointing back at the DP index
    return idx


def _encoded_names():
    idx = g.sample_index()
    sub = idx.root.children[0]
    sub.children.append(g.Entry("100%\r\n\x7f\x01", 8, length=0, extents=[]))
    sub.children.append(g.Entry("caf%C3%A9", 9, length=0, extents=[], raw_name=True))
    return idx


def good_cases():
    sample = g.sample_index()
    yield "good/sample.xml", g.to_xml(sample), None
    yield "good/sample-reordered.xml", g.to_xml(sample, order_seed=7), None
    yield "good/first-index.xml", g.to_xml(_first_index()), None
    yield "good/index-partition-copy.xml", g.to_xml(_index_partition_copy()), None
    yield "good/percent-encoded-names.xml", g.to_xml(_encoded_names()), None
    nxt = copy.deepcopy(sample)
    nxt.backptr, nxt.location, nxt.generation = sample.location, ("b", 30), sample.generation + 1
    yield "good/chain-next.xml", g.to_xml(nxt), "good/sample.xml"


def bad_cases():
    sample = g.sample_index()
    for code in sorted(g.MUTATIONS):
        m = g.mutate(sample, code, random.Random(0))
        assert m is not None, code
        yield "bad/%s.xml" % code.lower().replace("_", "-"), g.to_xml(m), None, code, g.MUTATIONS[code][1]
    chain = {
        "GENERATION_NOT_INCREASING": dict(generation=sample.generation),
        "GENERATION_REGRESSION": dict(generation=sample.generation - 1),
        "PREVIOUS_NOT_REFERENCED": dict(backptr=("b", 15)),
        "PREVIOUS_VOLUME_MISMATCH": dict(volumeuuid="11111111-2222-4333-8444-555555555555"),
        "HIGHESTFILEUID_REGRESSION": dict(highestfileuid=sample.highestfileuid - 1),
        "UPDATETIME_REGRESSION": dict(updatetime="2025-12-31T23:59:59.999999999Z"),
    }
    for code, changes in sorted(chain.items()):
        nxt = copy.deepcopy(sample)
        nxt.backptr, nxt.location, nxt.generation = sample.location, ("b", 30), sample.generation + 1
        for k, v in changes.items():
            setattr(nxt, k, v)
        severity = "warning" if code == "UPDATETIME_REGRESSION" else "error"
        yield ("bad/chain-%s.xml" % code.lower().replace("_", "-"), g.to_xml(nxt), "good/sample.xml",
               code, severity)


def build():
    files, manifest = {}, []
    for path, blob, prev in good_cases():
        files[path] = blob
        manifest.append({"file": path, "previous": prev, "status": "VALID", "codes": []})
    for path, blob, prev, code, severity in bad_cases():
        files[path] = blob
        manifest.append({"file": path, "previous": prev,
                         "status": "INVALID" if severity == "error" else "VALID",
                         "strict_status": "INVALID", "codes": [code], "severity": severity})
    files["manifest.json"] = (json.dumps({"generator": "tests/make_corpus.py", "cases": manifest},
                                         indent=2, sort_keys=True) + "\n").encode()
    return files


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=str(CORPUS))
    args = ap.parse_args(argv)
    out, files = Path(args.out), build()
    existing = {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} if out.exists() else set()
    if args.check:
        drift = sorted(p for p, b in files.items()
                       if not (out / p).is_file() or (out / p).read_bytes() != b)
        drift += sorted(existing - set(files))
        for p in drift:
            print("corpus drift:", p)
        return 1 if drift else 0
    for p in existing - set(files):
        (out / p).unlink()
    for p, b in files.items():
        (out / p).parent.mkdir(parents=True, exist_ok=True)
        (out / p).write_bytes(b)
    print("wrote %d files to %s" % (len(files), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
