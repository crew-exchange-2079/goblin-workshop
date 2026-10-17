#!/usr/bin/env python3
"""Run the mhvtl log-page regression kit in fresh, disposable upstream checkouts.

Public developer test artifact; GPL-2.0-or-later (see LICENSE-PROVENANCE.md).
Stdlib only. No sudo, kernel modules, daemons, hardware or installs. Builds only
inside a new temporary directory; never touches a caller's existing checkout.

Reports four results separately: DIRECT_REGRESSION, EXISTING_SUITE, SANITIZERS,
MUTATION_CONTROL, each PASS / FAIL / BLOCKED / NOT_RUN. Exit 0 only when all
four PASS (FULL_ACCEPTANCE); 1 = PARTIAL/FAIL; 2 = preflight blocker.
"""
import argparse, hashlib, json, os, re, select, shutil, signal, subprocess, sys, tempfile, time

KIT = os.path.dirname(os.path.realpath(__file__))
DEFAULT_UPSTREAM = "https://github.com/markh794/mhvtl.git"
BASE = "7ca971131c3e68d3a37073b83d235848dfa46559"
ALLOWED = {"tests/Makefile", "tests/test_log_device_status.c"}
SAN = "-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=undefined"
CASES = ["empty_list_is_safe", "tape_alert_without_other_pages",
         "device_status_page_present", "sequential_access_field"]

class Deadline(Exception):
    pass

class Ctx:
    def __init__(self, a):
        self.a = a
        self.t_end = time.monotonic() + a.deadline
        self.work = None
        self.logs = {}      # name -> path
        self.results = {k: ["NOT_RUN", "not reached"] for k in
                        ("DIRECT_REGRESSION", "EXISTING_SUITE", "SANITIZERS", "MUTATION_CONTROL")}
        self.prov = {"commands": []}

    def left(self):
        return self.t_end - time.monotonic()

def scrub(ctx, text):
    text = text.replace(KIT, "<KIT>")
    return text.replace(ctx.work, "<WORK>") if ctx.work else text

def run(ctx, name, cmd, cwd=None, env=None, step_max=600, shell=False):
    """Run cmd with a hard timeout (min of step_max and the global deadline),
    streaming output to a capped log. Returns (rc, tail_text). rc=-9 on timeout."""
    if ctx.left() <= 0:
        raise Deadline(name)
    to = min(step_max, ctx.left())
    path = os.path.join(ctx.work, "logs", name + ".log")
    ctx.logs[name] = path
    cap = ctx.a.log_cap_kb * 1024
    e = dict(os.environ)
    e.update({"LC_ALL": "C", "HOME": os.path.join(ctx.work, "home"),
              "TMPDIR": os.path.join(ctx.work, "tmp")})
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD", "RPM_OPT_FLAGS", "CFLAGS", "CC",
              "ASAN_OPTIONS", "UBSAN_OPTIONS"):
        e.pop(k, None)
    if env:
        e.update(env)
    ctx.prov["commands"].append(scrub(ctx, (cmd if isinstance(cmd, str) else " ".join(cmd))))
    p = subprocess.Popen(cmd, cwd=cwd, env=e, shell=shell, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    written = dropped = 0
    t0 = time.monotonic()
    timed_out = False
    with open(path, "wb") as f:
        while True:
            r = to - (time.monotonic() - t0)
            if r <= 0:
                timed_out = True
                break
            rl, _, _ = select.select([p.stdout], [], [], min(r, 1.0))
            if rl:
                b = os.read(p.stdout.fileno(), 65536)
                if not b:
                    break
                keep = max(0, min(len(b), cap - written))
                f.write(b[:keep]); written += keep; dropped += len(b) - keep
    if timed_out:
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL); p.wait()
    try: os.killpg(p.pid, signal.SIGKILL)   # sweep stragglers in the group
    except ProcessLookupError: pass
    with open(path, "ab") as f:
        if dropped: f.write(("\n[log truncated: %d bytes dropped]\n" % dropped).encode())
        if timed_out: f.write(("\n[TIMEOUT after %.0fs]\n" % to).encode())
    rc = -9 if timed_out else p.returncode
    with open(path, "rb") as f:
        txt = f.read().decode("utf-8", "replace")
    return rc, txt

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def setres(ctx, k, status, why):
    ctx.results[k] = [status, why]

def probe(ctx, name, code, extra=()):
    src = os.path.join(ctx.work, "tmp", name + ".c")
    with open(src, "w") as f:
        f.write(code)
    rc, out = run(ctx, "probe-" + name, ["gcc", *extra, "-o", os.path.join(ctx.work, "tmp", name), src],
                  step_max=60)
    return rc == 0, out

def checkout(ctx, name, src):
    d = os.path.join(ctx.work, name)
    rc, out = run(ctx, "clone-" + name, ["git", "clone", "--quiet", "--no-hardlinks", src, d], step_max=300)
    if rc: raise RuntimeError("clone %s failed: %s" % (name, out[-300:]))
    rc, out = run(ctx, "checkout-" + name, ["git", "-C", d, "checkout", "--quiet", "--detach", BASE])
    if rc: raise RuntimeError("checkout %s failed: %s" % (name, out[-300:]))
    return d

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="git URL or local path of markh794/mhvtl (read only)")
    ap.add_argument("--work", help="NEW or empty directory to use instead of a fresh mkdtemp")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    ap.add_argument("--evidence-dir", help="write compact summary.json + trimmed logs here")
    ap.add_argument("--deadline", type=int, default=1200, help="one overall deadline in seconds (default 1200)")
    ap.add_argument("--log-cap-kb", type=int, default=256, help="per-command log cap (default 256)")
    ap.add_argument("--skip-suite", action="store_true", help="do not run the existing `make test` (EXISTING_SUITE=NOT_RUN)")
    a = ap.parse_args()
    ctx = Ctx(a)
    created = False
    if a.work:
        w = os.path.abspath(a.work)
        if os.path.exists(w) and os.listdir(w):
            print("refusing: --work %s exists and is not empty" % w, file=sys.stderr); return 2
        os.makedirs(w, exist_ok=True); created = True
        ctx.work = w
    else:
        ctx.work = os.path.realpath(tempfile.mkdtemp(prefix="mhvtl-status-")); created = True
    for s in ("logs", "home", "tmp"):
        os.makedirs(os.path.join(ctx.work, s), exist_ok=True)
    rc = 1
    try:
        rc = body(ctx)
    except Deadline as e:
        print("DEADLINE reached during %s; remaining results stay NOT_RUN" % e)
        rc = 1
    except Exception as e:
        print("ERROR: %s" % e); rc = 2
    finally:
        report(ctx)
        if created and not a.keep:
            shutil.rmtree(ctx.work, ignore_errors=True)
        elif a.keep:
            print("work dir kept: %s" % ctx.work)
    full = all(v[0] == "PASS" for v in ctx.results.values())
    print("FULL_ACCEPTANCE" if full else "PARTIAL (not all four PASS)")
    return 0 if full else (rc if rc else 1)

def body(ctx):
    a = ctx.a
    # ---- preflight: precise errors, never a false PASS
    missing = [t for t in ("git", "gcc", "make") if not shutil.which(t)]
    if missing:
        for k in ctx.results: setres(ctx, k, "BLOCKED", "missing tools: " + ", ".join(missing))
        print("BLOCKED: required tool(s) not found on PATH: " + ", ".join(missing)); return 2
    for t, args in (("git", ["--version"]), ("gcc", ["--version"]), ("make", ["--version"])):
        _, o = run(ctx, "ver-" + t, [t] + args, step_max=30)
        ctx.prov[t] = o.splitlines()[0] if o else "?"
    ctx.prov["python"] = sys.version.split()[0]
    ok_san, _ = probe(ctx, "san", "int main(void){return 0;}\n", SAN.split())
    udev, _ = probe(ctx, "udev", "#include <libudev.h>\nint main(void){return 0;}\n")
    hdrs = {}
    for h in ("lz4.h", "zstd.h", "lzo/lzo1x.h", "libudev.h"):
        hdrs[h] = probe(ctx, "h" + re.sub(r"\W", "", h), "#include <%s>\nint main(void){return 0;}\n" % h)[0]
    # ---- fresh upstream checkout, literal base
    base = os.path.join(ctx.work, "upstream")
    rc, out = run(ctx, "clone-upstream", ["git", "clone", "--quiet", "--no-checkout", a.upstream, base], step_max=300)
    if rc:
        for k in ctx.results: setres(ctx, k, "BLOCKED", "cannot clone upstream %s" % a.upstream)
        print("BLOCKED: clone of %s failed:\n%s" % (a.upstream, out[-400:])); return 2
    rc, _ = run(ctx, "verify-base", ["git", "-C", base, "cat-file", "-e", BASE + "^{commit}"], step_max=30)
    rc2, o2 = run(ctx, "verify-file", ["git", "-C", base, "show", BASE + ":usr/mhvtl_log.c"], step_max=30)
    if rc or rc2 or "lookup_device_status_pg" not in o2 or "mhvtl" not in o2:
        for k in ctx.results: setres(ctx, k, "BLOCKED", "pin %s absent or not mhvtl" % BASE)
        print("BLOCKED: %s does not contain pinned commit %s / expected mhvtl source" % (a.upstream, BASE)); return 2
    _, head = run(ctx, "upstream-head", ["git", "-C", base, "rev-parse", "HEAD"], step_max=30)
    ctx.prov["upstream"] = a.upstream
    ctx.prov["pinned_base"] = BASE
    ctx.prov["upstream_default_head"] = head.strip()
    ctx.prov["upstream_head_moved_past_pin"] = head.strip() != BASE
    patch = os.path.join(KIT, "tests.patch")
    mut = os.path.join(KIT, "mutation-missing-guard.diff")
    ctx.prov["tests.patch_sha256"] = sha(patch)
    ctx.prov["mutation_diff_sha256"] = sha(mut)
    rc, ns = run(ctx, "patch-numstat", ["git", "apply", "--numstat", patch], cwd=os.path.join(ctx.work, "tmp"), step_max=30)
    paths = {l.split("\t")[2] for l in ns.splitlines() if l.count("\t") >= 2}
    if rc or paths != ALLOWED:
        for k in ctx.results: setres(ctx, k, "BLOCKED", "tests.patch touches unexpected paths")
        print("BLOCKED: tests.patch paths %s != %s" % (sorted(paths), sorted(ALLOWED))); return 2

    def tree(name):
        d = checkout(ctx, name, base)
        rc, o = run(ctx, "apply-" + name, ["git", "-C", d, "apply", patch], step_max=30)
        if rc: raise RuntimeError("tests.patch does not apply on %s: %s" % (BASE, o[-300:]))
        return d
    norm = tree("normal")
    # ---- 1. DIRECT_REGRESSION
    t = "direct"
    rc, o = run(ctx, "n-lib", ["make", "-C", "usr", "libvtlscsi.so"], cwd=norm, step_max=300)
    if rc:
        setres(ctx, "DIRECT_REGRESSION", "BLOCKED", "libvtlscsi.so build failed (see logs/n-lib.log)")
    else:
        rc, o = run(ctx, "n-test-build", ["make", "-C", "tests", "test_log_device_status"], cwd=norm, step_max=300)
        if rc:
            setres(ctx, "DIRECT_REGRESSION", "BLOCKED", "test link failed (see logs/n-test-build.log)")
        else:
            libdir = os.path.join(norm, "usr")
            exe = os.path.join(norm, "tests", "test_log_device_status")
            _, ldd = run(ctx, "n-ldd", ["ldd", exe], env={"LD_LIBRARY_PATH": libdir}, step_max=30)
            m = re.search(r"libvtlscsi\.so => (\S+)", ldd)
            resolved = os.path.realpath(m.group(1)) if m else "?"
            ctx.prov["normal_resolved_lib"] = scrub(ctx, resolved)
            ctx.prov["normal_lib_sha256"] = sha(os.path.join(libdir, "libvtlscsi.so"))
            ctx.prov["normal_test_sha256"] = sha(exe)
            rc, o = run(ctx, "n-direct", [exe], cwd=os.path.join(norm, "tests"),
                        env={"LD_LIBRARY_PATH": libdir}, step_max=120)
            good = rc == 0 and all(re.search(r"Test %s\.\.\.\s+\[ OK \]" % c, o) for c in CASES)
            if resolved != os.path.join(libdir, "libvtlscsi.so"):
                setres(ctx, "DIRECT_REGRESSION", "FAIL", "test resolved %s, not the build-tree library" % resolved)
            elif good:
                setres(ctx, "DIRECT_REGRESSION", "PASS", "4/4 cases OK against build-tree libvtlscsi.so, rc=0")
            else:
                setres(ctx, "DIRECT_REGRESSION", "FAIL", "rc=%s; see logs/n-direct.log" % rc)
    # ---- 2. EXISTING_SUITE
    if a.skip_suite:
        setres(ctx, "EXISTING_SUITE", "NOT_RUN", "--skip-suite")
    elif ctx.results["DIRECT_REGRESSION"][0] == "BLOCKED":
        setres(ctx, "EXISTING_SUITE", "BLOCKED", "library build failed")
    else:
        lack = [h for h, ok in hdrs.items() if not ok]
        if lack:
            setres(ctx, "EXISTING_SUITE", "BLOCKED",
                   "missing dev headers: %s (install lz4/zstd/lzo2/libudev dev packages)" % ", ".join(lack))
        else:
            rc1, _ = run(ctx, "n-etc", ["make", "-C", "etc"], cwd=norm, step_max=120)
            rc2, _ = run(ctx, "n-usr-all", ["make", "-C", "usr"], cwd=norm, step_max=600)
            if rc1 or rc2:
                setres(ctx, "EXISTING_SUITE", "BLOCKED", "etc/ or usr/ prerequisite build failed (logs/n-etc.log, n-usr-all.log)")
            else:
                rc, o = run(ctx, "n-suite", ["make", "-C", "tests", "test"], cwd=norm, step_max=600)
                if rc == 0 and "All test suites passed" in o and re.search(
                        r"--- test_log_device_status ---\s*(?:.*\n)*?SUCCESS", o):
                    setres(ctx, "EXISTING_SUITE", "PASS", "`make -C tests test` rc=0, 'All test suites passed'")
                else:
                    setres(ctx, "EXISTING_SUITE", "FAIL", "rc=%s; see logs/n-suite.log" % rc)
    _, gd = run(ctx, "n-diffnames", ["git", "-C", norm, "diff", "--name-only"], step_max=30)
    _, gu = run(ctx, "n-untracked", ["git", "-C", norm, "ls-files", "--others", "--exclude-standard", "--", "tests/*.c", "usr/*.c", "include"], step_max=30)
    ctx.prov["normal_tree_modified_tracked"] = gd.split()
    ctx.prov["normal_tree_new_source_files"] = gu.split()
    # ---- 3. SANITIZERS (production objects + harness)
    if not ok_san:
        setres(ctx, "SANITIZERS", "BLOCKED", "gcc cannot link -fsanitize=address,undefined (install libasan/libubsan)")
    else:
        san = tree("san")
        rc, o = run(ctx, "s-lib", ["make", "-C", "usr", "libvtlscsi.so", "RPM_OPT_FLAGS=" + SAN], cwd=san, step_max=300)
        rc2 = 1
        if not rc:
            rc2, o = run(ctx, "s-test-build", ["make", "-C", "tests", "test_log_device_status", "CC=gcc " + SAN],
                         cwd=san, step_max=300)
        if rc or rc2:
            setres(ctx, "SANITIZERS", "BLOCKED", "sanitizer build failed (logs/s-lib.log, s-test-build.log)")
        else:
            res = sanitizer_proof(ctx, san)
            setres(ctx, "SANITIZERS", *res)
    # ---- 4. MUTATION_CONTROL (separate checkout, diagnostic only)
    if ctx.results["SANITIZERS"][0] != "PASS":
        setres(ctx, "MUTATION_CONTROL", "NOT_RUN" if ctx.results["SANITIZERS"][0] == "NOT_RUN" else "BLOCKED",
               "needs a passing sanitizer baseline")
    else:
        m = tree("mutant")
        rc, o = run(ctx, "m-apply", ["git", "-C", m, "apply", mut], step_max=30)
        rc2, od = run(ctx, "m-diff", ["git", "-C", m, "diff", "--stat", "--", "."], step_max=30)
        changed_files = [l for l in od.splitlines() if "|" in l]
        if rc or len(changed_files) != 2 or not any("usr/mhvtl_log.c" in l for l in changed_files):
            setres(ctx, "MUTATION_CONTROL", "BLOCKED", "mutation.diff did not apply cleanly")
        else:
            r1, _ = run(ctx, "m-lib", ["make", "-C", "usr", "libvtlscsi.so", "RPM_OPT_FLAGS=" + SAN], cwd=m, step_max=300)
            r2 = 1
            if not r1:
                r2, _ = run(ctx, "m-test-build", ["make", "-C", "tests", "test_log_device_status", "CC=gcc " + SAN],
                            cwd=m, step_max=300)
            if r1 or r2:
                setres(ctx, "MUTATION_CONTROL", "BLOCKED", "mutant build failed (logs/m-lib.log, m-test-build.log)")
            else:
                exe = os.path.join(m, "tests", "test_log_device_status")
                rc, o = run(ctx, "m-run", [exe, "--no-exec", "empty_list_is_safe"], cwd=os.path.join(m, "tests"),
                            env={"LD_LIBRARY_PATH": os.path.join(m, "usr"),
                                 "ASAN_OPTIONS": "detect_leaks=1",
                                 "UBSAN_OPTIONS": "print_stacktrace=1"}, step_max=120)
                setup_fail = re.search(r"error while loading shared|undefined symbol|cannot open shared", o)
                in_path = "mhvtl_log.c:" in o and re.search(r"null pointer|SEGV|AddressSanitizer", o)
                ctx.prov["mutant_lib_sha256"] = sha(os.path.join(m, "usr", "libvtlscsi.so"))
                if rc != 0 and rc != -9 and in_path and not setup_fail:
                    line = next((l for l in o.splitlines() if "mhvtl_log.c:" in l), "")
                    setres(ctx, "MUTATION_CONTROL", "PASS", "mutant fails rc=%d in production path: %s" % (rc, scrub(ctx, line.strip())[:160]))
                elif rc == 0:
                    setres(ctx, "MUTATION_CONTROL", "FAIL", "mutant unexpectedly passed (test insensitive)")
                else:
                    setres(ctx, "MUTATION_CONTROL", "FAIL", "failed rc=%s but not in the intended path; see logs/m-run.log" % rc)
    return 0

def sanitizer_proof(ctx, san):
    lib = os.path.join(san, "usr", "libvtlscsi.so")
    obj = os.path.join(san, "usr", "mhvtl_log.o")
    exe = os.path.join(san, "tests", "test_log_device_status")
    _, nmu = run(ctx, "s-nm-obj", ["nm", "-u", obj], step_max=30)
    asan = len(re.findall(r"__asan_(?:report|load|store)", nmu)); ub = len(re.findall(r"__ubsan_handle", nmu))
    _, nl = run(ctx, "s-readelf-lib", ["readelf", "-d", lib], step_max=30)
    _, ne = run(ctx, "s-readelf-exe", ["readelf", "-d", exe], step_max=30)
    ctx.prov.update({"san_mhvtl_log_o_asan_refs": asan, "san_mhvtl_log_o_ubsan_refs": ub,
                     "san_lib_needs_asan": "libasan" in nl, "san_exe_needs_asan": "libasan" in ne,
                     "san_lib_sha256": sha(lib), "san_test_sha256": sha(exe), "san_mhvtl_log_o_sha256": sha(obj)})
    if not (asan and ub):
        return "FAIL", "production object mhvtl_log.o lacks ASan/UBSan instrumentation"
    libdir = os.path.join(san, "usr")
    _, ldd = run(ctx, "s-ldd", ["ldd", exe], env={"LD_LIBRARY_PATH": libdir}, step_max=30)
    m = re.search(r"libvtlscsi\.so => (\S+)", ldd)
    ctx.prov["san_resolved_lib"] = scrub(ctx, os.path.realpath(m.group(1))) if m else "?"
    rc, o = run(ctx, "s-run", [exe], cwd=os.path.join(san, "tests"),
                env={"LD_LIBRARY_PATH": libdir, "ASAN_OPTIONS": "detect_leaks=1",
                     "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1"}, step_max=180)
    diag = re.search(r"AddressSanitizer|LeakSanitizer|runtime error:", o)
    good = rc == 0 and not diag and all(re.search(r"Test %s\.\.\.\s+\[ OK \]" % c, o) for c in CASES)
    if not m or os.path.realpath(m.group(1)) != os.path.join(libdir, "libvtlscsi.so"):
        return "FAIL", "sanitizer test did not resolve the sanitizer build-tree library"
    if good:
        return "PASS", ("4/4 OK, rc=0, no sanitizer diagnostics; mhvtl_log.o has %d asan + %d ubsan refs, lib and exe link libasan"
                        % (asan, ub))
    return "FAIL", "rc=%s diagnostics=%s; see logs/s-run.log" % (rc, bool(diag))

def report(ctx):
    print("\n==== RESULTS ====")
    for k, (s, why) in ctx.results.items():
        print("%-18s %-8s %s" % (k, s, scrub(ctx, why)))
    ev = ctx.a.evidence_dir
    if not ev: return
    os.makedirs(ev, exist_ok=True)
    summ = {"results": {k: {"status": s, "evidence": scrub(ctx, w)} for k, (s, w) in ctx.results.items()},
            "provenance": ctx.prov, "deadline_s": ctx.a.deadline}
    with open(os.path.join(ev, "summary.json"), "w") as f:
        json.dump(summ, f, indent=2, sort_keys=True); f.write("\n")
    keep = re.compile(r" -o (mhvtl_log\.o|libvtlscsi\.so|test_log_device_status)\b|^cc -shared")
    cmds = []
    for n in ("n-lib", "n-test-build", "s-lib", "s-test-build", "m-lib", "m-test-build"):
        p = ctx.logs.get(n)
        if p and os.path.exists(p):
            ls = open(p, errors="replace").read().splitlines()
            for i, l in enumerate(ls):
                if keep.search(l):
                    if l.endswith("\\") and i + 1 < len(ls):
                        l = l[:-1] + ls[i + 1].strip()
                    cmds.append("[%s] %s" % (n, scrub(ctx, l.rstrip())))
    with open(os.path.join(ev, "build-commands.txt"), "w") as f:
        f.write("\n".join(cmds) + "\n")
    for n in ("n-direct", "n-suite", "s-run", "m-run", "m-diff", "s-nm-obj", "n-ldd", "s-ldd"):
        p = ctx.logs.get(n)
        if p and os.path.exists(p):
            lines = open(p, errors="replace").read().splitlines()
            if len(lines) > 120:
                lines = lines[:20] + ["[... %d lines elided ...]" % (len(lines) - 80)] + lines[-60:]
            with open(os.path.join(ev, n + ".log"), "w") as f:
                f.write(scrub(ctx, "\n".join(lines)) + "\n")

if __name__ == "__main__":
    sys.exit(main())
