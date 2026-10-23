"""Scripted mhvtl scenarios with assertions on lto-exporter metrics.

The harness (root, on a disposable VM) drives ONE emulated mhvtl drive
through media states with mhvtl's own tools and plain SSC commands sent by
`sg_raw`; the exporter (unprivileged, under strace, started by run.sh) is
only ever scraped. After every step the harness also reads pages 0x02, 0x03
and 0x0c itself with the exporter's exact `sg_logs` argv and requires the
exporter to agree with that read: same status, same values, and no series at
all when the read fails (unknown is not zero).

Scenarios (see README.md for what each asserts and why):
  baseline            empty drive, everything readable, counters zero
  load_unload         per-load byte counters: write/read, unload keeps,
                      reload resets
  full_tape           write to a 4 MiB cartridge until VOLUME OVERFLOW
  cleaning_cartridge  cleaning flag set by a cleaning cart, survives exporter
                      scrapes and unload, clears only when the host reads 0x2E
  drive_offline       SCSI device state offline: every collector os_error,
                      no counters; back to running: values unchanged
  worm                WORM cartridge: append accepted, overwrite refused and
                      not counted

Standard library only, plus tape-exporter's own parser and verdict modules.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXPORTER = os.path.join(REPO, "tape-exporter")
sys.path.insert(0, EXPORTER)
sys.path.insert(0, os.path.join(EXPORTER, "ci", "mhvtl"))

from check_evidence import SA_BYTES, analyse_strace, parse_metrics  # noqa: E402
from lto_exporter.parsers import (PARAM_LABELS, SA_FIELDS,  # noqa: E402
                                  parse_sg_logs_error_counters,
                                  parse_sg_logs_sequential_access,
                                  sg_exit_status)

# Page -> (collector name, error-counter direction or None for 0x0c)
PAGES = {"0x2": ("sg_logs_write_errors", "write"),
         "0x3": ("sg_logs_read_errors", "read"),
         "0xc": ("sg_logs_sequential_access", None)}
COLLECTORS = tuple(c for c, _ in PAGES.values())
BLOCK = 64 * 1024            # variable-block WRITE(6)/READ(6) transfer size
FULL_TAPE_MB = 4             # mhvtl early warning is 2 MiB before the end
MAX_FULL_TAPE_WRITES = 256   # 16 MiB: far beyond a 4 MiB cartridge
ZERO_BYTES = {f: 0 for f in SA_FIELDS if f != "cleaning_action_required"}


class HarnessError(Exception):
    """A harness action (not the exporter) did not do what it should."""


# ---- metrics and harness reads -> comparable views ------------------------

def snapshot(metrics_text):
    """Exporter /metrics text -> per-page view plus exporter-level values."""
    rows = parse_metrics(metrics_text)
    pages = {}
    for page, (coll, direction) in PAGES.items():
        status = [lbl["status"] for n, lbl, _ in rows
                  if n == "tape_exporter_collector_status" and lbl.get("collector") == coll]
        success = [v for n, lbl, v in rows
                   if n == "tape_exporter_collector_success" and lbl.get("collector") == coll]
        if direction:
            values = {lbl["parameter"]: int(v) for n, lbl, v in rows
                      if n == "tape_log_errors_total" and lbl.get("direction") == direction}
            values.update({"bytes_processed": int(v) for n, lbl, v in rows
                           if n == "tape_bytes_processed_total" and lbl.get("direction") == direction})
            present = {lbl["parameter"]: v for n, lbl, v in rows
                       if n == "tape_log_parameter_present" and lbl.get("direction") == direction}
        else:
            values = {SA_BYTES.get((lbl.get("direction"), lbl.get("side")), "?"): int(v)
                      for n, lbl, v in rows if n == "tape_data_bytes_total"}
            values.update({"cleaning_action_required": int(v)
                           for n, _, v in rows if n == "tape_cleaning_required"})
            present = {lbl["field"]: v for n, lbl, v in rows
                       if n == "tape_sequential_access_field_present"}
        pages[page] = {"status": status[0] if len(status) == 1 else status,
                       "success": success, "values": values, "present": present}
    unreaped = [v for n, _, v in rows if n == "tape_exporter_unreaped_children"]
    return {"pages": pages, "unreaped": unreaped}


def harness_view(reads):
    """{page: (returncode, stdout)} from the harness's own sg_logs runs."""
    out = {}
    for page, (rc, text) in reads.items():
        status, values = sg_exit_status(rc), {}
        if status == "ok":
            _, direction = PAGES[page]
            parsed = (parse_sg_logs_error_counters(text, direction) if direction
                      else parse_sg_logs_sequential_access(text))
            status, values = parsed.status, parsed.values
        out[page] = {"status": status, "values": values}
    return out


def agreement_problems(snap, harness):
    """Exporter must say exactly what the harness's own read said."""
    problems = []
    if snap["unreaped"] != ["0"]:
        problems.append(f"tape_exporter_unreaped_children={snap['unreaped']}")
    for page, h in harness.items():
        e = snap["pages"][page]
        if e["status"] != h["status"]:
            problems.append(f"{page}: exporter status {e['status']!r} != harness {h['status']!r}")
        if e["success"] != ["1" if h["status"] == "ok" else "0"]:
            problems.append(f"{page}: collector_success {e['success']}")
        if h["status"] == "ok":
            if e["values"] != h["values"]:
                problems.append(f"{page}: exporter {e['values']} != harness {h['values']}")
        elif e["values"] or e["present"]:
            problems.append(f"{page}: read failed ({h['status']}) but exporter has "
                            f"values {e['values']} / present {e['present']}")
    return problems


# ---- sg_raw output -> outcome ----------------------------------------------

_SENSE_KEY = re.compile(r"Sense key:\s*([A-Za-z][A-Za-z ]*?)\s*(?:\n|$|\[)", re.M)


def classify_scsi(rc, text):
    """Outcome of one sg_raw command, from its exit status and decoded sense.

    ok           GOOD status
    ok_eom       CHECK CONDITION, NO SENSE + end-of-partition/medium (early
                 warning: the WRITE was accepted)
    ok_sense     CHECK CONDITION, NO SENSE, anything else
    overflow     VOLUME OVERFLOW (the WRITE was refused, medium full)
    data_protect DATA PROTECT (e.g. WORM overwrite refused)
    not_ready / unit_attention / blank_check / error:<key> / rc<N>
    """
    m = _SENSE_KEY.search(text or "")
    if m:
        key = m.group(1).strip().lower()
        if key == "no sense":
            return "ok_eom" if "end-of-partition/medium" in text.lower() else "ok_sense"
        return {"volume overflow": "overflow", "data protect": "data_protect",
                "not ready": "not_ready", "unit attention": "unit_attention",
                "blank check": "blank_check"}.get(key, f"error:{key}")
    return "ok" if rc == 0 else f"rc{rc}"


WRITE_ACCEPTED = ("ok", "ok_eom")


def be24(n):
    return [f"{(n >> s) & 0xff:02x}" for s in (16, 8, 0)]


# ---- the rig: real harness actions ------------------------------------------

class Rig:
    """Harness side. Runs as root; never used by or shared with the exporter."""

    def __init__(self, a):
        self.sg, self.sg_logs, self.url = a.sg, a.sg_logs, a.metrics_url
        self.bin = os.path.join(a.mhvtl_src, "usr", "bin")
        self.env = {**os.environ, "MHVTL_CONFIG_PATH": a.conf, "MHVTL_HOME_PATH": a.home,
                    "LD_LIBRARY_PATH": os.path.join(a.mhvtl_src, "usr")}
        self.conf, self.home, self.drive, self.lib = a.conf, a.home, a.drive_id, a.library_id
        self.evidence = a.evidence
        self.loaded = None
        self.state_path = os.path.join("/sys/class/scsi_generic", os.path.basename(a.sg),
                                       "device", "state")
        self.payload = os.path.join(a.work, "block.bin")
        with open(self.payload, "wb") as fh:
            fh.write(os.urandom(BLOCK))   # incompressible: media bytes ~ host bytes
        self.log = open(os.path.join(a.evidence, "harness_commands.log"), "a", encoding="utf-8")

    def run(self, argv, timeout=60, check=False):
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.env)
            rc, out = p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as exc:
            rc, out = -9, f"TIMEOUT after {timeout}s: {exc}"
        self.log.write(f"$ {' '.join(argv)}\n[rc={rc} {time.monotonic() - t0:.2f}s]\n{out}\n")
        self.log.flush()
        if check and rc != 0:
            raise HarnessError(f"{argv[0]} rc={rc}: {out.strip()[-400:]}")
        return rc, out

    def mktape(self, barcode, kind, size_mb):
        self.run([os.path.join(self.bin, "mktape"), "-C", self.conf, "-H", self.home,
                  "-l", str(self.lib), "-m", barcode, "-s", str(size_mb), "-t", kind,
                  "-d", "LTO8"], check=True)

    def vtlcmd(self, *args):
        self.run([os.path.join(self.bin, "vtlcmd"), str(self.drive), *args], check=True)

    def scsi(self, cdb, send=None, request=None, timeout=60):
        argv = ["sg_raw", "--timeout=50"]
        if send is not None:
            argv += [f"--send={send}", f"--infile={self.payload}"]
        if request is not None:
            argv += [f"--request={request}", "--outfile=/dev/null"]
        rc, out = self.run(argv + [self.sg] + cdb, timeout=timeout)
        return classify_scsi(rc, out), out

    def tur(self):
        return self.scsi(["00"] * 6)

    def wait_for(self, what, pred, timeout=90):
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            last = self.tur()
            if pred(*last):
                return last
            time.sleep(1)
        raise HarnessError(f"timed out waiting for {what}; last TUR: {last}")

    def load(self, barcode, cleaning=False):
        self.vtlcmd("load", barcode)
        self.loaded = barcode
        if cleaning:   # a cleaning cart never becomes ready; wait until it is in
            return self.wait_for(f"cleaning cart {barcode} mounted", lambda c, t:
                                 c == "not_ready" and "cleaning" in t.lower())
        return self.wait_for(f"{barcode} ready", lambda c, t: c == "ok")

    def unload(self):
        # vtlcmd insists on "unload ID"; vtltape itself only matches "unload".
        self.vtlcmd("unload", self.loaded or "NONE")
        self.loaded = None
        self.wait_for("medium removed", lambda c, t:
                      c == "not_ready" and "not present" in t.lower())

    def write(self):
        return self.scsi(["0a", "00", *be24(BLOCK), "00"], send=BLOCK)[0]

    def write_filemark(self):
        return self.scsi(["10", "00", "00", "00", "01", "00"])[0]

    def read(self):
        return self.scsi(["08", "00", *be24(BLOCK), "00"], request=BLOCK)[0]

    def rewind(self):
        return self.scsi(["01", "00", "00", "00", "00", "00"])[0]

    def space_eod(self):
        return self.scsi(["11", "03", "00", "00", "00", "00"])[0]

    def set_state(self, state):
        with open(self.state_path, "w") as fh:
            fh.write(state + "\n")   # scsi sysfs store wants the newline: EINVAL without
        self.log.write(f"# {self.state_path} <- {state}\n")
        with open(self.state_path) as fh:
            now = fh.read().strip()
        if now != state:
            raise HarnessError(f"device state is {now!r}, wanted {state!r}")

    def ack_tapealert(self):
        """What backup software does: read the TapeAlert page (0x2E). On mhvtl
        a full-length read clears the flags. The exporter never does this."""
        rc, out = self.run([self.sg_logs, "--readonly", "--page=0x2e", self.sg])
        if rc != 0:
            raise HarnessError(f"TapeAlert read failed rc={rc}")

    def harness_reads(self):
        reads = {}
        for page in PAGES:
            argv = [self.sg_logs, "--readonly"] + (["--verbose"] if page == "0xc" else [])
            p = subprocess.run(argv + [f"--page={page}", self.sg],
                               capture_output=True, text=True, timeout=60)
            reads[page] = (p.returncode, p.stdout)
        return reads

    def scrape(self):
        with urllib.request.urlopen(self.url, timeout=60) as r:
            return r.read().decode("utf-8")

    def recover(self):
        """Best effort between scenarios: back online, no media, flags acked."""
        for step in (lambda: self.set_state("running"),
                     lambda: self.wait_for("drive answering", lambda c, t: c in ("ok", "not_ready")),
                     self.unload, self.ack_tapealert):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - recovery is best effort
                self.log.write(f"# recover: {exc!r}\n")


# ---- scenario bookkeeping ----------------------------------------------------

class Run:
    def __init__(self, rig, evidence):
        self.rig, self.evidence = rig, evidence
        self.checks, self.scenario, self.n = [], None, 0

    def check(self, name, ok, detail=None):
        self.checks.append({"scenario": self.scenario, "check": name,
                            "result": "PASS" if ok else "FAIL", "detail": detail})
        return ok

    def snap(self, step):
        """Scrape the exporter, read the pages ourselves, require agreement."""
        self.n += 1
        text = self.rig.scrape()
        reads = self.rig.harness_reads()
        s, h = snapshot(text), harness_view(reads)
        if self.evidence:
            d = os.path.join(self.evidence, "scenarios", self.scenario)
            os.makedirs(d, exist_ok=True)
            base = os.path.join(d, f"{self.n:02d}-{step}")
            with open(base + ".metrics.txt", "w") as fh:
                fh.write(text)
            with open(base + ".json", "w") as fh:
                json.dump({"exporter": s, "harness": h, "harness_raw": reads}, fh, indent=2)
        p = agreement_problems(s, h)
        self.check(f"{step}: exporter agrees with harness sg_logs", not p, p)
        s["step"] = step
        return s

    def expect(self, s, *, status="ok", bytes_=None, cleaning="any", errors_zero=None):
        """Scenario expectations on one snapshot (beyond agreement)."""
        step = s["step"]
        got = {p: v["status"] for p, v in s["pages"].items()}
        self.check(f"{step}: every collector status == {status}",
                   all(v == status for v in got.values()), got)
        sa = s["pages"]["0xc"]["values"]
        if bytes_ is not None:
            want = {**ZERO_BYTES, **bytes_}   # unnamed fields must be 0
            have = {k: v for k, v in sa.items() if k != "cleaning_action_required"}
            ok = all(have.get(k) == v if isinstance(v, int) else (k in have and v(have[k]))
                     for k, v in want.items()) and set(have) == set(want)
            self.check(f"{step}: tape_data_bytes_total", ok,
                       {"have": have, "want": {k: v if isinstance(v, int) else v.__doc__
                                               for k, v in want.items()}})
        if cleaning != "any":
            have = sa.get("cleaning_action_required")
            self.check(f"{step}: tape_cleaning_required == {cleaning}", have == cleaning, have)
        if errors_zero is not None:
            ec = {p: s["pages"][p]["values"] for p in ("0x2", "0x3")}
            ok = all(v == {k: 0 for k in PARAM_LABELS} for v in ec.values())
            self.check(f"{step}: error counters all present and 0 (mhvtl static page)", ok, ec)
        return s


def positive(v):
    """> 0"""
    return v > 0


# ---- the scenarios -----------------------------------------------------------

def baseline(run, rig):
    """Empty drive straight after vtltape start."""
    run.expect(run.snap("empty"), bytes_={}, cleaning=0, errors_zero=True)


def load_unload(run, rig):
    rig.mktape("GWS001L8", "data", 64)
    rig.load("GWS001L8")
    run.expect(run.snap("loaded"), bytes_={}, cleaning=0, errors_zero=True)

    results = [rig.write() for _ in range(3)] + [rig.write_filemark()]
    if not run.check("harness: 3 WRITEs + WRITE FILEMARKS accepted", all(r == "ok" for r in results), results):
        raise HarnessError("writes refused")
    w = run.expect(run.snap("written"),
                   bytes_={"bytes_received_write": 3 * BLOCK, "bytes_written_media": positive},
                   cleaning=0, errors_zero=True)
    media_w = w["pages"]["0xc"]["values"]["bytes_written_media"]

    results = [rig.rewind()] + [rig.read() for _ in range(3)]
    if not run.check("harness: REWIND + 3 READs ok", all(r == "ok" for r in results), results):
        raise HarnessError("reads failed")
    after_read = {"bytes_received_write": 3 * BLOCK, "bytes_written_media": media_w,
                  "bytes_transferred_read": 3 * BLOCK, "bytes_read_media": positive}
    r = run.expect(run.snap("read_back"), bytes_=after_read, cleaning=0, errors_zero=True)
    kept = {k: r["pages"]["0xc"]["values"][k] for k in ZERO_BYTES}

    rig.unload()
    run.expect(run.snap("unloaded"), bytes_=kept, cleaning=0, errors_zero=True)
    rig.load("GWS001L8")
    run.expect(run.snap("reloaded_counters_reset"), bytes_={}, cleaning=0, errors_zero=True)
    rig.unload()


def full_tape(run, rig):
    rig.mktape("GWS002L8", "data", FULL_TAPE_MB)
    rig.load("GWS002L8")
    outcomes = []
    for _ in range(MAX_FULL_TAPE_WRITES):
        outcomes.append(rig.write())
        if outcomes[-1] not in WRITE_ACCEPTED:
            break
    accepted = sum(1 for o in outcomes if o in WRITE_ACCEPTED)
    summary = {"writes": len(outcomes), "accepted": accepted,
               "early_warning": outcomes.count("ok_eom"), "last": outcomes[-1]}
    run.check("harness: WRITEs accepted, then early warning, then VOLUME OVERFLOW",
              outcomes[-1] == "overflow" and "ok_eom" in outcomes
              and all(o in WRITE_ACCEPTED for o in outcomes[:-1]), summary)
    run.expect(run.snap("medium_full"),
               bytes_={"bytes_received_write": accepted * BLOCK, "bytes_written_media": positive},
               cleaning=0, errors_zero=True)
    run.check("harness: WRITE at end of medium still refused", rig.write() == "overflow")
    run.expect(run.snap("refused_write_not_counted"),
               bytes_={"bytes_received_write": accepted * BLOCK, "bytes_written_media": positive},
               cleaning=0)
    rig.unload()


def cleaning_cartridge(run, rig):
    rig.ack_tapealert()
    run.expect(run.snap("before"), cleaning=0)   # bytes: whatever the last load left
    rig.mktape("GWC001L8", "clean", 64)
    rig.load("GWC001L8", cleaning=True)
    run.expect(run.snap("cleaning_cart_mounted"), bytes_={}, cleaning=1, errors_zero=True)
    run.check("harness: WRITE to cleaning cart refused", rig.write() == "not_ready")
    for i in (1, 2):   # the exporter must not clear the flag by reading
        run.expect(run.snap(f"rescrape_{i}"), cleaning=1)
    rig.unload()
    run.expect(run.snap("unloaded_flag_sticky"), bytes_={}, cleaning=1)
    rig.ack_tapealert()
    run.expect(run.snap("host_read_tapealert"), bytes_={}, cleaning=0)


def drive_offline(run, rig):
    rig.mktape("GWS003L8", "data", 64)
    rig.load("GWS003L8")
    run.check("harness: WRITE accepted", rig.write() == "ok")
    run.expect(run.snap("online"), bytes_={"bytes_received_write": BLOCK,
                                           "bytes_written_media": positive})
    rig.set_state("offline")
    try:
        # sg_open() refuses an offline device with ENXIO; sg_logs exits 50+6.
        off = run.expect(run.snap("offline"), status="os_error")
        run.check("offline: no counters, flags or present series exported",
                  not any(p["values"] or p["present"] for p in off["pages"].values()),
                  {p: (v["values"], v["present"]) for p, v in off["pages"].items()})
        run.expect(run.snap("offline_again"), status="os_error")
    finally:
        rig.set_state("running")
    rig.wait_for("drive answering after online", lambda c, t: c == "ok")
    run.expect(run.snap("back_online"), bytes_={"bytes_received_write": BLOCK,
                                                "bytes_written_media": positive})
    rig.unload()


def worm(run, rig):
    rig.ack_tapealert()
    rig.mktape("GWW001L8", "WORM", 64)
    rig.load("GWW001L8")
    results = [rig.write(), rig.write(), rig.write_filemark()]
    run.check("harness: append to blank WORM accepted", all(r == "ok" for r in results), results)
    run.expect(run.snap("appended"), bytes_={"bytes_received_write": 2 * BLOCK,
                                             "bytes_written_media": positive}, cleaning=0)
    run.check("harness: REWIND ok", rig.rewind() == "ok")
    run.check("harness: overwrite at BOT refused with DATA PROTECT",
              rig.write() == "data_protect")
    run.expect(run.snap("overwrite_refused_not_counted"),
               bytes_={"bytes_received_write": 2 * BLOCK, "bytes_written_media": positive},
               cleaning=0)
    run.check("harness: SPACE to EOD ok", rig.space_eod() == "ok")
    run.check("harness: append at EOD accepted", rig.write() == "ok")
    run.expect(run.snap("appended_at_eod"),
               bytes_={"bytes_received_write": 3 * BLOCK, "bytes_written_media": positive},
               cleaning=0, errors_zero=True)
    rig.unload()


SCENARIOS = {"baseline": baseline, "load_unload": load_unload, "full_tape": full_tape,
             "cleaning_cartridge": cleaning_cartridge, "drive_offline": drive_offline,
             "worm": worm}


def run_scenarios(rig, evidence, names=None):
    run = Run(rig, evidence)
    outcome = {}
    for name in dict.fromkeys(names or SCENARIOS):
        run.scenario = name
        before = len(run.checks)
        try:
            SCENARIOS[name](run, rig)
            err = None
        except Exception as exc:  # noqa: BLE001 - one scenario never stops the rest
            err = f"{type(exc).__name__}: {exc}"
            run.check("scenario ran to completion", False,
                      {"error": err, "traceback": traceback.format_exc()[-2000:]})
            rig.recover()
        mine = run.checks[before:]
        outcome[name] = {"result": "FAIL" if any(c["result"] == "FAIL" for c in mine) else "PASS",
                         "checks": len(mine), "error": err}
    return run.checks, outcome


def write_summary(evidence, outcome, checks, strace_checks):
    overall = ("PASS" if all(o["result"] == "PASS" for o in outcome.values())
               and all(c["result"] == "PASS" for c in strace_checks) else "FAIL")
    summary = {"overall": overall, "scenarios": outcome, "checks": checks,
               "exporter_strace": strace_checks}
    with open(os.path.join(evidence, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    lines = [f"# mhvtl scenarios: {overall}", "", "| scenario | result | checks |", "|---|---|---|"]
    lines += [f"| {n} | {o['result']} | {o['checks']} |" for n, o in outcome.items()]
    lines += ["", "| exporter strace (whole run) | result |", "|---|---|"]
    lines += [f"| {c['check']} | {c['result']} |" for c in strace_checks]
    failed = [c for c in checks if c["result"] == "FAIL"]
    if failed:
        lines += ["", "## failed checks", ""]
        lines += [f"- `{c['scenario']}` {c['check']}: `{json.dumps(c['detail'])[:600]}`" for c in failed]
    with open(os.path.join(evidence, "summary.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return overall


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for opt in ("sg", "sg-logs", "metrics-url", "mhvtl-src", "conf", "home", "work", "evidence"):
        ap.add_argument(f"--{opt}", required=True)
    ap.add_argument("--drive-id", type=int, required=True)
    ap.add_argument("--library-id", type=int, required=True)
    ap.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    a = ap.parse_args(argv)
    rig = Rig(a)
    checks, outcome = run_scenarios(rig, a.evidence, a.only)
    with open(os.path.join(a.evidence, "scenario_checks.json"), "w") as fh:
        json.dump({"scenarios": outcome, "checks": checks}, fh, indent=2)
    return 0


def verdict(argv=None):
    """After the exporter has stopped: fold in the strace verdict."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--sg", required=True)
    ap.add_argument("--sg-logs", required=True)
    a = ap.parse_args(argv)
    try:
        with open(os.path.join(a.evidence, "scenario_checks.json")) as fh:
            sc = json.load(fh)
    except OSError:
        sc = {"scenarios": {"(none)": {"result": "FAIL", "checks": 0,
                                       "error": "scenario_checks.json missing"}}, "checks": []}
    with open(os.path.join(a.evidence, "strace.txt"), encoding="utf-8", errors="replace") as fh:
        st = analyse_strace(fh.read(), a.sg, a.sg_logs).checks
    return 0 if write_summary(a.evidence, sc["scenarios"], sc["checks"], st) == "PASS" else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "verdict":
        sys.exit(verdict(sys.argv[2:]))
    sys.exit(main())
