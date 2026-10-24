#!/usr/bin/env python3
"""Build the synthetic sg_logs fixture corpus, or check it is reproducible.

Every page in PAGES is turned into a SPC log page in ASCII hex
(pages/<id>.hex) and decoded offline by each real sg_logs binary given on
the command line:

    sg_logs --pdt=1 [--vendor=lto5] [-v] --in=pages/<id>.hex

No device is opened. --pdt=1 (sequential-access) makes sg_logs pick the SSC
decoders that a tape drive's INQUIRY would select. The stdout of each run is
stored as sg3_utils-<version>/<id>[.v].txt, and manifest.json records the
argv, exit status, sha256 and what every tape-exporter parser must return.

    python3 generate.py --sg-logs 1.46=/usr/bin/sg_logs \\
                        --sg-logs 1.48=/path/to/1.48/sg_logs ...   # rewrite
    python3 generate.py --check --sg-logs 1.46=/usr/bin/sg_logs     # verify

--check rewrites nothing; it fails if any committed hex, output or manifest
entry differs from what the given binaries produce now. Versions not given
are left alone (rewrite) or not checked (--check).

The expected parser results are derived from the page *specification* below,
never from running the parsers, so the corpus test cannot pass by agreeing
with itself.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
U64_MAX = 2**64 - 1

# Read-may-clear: per SSC, TapeAlert flags (page 0x2e) may be reset by the
# act of reading the page (and are on many drives, depending on the
# Informational Exceptions mode page). Never read 0x2e from a drive another
# tool is watching. These fixtures exist only so parsers can be fed the
# decoded text and proven to reject it.
READ_MAY_CLEAR = {0x2E}

# Where each sg_logs binary comes from. Kept in the manifest so a reader can
# rebuild exactly what produced the text; see README.md for the commands.
SOURCES = {
    "1.46": "Ubuntu 24.04 package sg3-utils 1.46-3ubuntu4.1 (/usr/bin/sg_logs)",
    "1.49": "upstream https://github.com/doug-gilbert/sg3_utils tag release-1.49 "
            "(commit b1f5e2eee8f91cda2926dbcc71f7734ec9fac358), built from source",
}


def be(value, width):
    return value.to_bytes(width, "big")


def counters(*pairs, width=4, control=0x60):
    """(code, value) pairs -> parameters with a big-endian counter."""
    return [(code, control, be(value, width)) for code, value in pairs]


def lpage(page, params, subpage=0):
    body = b"".join(be(code, 2) + bytes([ctl, len(val)]) + val
                    for code, ctl, val in params)
    b0 = page | (0x40 if subpage else 0)
    return bytes([b0, subpage]) + be(len(body), 2) + body


EC_LABELS = {
    0: "corrected_without_delay", 1: "corrected_with_delay",
    2: "rewrites_or_rereads", 3: "total_corrected",
    4: "correction_algorithm_processed", 5: "bytes_processed",
    6: "total_uncorrected",
}
SA_LABELS = {
    0: "bytes_received_write", 1: "bytes_written_media",
    2: "bytes_read_media", 3: "bytes_transferred_read",
}


def ec_page(page, values, extra=(), width=4):
    params = counters(*values.items(), width=width)
    params += list(extra)
    return lpage(page, params)


def ec_expect(values):
    return {EC_LABELS[c]: v for c, v in values.items() if c in EC_LABELS and v <= U64_MAX}


def sa_page(bytes_by_code, cleaning=None):
    params = [(c, 0x40, be(v, 8)) for c, v in bytes_by_code.items()]
    if cleaning is not None:
        params.append((0x100, 0x40, be(cleaning, 8)))
    return lpage(0x0C, params)


def sa_expect(bytes_by_code, cleaning, verbose):
    out = {}
    if verbose:  # exact bytes are only printed with -v; plain "N GB" is not exact
        out.update({SA_LABELS[c]: v for c, v in bytes_by_code.items() if c in SA_LABELS})
    if cleaning is not None:
        out["cleaning_action_required"] = 1 if cleaning else 0
    return out


def tapealert(set_flags):
    return lpage(0x2E, [(f, 0x00, bytes([1 if f in set_flags else 0])) for f in range(1, 0x41)])


# ---------------------------------------------------------------------------
# The corpus. Each entry: id, page, subpage, description, page bytes,
# vendor (None or sg_logs --vendor), and expected parser results by
# verbosity: {"error_counters.write": {...}|None, ...}. None = parse_error.

def _corpus():
    out = []

    def add(id_, page, desc, data, expect, subpage=0, vendor=None):
        out.append(dict(id=id_, page=page, subpage=subpage, description=desc,
                        data=data, expect=expect, vendor=vendor))

    def ec(id_, page, desc, values, extra=(), width=4):
        exp = ec_expect(values)
        direction = {0x02: "write", 0x03: "read"}[page]
        add(id_, page, desc, ec_page(page, values, extra, width),
            lambda v: {f"error_counters.{direction}": exp})

    w_typ = {0: 0, 1: 3, 2: 17, 3: 3, 4: 21, 5: 2199023255553, 6: 0}
    ec("p02-typical", 0x02, "Write error counters, all seven SPC parameters", w_typ, width=8)
    ec("p02-zero", 0x02, "Write error counters, all zero (fresh cartridge)",
       {c: 0 for c in range(7)})
    ec("p02-u64max", 0x02, "Write error counters at the uint64 edge",
       {0: U64_MAX, 1: U64_MAX - 1, 2: 2**63, 3: 2**32, 4: 2**32 - 1,
        5: 10**18, 6: 1}, width=8)
    ec("p02-partial-vendor", 0x02,
       "Write error counters: only codes 0, 5, 6 plus a vendor parameter 0x8000",
       {0: 5, 5: 1073741825, 6: 2}, extra=counters((0x8000, 99)))
    ec("p03-typical", 0x03, "Read error counters, all seven SPC parameters",
       {0: 12, 1: 0, 2: 4, 3: 12, 4: 40, 5: 1500000000000, 6: 1}, width=8)
    ec("p03-zero", 0x03, "Read error counters, all zero", {c: 0 for c in range(7)})
    ec("p03-partial-vendor", 0x03,
       "Read error counters: codes 2 and 6 plus vendor parameters 0x8001, 0x8002",
       {2: 7, 6: 0}, extra=counters((0x8001, 1), (0x8002, 2)))

    def sa(id_, desc, bytes_by_code, cleaning):
        add(id_, 0x0C, desc, sa_page(bytes_by_code, cleaning),
            lambda v: {"sequential_access": sa_expect(bytes_by_code, cleaning, v)})

    sa("p0c-cleaning-required", "Sequential access: small byte counts, cleaning required",
       {0: 2500000, 1: 1250000, 2: 3100000, 3: 6200000}, 1)
    sa("p0c-cleaning-not-required", "Sequential access: TB-scale counts, no cleaning",
       {0: 12000000000000, 1: 6000000000001, 2: 999999999999, 3: 1000000000000}, 0)
    sa("p0c-zero", "Sequential access: all counters zero, no cleaning",
       {0: 0, 1: 0, 2: 0, 3: 0}, 0)
    sa("p0c-partial", "Sequential access: only parameter 1, no cleaning parameter",
       {1: 42000000000}, None)

    nothing = lambda v: {}
    add("p2e-clear", 0x2E, "TapeAlert, all 64 flags clear (READ-MAY-CLEAR page)",
        tapealert(set()), nothing)
    add("p2e-flags-set", 0x2E,
        "TapeAlert, flags 3, 4, 0x14 (clean now), 0x1e set (READ-MAY-CLEAR page)",
        tapealert({3, 4, 0x14, 0x1E}), nothing)
    add("p31-lto", 0x31, "LTO tape capacity (vendor page, --vendor=lto5), MB units",
        lpage(0x31, counters((1, 2400000), (2, 0), (3, 2500000), (4, 0))),
        nothing, vendor="lto5")
    add("p17-volume-stats", 0x17,
        "Volume statistics subpage 0 (SSC-4): page valid, counts, volume serial",
        lpage(0x17, [(0, 0x03, b"\x01")]
              + counters((1, 314), (2, 5000), (3, 2), (4, 0), (7, 1200), (8, 1), width=8)
              + [(0x40, 0x01, b"SYN001L8" + b" " * 24)]),
        nothing)
    return out


PARSERS = ("error_counters.write", "error_counters.read", "sequential_access")


def hex_text(data, ident, desc):
    lines = [f"# {ident}: {desc}",
             "# Synthetic SPC log page; not captured from any device."]
    for i in range(0, len(data), 16):
        lines.append(" ".join(f"{b:02x}" for b in data[i:i + 16]))
    return "\n".join(lines) + "\n"


def run_sg_logs(exe, entry, verbose, hexpath):
    argv = ["sg_logs", "--pdt=1"]
    if entry["vendor"]:
        argv.append(f"--vendor={entry['vendor']}")
    if verbose:
        argv.append("-v")
    argv.append(f"--in=pages/{hexpath.name}")
    proc = subprocess.run([exe] + argv[1:], cwd=HERE, capture_output=True,
                          text=True, timeout=30, check=False)
    return argv, proc


def version_string(exe):
    p = subprocess.run([exe, "-V"], capture_output=True, text=True, timeout=10, check=False)
    return (p.stdout + p.stderr).strip()


def expected(entry, verbose):
    got = entry["expect"](verbose)
    return {p: ({"status": "ok", "values": got[p]} if p in got
                else {"status": "parse_error", "values": {}}) for p in PARSERS}


def build(binaries, check):
    manifest_path = HERE / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    old_fx = {f["id"]: f for f in old.get("fixtures", [])}
    problems = []
    fixtures = []
    versions = dict(old.get("sg_logs_versions", {}))

    def put(path, text):
        if check:
            if not path.exists() or path.read_text() != text:
                problems.append(f"{path.relative_to(HERE)} differs")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    for ver, exe in binaries.items():
        vs = version_string(exe)
        if check and versions.get(ver, {}).get("version_string") != vs:
            problems.append(f"sg3_utils {ver}: binary says {vs!r}, manifest has "
                            f"{versions.get(ver, {}).get('version_string')!r}")
        versions.setdefault(ver, {})["version_string"] = vs
        versions[ver]["source"] = SOURCES.get(ver, "unrecorded")

    for entry in _corpus():
        hexpath = HERE / "pages" / f"{entry['id']}.hex"
        put(hexpath, hex_text(entry["data"], entry["id"], entry["description"]))
        prev = old_fx.get(entry["id"], {}).get("outputs", [])
        outputs = [o for o in prev if o["sg3_utils"] not in binaries]
        for ver, exe in binaries.items():
            for verbose in (False, True):
                argv, proc = run_sg_logs(exe, entry, verbose, hexpath)
                name = f"{entry['id']}{'.v' if verbose else ''}.txt"
                rel = f"sg3_utils-{ver}/{name}"
                put(HERE / rel, proc.stdout)
                if proc.returncode != 0:
                    problems.append(f"{rel}: sg_logs exited {proc.returncode}: {proc.stderr.strip()}")
                outputs.append({
                    "sg3_utils": ver, "file": rel, "argv": argv, "verbose": verbose,
                    "exit_status": proc.returncode,
                    "stderr": proc.stderr,
                    "sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(),
                    "expect": expected(entry, verbose),
                })
        outputs.sort(key=lambda o: (o["sg3_utils"], o["verbose"]))
        fixtures.append({
            "id": entry["id"],
            "page": f"0x{entry['page']:02x}",
            "subpage": f"0x{entry['subpage']:02x}",
            "description": entry["description"],
            "read_may_clear": entry["page"] in READ_MAY_CLEAR,
            "page_hex": f"pages/{entry['id']}.hex",
            "outputs": outputs,
        })

    manifest = {
        "about": "Synthetic sg_logs outputs for SSC log pages; see README.md. "
                 "Not captured from any device.",
        "parsers": list(PARSERS),
        "sg_logs_versions": dict(sorted(versions.items())),
        "fixtures": fixtures,
    }
    text = json.dumps(manifest, indent=1, sort_keys=False) + "\n"
    if check:
        # Only compare outputs for the versions that were actually run.
        for fx in fixtures:
            ofx = old_fx.get(fx["id"])
            if ofx is None:
                problems.append(f"manifest: fixture {fx['id']} missing")
                continue
            mine = [o for o in fx["outputs"] if o["sg3_utils"] in binaries]
            theirs = [o for o in ofx["outputs"] if o["sg3_utils"] in binaries]
            if mine != theirs:
                problems.append(f"manifest: outputs for {fx['id']} differ")
            if {k: v for k, v in fx.items() if k != "outputs"} != \
               {k: v for k, v in ofx.items() if k != "outputs"}:
                problems.append(f"manifest: metadata for {fx['id']} differs")
        if [f["id"] for f in fixtures] != [f["id"] for f in old.get("fixtures", [])]:
            problems.append("manifest: fixture list differs")
    else:
        manifest_path.write_text(text)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sg-logs", action="append", required=True, metavar="VER=PATH",
                    help="an sg_logs binary and the sg3_utils release it is from")
    ap.add_argument("--check", action="store_true", help="verify instead of rewriting")
    args = ap.parse_args(argv)
    binaries = {}
    for spec in args.sg_logs:
        ver, sep, path = spec.partition("=")
        if not sep or not re.fullmatch(r"\d+\.\d+", ver):
            ap.error(f"--sg-logs wants VER=PATH, got {spec!r}")
        binaries[ver] = path
    problems = build(binaries, args.check)
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    if not problems:
        print(("checked" if args.check else "wrote") + f" corpus for sg3_utils {', '.join(binaries)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
