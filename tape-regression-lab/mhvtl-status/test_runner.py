#!/usr/bin/env python3
"""Stdlib tests for run.py (imports the real runner). No network, build, hardware
or API calls. Fixtures are test-owned temp dirs only. Run under a finite timeout:
    timeout 120 python3 -I test_runner.py
"""
import argparse, contextlib, io, os, signal, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import run as R


def quiet_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = R.main(argv)
    return rc, out.getvalue() + err.getvalue()


def mkctx(base, deadline=20, cap_kb=1):
    ctx = R.Ctx(argparse.Namespace(deadline=deadline, log_cap_kb=cap_kb))
    ctx.work = base
    for d in ("logs", "home", "tmp"):
        os.makedirs(os.path.join(base, d), exist_ok=True)
    return ctx


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # zombie counts as dead
        return open("/proc/%d/stat" % pid).read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rt-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def sentinel(self, d):
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "keep.txt")
        open(p, "w").write("caller data")
        return p

    def intact(self, p):
        self.assertEqual(open(p).read(), "caller data")


class Destinations(Base):
    def test_existing_empty_work_refused_and_kept(self):
        w = os.path.join(self.tmp, "w"); os.mkdir(w)
        rc, out = quiet_main(["--work", w])
        self.assertEqual(rc, 2); self.assertTrue(os.path.isdir(w))

    def test_existing_nonempty_work_refused_sentinel_kept(self):
        w = os.path.join(self.tmp, "w"); s = self.sentinel(w)
        rc, _ = quiet_main(["--work", w])
        self.assertEqual(rc, 2); self.intact(s)

    def test_work_file_symlink_dangling_refused(self):
        f = os.path.join(self.tmp, "f"); open(f, "w").write("caller data")
        self.assertEqual(quiet_main(["--work", f])[0], 2); self.intact(f)
        l = os.path.join(self.tmp, "dangling"); os.symlink(os.path.join(self.tmp, "nowhere"), l)
        self.assertEqual(quiet_main(["--work", l])[0], 2)
        self.assertTrue(os.path.islink(l)); self.assertFalse(os.path.exists(os.path.join(self.tmp, "nowhere")))

    def test_evidence_existing_dir_file_symlink_refused(self):
        d = os.path.join(self.tmp, "ev"); s = self.sentinel(d)
        self.assertEqual(quiet_main(["--evidence-dir", d])[0], 2); self.intact(s)
        e = os.path.join(self.tmp, "empty"); os.mkdir(e)
        self.assertEqual(quiet_main(["--evidence-dir", e])[0], 2); self.assertEqual(os.listdir(e), [])
        f = os.path.join(self.tmp, "evfile"); open(f, "w").write("caller data")
        self.assertEqual(quiet_main(["--evidence-dir", f])[0], 2); self.intact(f)
        target = os.path.join(self.tmp, "target"); ts = self.sentinel(target)
        l = os.path.join(self.tmp, "evlink"); os.symlink(target, l)
        self.assertEqual(quiet_main(["--evidence-dir", l])[0], 2); self.intact(ts)

    def test_symlink_ancestor_refused(self):
        real = os.path.join(self.tmp, "real"); s = self.sentinel(real)
        link = os.path.join(self.tmp, "link"); os.symlink(real, link)
        for flag in ("--evidence-dir", "--work"):
            rc, out = quiet_main([flag, os.path.join(link, "new")])
            self.assertEqual(rc, 2); self.assertIn("symlink", out)
        self.intact(s); self.assertEqual(sorted(os.listdir(real)), ["keep.txt"])

    def test_overlap_refused(self):
        a = os.path.join(self.tmp, "a")
        for w, e in ((a, a), (a, os.path.join(a, "ev")), (os.path.join(a, "w"), a)):
            rc, out = quiet_main(["--work", w, "--evidence-dir", e])
            self.assertEqual(rc, 2); self.assertIn("overlap", out)
        self.assertFalse(os.path.exists(a))

    def test_new_paths_created_and_only_own_work_removed(self):
        w = os.path.join(self.tmp, "w"); e = os.path.join(self.tmp, "e")
        orig = R.body
        R.body = lambda ctx: 1   # no build/network
        self.addCleanup(setattr, R, "body", orig)
        rc, _ = quiet_main(["--work", w, "--evidence-dir", e])
        self.assertFalse(os.path.exists(w))
        self.assertTrue(os.path.isfile(os.path.join(e, "summary.json")))
        self.assertNotEqual(rc, 0)   # nothing PASSed

    def test_report_error_still_cleans_up_and_fails(self):
        w = os.path.join(self.tmp, "w")
        o_b, o_r = R.body, R.report
        R.body = lambda ctx: 0
        def boom(ctx): raise RuntimeError("report exploded")
        R.report = boom
        self.addCleanup(setattr, R, "body", o_b); self.addCleanup(setattr, R, "report", o_r)
        rc, out = quiet_main(["--work", w])
        self.assertFalse(os.path.exists(w)); self.assertNotEqual(rc, 0)
        self.assertIn("REPORT ERROR", out)

    def test_evidence_files_exclusive(self):
        e = os.path.join(self.tmp, "e"); os.mkdir(e)
        pre = os.path.join(e, "summary.json"); open(pre, "w").write("caller data")
        ctx = mkctx(os.path.join(self.tmp, "w")); ctx.a.evidence_dir = e
        with self.assertRaises(FileExistsError):
            R.report(ctx)
        self.intact(pre)


class SanitizerPredicate(Base):
    """sanitizer_proof() must not PASS without probe success and runtime evidence."""
    GOOD = {"s-nm-obj": (0, "U __asan_report_load4\nU __ubsan_handle_x\n"),
            "s-readelf-lib": (0, " 0x1 (NEEDED) Shared library: [libasan.so.8]\n"),
            "s-readelf-exe": (0, " 0x1 (NEEDED) Shared library: [libasan.so.8]\n"),
            "s-ldd": (0, None), "s-run": (0, "".join("Test %s...  [ OK ]\n" % c for c in R.CASES))}

    def go(self, **over):
        san = os.path.join(self.tmp, "san")
        for d in ("usr", "tests"):
            os.makedirs(os.path.join(san, d), exist_ok=True)
        libp = os.path.join(san, "usr", "libvtlscsi.so")
        res = dict(self.GOOD); res.update(over)
        res["s-ldd"] = res["s-ldd"] if res["s-ldd"][1] is not None else (0, "\tlibvtlscsi.so => %s (0x1)\n" % libp)
        ctx = mkctx(os.path.join(self.tmp, "w"))
        o_run, o_sha = R.run, R.sha
        R.run = lambda c, name, *a, **k: res[name]
        R.sha = lambda p: "0" * 64
        try:
            os.path.exists(libp) or open(libp, "w").close()
            return R.sanitizer_proof(ctx, san)[0]
        finally:
            R.run, R.sha = o_run, o_sha

    def test_good_evidence_passes(self):
        self.assertEqual(self.go(), "PASS")

    def test_failing_readelf_rc_not_pass(self):
        self.assertEqual(self.go(**{"s-readelf-lib": (1, "NEEDED libasan")}), "BLOCKED")
        self.assertEqual(self.go(**{"s-readelf-exe": (127, "NEEDED libasan")}), "BLOCKED")
        self.assertEqual(self.go(**{"s-nm-obj": (1, "__asan_report __ubsan_handle")}), "BLOCKED")

    def test_missing_runtime_evidence_not_pass(self):
        self.assertEqual(self.go(**{"s-readelf-lib": (0, "no runtime here")}), "FAIL")
        self.assertEqual(self.go(**{"s-readelf-exe": (0, "no runtime here")}), "FAIL")
        self.assertEqual(self.go(**{"s-nm-obj": (0, "U __ubsan_handle_x")}), "FAIL")
        self.assertEqual(self.go(**{"s-ldd": (1, "")}), "BLOCKED")


class Identity(Base):
    def test_replaced_workdir_not_removed(self):
        w = os.path.join(self.tmp, "w"); moved = os.path.join(self.tmp, "moved")
        keep = os.path.join(self.tmp, "caller")
        def body(ctx):
            os.rename(ctx.work, moved)           # swap in a different directory at the same path
            os.mkdir(ctx.work); open(os.path.join(ctx.work, "keep.txt"), "w").write("caller data")
            return 1
        o = R.body; R.body = body; self.addCleanup(setattr, R, "body", o)
        quiet_main(["--work", w])
        self.intact(os.path.join(w, "keep.txt"))


class Running(Base):
    def cmd(self, name, py, step_max=2, deadline=30, cap_kb=1):
        ctx = mkctx(os.path.join(self.tmp, "w"), deadline=deadline, cap_kb=cap_kb)
        t0 = time.monotonic()
        rc, txt = R.run(ctx, name, [sys.executable, "-c", py], step_max=step_max)
        return ctx, rc, txt, time.monotonic() - t0

    def pidfile(self):
        return os.path.join(self.tmp, "pid")

    def spawn_descendant(self, pf):
        code = ("import os,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)")
        return ("import subprocess,sys\nc=subprocess.Popen([sys.executable,'-c',%r,%r])\n" % (code, pf))

    def wait_pid(self, pf):
        for _ in range(100):
            if os.path.exists(pf) and open(pf).read().strip():
                return int(open(pf).read())
            time.sleep(0.02)
        self.fail("descendant never started")

    def test_pipe_holding_term_ignoring_descendant(self):
        pf = self.pidfile()
        py = self.spawn_descendant(pf) + "import time;time.sleep(0.3)\n"   # leader exits, child holds pipe
        ctx, rc, txt, dt = self.cmd("hold", py)
        pid = self.wait_pid(pf)
        self.assertEqual(rc, -9); self.assertLess(dt, 6)
        time.sleep(0.2); self.assertFalse(alive(pid), "descendant survived")
        self.assertFalse(ctx.incomplete)

    def test_eof_but_leader_alive(self):
        py = "import os,time;os.close(1);os.close(2);time.sleep(60)"
        ctx, rc, txt, dt = self.cmd("eof", py)
        self.assertEqual(rc, -9); self.assertLess(dt, 6)

    def test_stream_exception_kills_child_and_fails(self):
        pf = self.pidfile()
        py = self.spawn_descendant(pf) + "import time;time.sleep(60)\n"
        orig = R.select.select
        calls = []
        def bad(*a, **k):
            self.wait_pid(pf)
            calls.append(1); raise OSError("injected stream failure")
        R.select.select = bad
        try:
            ctx, rc, txt, dt = self.cmd("exc", py)
        finally:
            R.select.select = orig
        self.assertEqual(rc, -10); self.assertIn("stream error", txt)
        pid = self.wait_pid(pf); time.sleep(0.2)
        self.assertFalse(alive(pid)); self.assertLess(dt, 6)

    def test_overall_deadline_counts_spawn_and_reserve(self):
        ctx = mkctx(os.path.join(self.tmp, "w"), deadline=int(R.CLEANUP_RESERVE) + 2)
        t0 = time.monotonic()
        rc, _ = R.run(ctx, "long", [sys.executable, "-c", "import time;time.sleep(60)"], step_max=600)
        self.assertEqual(rc, -9)
        self.assertLess(time.monotonic() - t0, R.CLEANUP_RESERVE + 2)   # stopped before the overall deadline
        with self.assertRaises(R.Deadline):
            ctx2 = mkctx(os.path.join(self.tmp, "w2"), deadline=1)
            R.run(ctx2, "late", ["true"])

    def test_log_bounded(self):
        ctx, rc, txt, dt = self.cmd("big", "import sys;sys.stdout.write('x'*3000000)", step_max=20, cap_kb=1)
        self.assertEqual(rc, 0)
        self.assertLess(os.path.getsize(ctx.logs["big"]), 1024 + 200)
        self.assertIn("truncated", txt)

    def test_failure_never_pass(self):
        ctx, rc, txt, dt = self.cmd("fail", "import sys;sys.exit(3)")
        self.assertEqual(rc, 3)
        ctx, rc, txt, dt = self.cmd("missing", "pass")
        self.assertEqual(rc, 0)   # control: success is still success
        c2 = mkctx(os.path.join(self.tmp, "w3"))
        rc, _ = R.run(c2, "nobin", ["/nonexistent/binary-xyz"])
        self.assertNotEqual(rc, 0)

    def test_incomplete_cleanup_is_not_pass_and_keeps_work(self):
        w = os.path.join(self.tmp, "w")
        def body(ctx):
            for k in ctx.results: ctx.results[k] = ["PASS", "x"]
            ctx.incomplete = True
            return 0
        o = R.body; R.body = body; self.addCleanup(setattr, R, "body", o)
        rc, out = quiet_main(["--work", w])
        self.assertEqual(rc, 3); self.assertIn("INCOMPLETE", out); self.assertNotIn("FULL_ACCEPTANCE", out)
        self.assertTrue(os.path.isdir(w))

    def test_all_four_pass_required_for_full(self):
        w = os.path.join(self.tmp, "w")
        def body(ctx):
            for k in ctx.results: ctx.results[k] = ["PASS", "x"]
            ctx.results["SANITIZERS"] = ["BLOCKED", "x"]
            return 0
        o = R.body; R.body = body; self.addCleanup(setattr, R, "body", o)
        rc, out = quiet_main(["--work", w])
        self.assertNotEqual(rc, 0); self.assertNotIn("FULL_ACCEPTANCE", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
