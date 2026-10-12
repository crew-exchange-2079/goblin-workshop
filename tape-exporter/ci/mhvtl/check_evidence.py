"""Verdict for the mhvtl integration run. Standard library only.

Checks, from strace of the exporter process tree (run as an unprivileged
user) and from the scraped metrics:

  exec      every program the exporter started is exactly
            [sg_logs, --readonly, --page=0x2|0x3, <identified sg node>]
  open      device nodes opened: only the identified sg node, O_RDONLY
  cdb       every SG_IO CDB is INQUIRY (0x12) or LOG SENSE (0x4d) with
            SP=0, PPC=0 and page 0x02/0x03 -- no LOG SELECT (0x4c), no
            motion, no writes; at least one LOG SENSE per page
  write     no write()/pwrite()/writev() to any sg node (the sg driver's
            async command interface)
  metrics   per page: if the emulator lists it in Supported Log Pages the
            collector must be ok with all 7 parameters present and values
            equal to the harness's own sg_logs run; if it does not list it,
            the collector must not be ok and no counters may be exported
            (unknown is not zero); if support is unknown, only consistency
            is checked and the result says UNKNOWN.

Exit 0 only if every check passes. Writes summary.json and summary.md.
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from lto_exporter.parsers import (PARAM_LABELS,  # noqa: E402
                                  parse_sg_logs_error_counters)

PAGES = {"write": 0x02, "read": 0x03}
_HEX = re.compile(r"\\x([0-9a-fA-F]{2})")
_LINE = re.compile(r"^(\d+)\s+(.*)$")
_STR = r'"((?:\\x[0-9a-fA-F]{2})*)"(\.\.\.)?'
_EXECVE = re.compile(r"execve\(" + _STR + r", \[(.*?)\](?:\.\.\.)?, ")
_OPEN = re.compile(r"open(?:at2?)?\((?:[^,]*, )?" + _STR + r", ([A-Z_|0-9]+)")
_FDPATH = re.compile(r"\(\d+<((?:\\x[0-9a-fA-F]{2})*)>")
_CMDP = re.compile(r'cmdp="((?:\\x[0-9a-fA-F]{2})*)"(\.\.\.)?')
_IOCTL = re.compile(r"ioctl\(\d+<((?:\\x[0-9a-fA-F]{2})*)>, ([A-Za-z0-9_]+|0x[0-9a-f]+|_IOC\([^)]*\))")
DEVICE_PREFIXES = ("/dev/sg", "/dev/st", "/dev/nst", "/dev/sd", "/dev/bsg", "/dev/sch",
                   "/dev/tape", "/dev/mhvtl")
ALLOWED_SG_IOCTLS = {"SG_IO", "SG_GET_VERSION_NUM", "SG_GET_TIMEOUT", "SG_GET_RESERVED_SIZE",
                     "SG_EMULATED_HOST", "SG_GET_SCSI_ID", "SG_GET_PACK_ID",
                     "SG_GET_NUM_WAITING", "SG_GET_SG_TABLESIZE", "SG_GET_COMMAND_Q",
                     "SG_GET_KEEP_ORPHAN", "SG_GET_LOW_DMA", "SG_GET_ACCESS_COUNT"}


def unhex(s):
    return bytes(int(h, 16) for h in _HEX.findall(s))


def ustr(s):
    return unhex(s).decode("utf-8", "replace")


class Verdict:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail):
        self.checks.append({"check": name, "result": ok if isinstance(ok, str) else
                            ("PASS" if ok else "FAIL"), "detail": detail})

    @property
    def failed(self):
        return [c for c in self.checks if c["result"] == "FAIL"]


def analyse_strace(text, sg, sg_logs):
    v = Verdict()
    execs, opens, cdbs, sg_writes, bad_ioctls, undecoded = [], [], [], [], [], 0
    first_exec = True
    for raw in text.splitlines():
        m = _LINE.match(raw)
        body = m.group(2) if m else raw
        if body.startswith("execve("):
            e = _EXECVE.match(body)
            if not e or e.group(2):
                undecoded += 1
                continue
            argv = [ustr(a) for a in re.findall(r'"((?:\\x[0-9a-fA-F]{2})*)"', e.group(3))]
            failed = "= -1 " in body
            if first_exec:
                first_exec = False  # strace launching the exporter itself
                continue
            execs.append({"path": ustr(e.group(1)), "argv": argv, "failed": failed})
        elif body.startswith(("open(", "openat(", "openat2(")):
            o = _OPEN.match(body)
            if not o:
                continue
            path = ustr(o.group(1))
            if path.startswith("/dev/"):
                opens.append({"path": path, "flags": o.group(3), "result": body.rsplit("= ", 1)[-1]})
        elif body.startswith("ioctl("):
            i = _IOCTL.match(body)
            if not i:
                continue
            path, req = ustr(i.group(1)), i.group(2)
            if not path.startswith("/dev/sg"):
                continue
            if req not in ALLOWED_SG_IOCTLS:
                bad_ioctls.append({"path": path, "request": req})
            if req == "SG_IO":
                c = _CMDP.search(body)
                if not c or c.group(2):
                    undecoded += 1
                else:
                    cdbs.append({"path": path, "cdb": unhex(c.group(1)).hex()})
        elif body.startswith(("write(", "pwrite64(", "writev(")):
            f = _FDPATH.search(body)
            if f and ustr(f.group(1)).startswith(("/dev/sg", "/dev/st", "/dev/nst", "/dev/mhvtl")):
                sg_writes.append(body[:120])

    # exec
    bad_exec = []
    for e in execs:
        if e["failed"]:
            bad_exec.append(e)
            continue
        if not (e["path"] == sg_logs and len(e["argv"]) == 4
                and os.path.basename(e["argv"][0]) == "sg_logs"
                and e["argv"][1] == "--readonly"
                and e["argv"][2] in ("--page=0x2", "--page=0x3")
                and e["argv"][3] == sg):
            bad_exec.append(e)
    v.add("exec: only sg_logs --readonly --page=0x2|0x3 <sg>", bool(execs) and not bad_exec,
          {"count": len(execs), "unexpected": bad_exec[:10]})

    # opens
    dev_opens = [o for o in opens if o["path"].startswith(DEVICE_PREFIXES)]
    bad_open = [o for o in dev_opens if o["path"] != sg
                or "O_WRONLY" in o["flags"] or "O_RDWR" in o["flags"]
                or not o["flags"].startswith("O_RDONLY")]
    bad_open += [o for o in opens if o["path"] not in ("/dev/null", "/dev/urandom")
                 and not o["path"].startswith(DEVICE_PREFIXES)
                 and ("O_WRONLY" in o["flags"] or "O_RDWR" in o["flags"])]
    v.add("open: only the identified sg node, O_RDONLY", bool(dev_opens) and not bad_open,
          {"device_opens": len(dev_opens), "flags_seen": sorted({o["flags"] for o in dev_opens}),
           "unexpected": bad_open[:10]})

    # CDBs
    bad_cdb, log_sense_pages = [], {}
    for c in cdbs:
        b = bytes.fromhex(c["cdb"])
        op = b[0] if b else None
        if c["path"] != sg:
            bad_cdb.append({**c, "why": "other node"})
        elif op == 0x12:
            continue
        elif op == 0x4D and len(b) >= 10:
            sp, ppc, page = b[1] & 0x01, (b[1] >> 1) & 0x01, b[2] & 0x3F
            if sp or ppc or page not in PAGES.values():
                bad_cdb.append({**c, "why": f"LOG SENSE sp={sp} ppc={ppc} page=0x{page:02x}"})
            log_sense_pages[page] = log_sense_pages.get(page, 0) + 1
        else:
            bad_cdb.append({**c, "why": f"opcode 0x{op:02x} not allowed" if op is not None else "empty"})
    opcodes = sorted({c["cdb"][:2] for c in cdbs})
    v.add("cdb: only INQUIRY / LOG SENSE(SP=0,PPC=0,page 0x02|0x03)",
          bool(cdbs) and not bad_cdb and undecoded == 0
          and all(log_sense_pages.get(p) for p in PAGES.values()),
          {"sg_io_count": len(cdbs), "opcodes": opcodes,
           "log_sense_per_page": {f"0x{k:02x}": n for k, n in sorted(log_sense_pages.items())},
           "undecoded_lines": undecoded, "unexpected": bad_cdb[:10]})
    v.add("ioctl: only SG_IO / SG_GET_* on the sg node", not bad_ioctls, {"unexpected": bad_ioctls[:10]})
    v.add("write: no write()/writev() to tape or sg nodes", not sg_writes, {"unexpected": sg_writes[:10]})
    return v


def parse_metrics(text):
    rows = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([a-z_]+)(?:\{(.*)\})? (\S+)$", line)
        if m:
            labels = dict(re.findall(r'([a-z_]+)="((?:[^"\\]|\\.)*)"', m.group(2) or ""))
            rows.append((m.group(1), labels, m.group(3)))
    return rows


def supported_pages(path, rc_path):
    try:
        rc = int(open(rc_path).read().strip())
        text = open(path).read()
    except (OSError, ValueError):
        return None
    if rc != 0:
        return None
    pages = {int(h, 16) for h in re.findall(r"^\s+0x([0-9a-fA-F]{2})\b", text, re.M)}
    return pages or None


def analyse_metrics(v, evidence, sg):
    text = open(os.path.join(evidence, "metrics_3.txt")).read()
    rows = parse_metrics(text)
    v.add("metrics: no device path or serial in output", "/dev/" not in text and "GWCI" not in text, {})
    unreaped = [val for n, _, val in rows if n == "tape_exporter_unreaped_children"]
    v.add("metrics: tape_exporter_unreaped_children == 0", unreaped == ["0"], {"value": unreaped})
    supported = supported_pages(os.path.join(evidence, "supported_pages.txt"),
                                os.path.join(evidence, "supported_pages.rc"))
    for direction, page in PAGES.items():
        coll = f"sg_logs_{direction}_errors"
        status = [l["status"] for n, l, _ in rows
                  if n == "tape_exporter_collector_status" and l.get("collector") == coll]
        counters = {l["parameter"]: int(val) for n, l, val in rows
                    if n == "tape_log_errors_total" and l.get("direction") == direction}
        bytes_ = [int(val) for n, l, val in rows
                  if n == "tape_bytes_processed_total" and l.get("direction") == direction]
        if bytes_:
            counters["bytes_processed"] = bytes_[0]
        present = {l["parameter"]: val for n, l, val in rows
                   if n == "tape_log_parameter_present" and l.get("direction") == direction}
        detail = {"status": status, "values": counters, "present": present,
                  "listed_in_supported_pages": None if supported is None else page in supported}
        name = f"metrics: page 0x{page:02x} ({direction})"
        if supported is None:
            consistent = status == ["ok"] or not counters
            v.add(name, "UNKNOWN" if consistent else False,
                  {**detail, "note": "supported-pages query failed; support unknown, not zero"})
        elif page in supported:
            harness = open(os.path.join(evidence, f"harness_sg_logs_0x{page:x}.txt")).read()
            parsed = parse_sg_logs_error_counters(harness, direction)
            detail["harness_parsed"] = {"status": parsed.status, "values": parsed.values}
            ok = (status == ["ok"] and set(present) == set(PARAM_LABELS)
                  and parsed.status == "ok" and counters == parsed.values)
            v.add(name, ok, detail)
        else:
            v.add(name, status != ["ok"] and not counters and not present, detail)
    return supported


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--sg", required=True)
    ap.add_argument("--sg-logs", required=True)
    args = ap.parse_args()
    with open(os.path.join(args.evidence, "strace.txt"), encoding="utf-8", errors="replace") as fh:
        v = analyse_strace(fh.read(), args.sg, args.sg_logs)
    supported = analyse_metrics(v, args.evidence, args.sg)
    ident = json.load(open(os.path.join(args.evidence, "identify.json")))
    v.add("identify: exactly one mhvtl-hosted drive with the synthetic serial",
          ident.get("ok") is True and ident.get("matches") == [os.path.basename(args.sg)],
          {"matches": ident.get("matches"), "virtual_evidence": ident.get("virtual_evidence")})
    overall = "FAIL" if v.failed else "PASS"
    summary = {"overall": overall, "sg": args.sg,
               "supported_pages": None if supported is None else sorted(f"0x{p:02x}" for p in supported),
               "checks": v.checks}
    with open(os.path.join(args.evidence, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    lines = [f"# lto-exporter mhvtl integration: {overall}", "",
             f"Device: `{args.sg}` (mhvtl emulated drive, synthetic serial)", "",
             f"Supported log pages reported by the emulator: {summary['supported_pages']}", "",
             "| check | result |", "|---|---|"]
    lines += [f"| {c['check']} | {c['result']} |" for c in v.checks]
    with open(os.path.join(args.evidence, "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(json.dumps(v.checks, indent=2))
    return 1 if v.failed else 0


if __name__ == "__main__":
    sys.exit(main())
