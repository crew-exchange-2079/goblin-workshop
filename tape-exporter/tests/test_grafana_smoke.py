"""Mocked tests for ci/grafana/smoke.py and browser_smoke.py.

No container, Grafana or browser is started here: docker is a recording
fake and Grafana responses are hand-written frames. These tests check that
the verifier FAILS on the defects it exists to catch and that cleanup runs
on every exit path. The real run is the CI workflow
(.github/workflows/tape-exporter-grafana.yml).
"""

import copy
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ci", "grafana"))
import browser_smoke  # noqa: E402
import smoke  # noqa: E402

with open(smoke.DASHBOARD_FILE, encoding="utf-8") as _fh:
    DASH = json.load(_fh)
PANELS = {p["id"]: p for p in DASH["panels"]}


def ts_frame(labels, values):
    return {"schema": {"fields": [{"name": "Time", "type": "time"},
                                  {"name": "Value", "type": "number", "labels": labels}]},
            "data": {"values": [list(range(len(values))), values]}}


def table_frame(rows):
    keys = sorted({k for r in rows for k in r if k != "Value"})
    fields = [{"name": "Time", "type": "time"}] + [{"name": k, "type": "string"} for k in keys] \
        + [{"name": "Value #A", "type": "number"}]
    cols = [[0] * len(rows)] + [[r.get(k) for r in rows] for k in keys] + [[r["Value"] for r in rows]]
    return {"schema": {"fields": fields}, "data": {"values": cols}}


def series(*frames):
    return smoke.frames_to_series({"frames": list(frames)})


class VerifierTests(unittest.TestCase):
    def test_missing_shown_as_zero_fails(self):
        # e.g. someone adds `or vector(0)`: an absent exporter would read 0
        zero = {"A": series(ts_frame({}, [0, 0, 0]))}
        errs = smoke.evaluate_panel(PANELS[4], {"none": True}, zero)
        self.assertTrue(errs and "missing shown as zero" in errs[0], errs)
        self.assertEqual(smoke.evaluate_panel(PANELS[4], {"none": True}, {"A": []}), [])

    def test_all_zero_where_activity_expected_fails(self):
        errs = smoke.evaluate_panel(PANELS[4], {"nonzero": True},
                                    {"A": series(ts_frame({"device": "drive0"}, [0, 0]))})
        self.assertIn("non-zero", errs[0])

    def test_old_collectors_ok_panel_is_flagged_zero_as_healthy(self):
        # panel 1 as shipped before the smoke test: "ok"/"total", no thresholds
        old = copy.deepcopy(PANELS[1])
        old.pop("fieldConfig")
        failing = {"A": series(ts_frame({}, [0])), "B": series(ts_frame({}, [4]))}
        errs = smoke.evaluate_panel(old, {"not_healthy": True}, failing)
        self.assertTrue(any("value 0 shown green" in e for e in errs), errs)

    def test_current_panel_reports_failures_in_red(self):
        failing = {"A": series(ts_frame({}, [4])), "B": series(ts_frame({}, [4]))}
        self.assertEqual(smoke.evaluate_panel(PANELS[1], {"not_healthy": True,
                                                          "values": {"A": 4, "B": 4}}, failing), [])
        self.assertEqual(smoke.stat_colour(PANELS[1], "A", 0), "green")
        self.assertEqual(smoke.stat_colour(PANELS[1], "A", 1), "red")
        self.assertEqual(smoke.stat_colour(PANELS[1], "B", 4), "text")

    def test_variable_leak_and_status_expectations(self):
        rows = [{"device": "drive0", "status": "ok", "Value": 1},
                {"device": "drive2", "status": "timeout", "Value": 1}]
        s = {"A": series(table_frame(rows))}
        errs = smoke.evaluate_panel(PANELS[3], {"devices": {"drive0"}, "statuses_ok": True}, s)
        self.assertTrue(any("outside selection" in e for e in errs), errs)
        self.assertTrue(any("only status ok" in e for e in errs), errs)
        ok_only = {"A": series(table_frame(rows[:1]))}
        self.assertTrue(smoke.evaluate_panel(PANELS[3], {"statuses_ok": False}, ok_only))

    def test_values_expectation(self):
        self.assertTrue(smoke.evaluate_panel(PANELS[1], {"values": {"A": 0}}, {"A": []}))
        two = {"A": series(ts_frame({}, [0]), ts_frame({}, [0]))}
        self.assertTrue(smoke.evaluate_panel(PANELS[1], {"values": {"A": 0}}, two))

    def test_query_errors_detected(self):
        self.assertEqual(smoke.query_errors({"A": {"status": 200, "frames": []}}), [])
        self.assertTrue(smoke.query_errors({"A": {"status": 500, "error": "boom"}}))
        self.assertTrue(smoke.query_errors({"A": {"error": "parse error"}}))
        notice = {"frames": [{"schema": {"meta": {"notices": [{"severity": "error", "text": "x"}]}}}]}
        self.assertTrue(smoke.query_errors({"A": notice}))

    def test_datasource_health_both_ways(self):
        self.assertEqual(smoke.check_ds_health(200, {"status": "OK"}, True), [])
        self.assertTrue(smoke.check_ds_health(400, {"status": "ERROR"}, True))
        self.assertEqual(smoke.check_ds_health(400, {"status": "ERROR"}, False), [])
        self.assertIn("proves nothing", smoke.check_ds_health(200, {"status": "OK"}, False)[0])

    def test_provisioned_dashboard_must_match_repo(self):
        served = {"meta": {"provisioned": True}, "dashboard": copy.deepcopy(DASH)}
        self.assertEqual(smoke.check_provisioned(served, DASH), [])
        served["dashboard"]["panels"][3]["targets"][0]["expr"] = "up"
        self.assertTrue(smoke.check_provisioned(served, DASH))
        self.assertTrue(smoke.check_provisioned({"meta": {}, "dashboard": DASH}, DASH))

    def test_static_zero_defaults(self):
        self.assertEqual(smoke.static_zero_defaults(DASH), [])
        bad = copy.deepcopy(DASH)
        bad["panels"][0]["fieldConfig"]["defaults"]["noValue"] = "0"
        bad["panels"][3]["targets"][0]["expr"] += " or vector(0)"
        self.assertEqual(len(smoke.static_zero_defaults(bad)), 2)

    def test_query_building_substitutes_like_grafana(self):
        qs = smoke.build_queries(DASH, "ds", ["a.b:1"], ["drive0", "drive2"])
        exprs = [q["expr"] for _, q in qs]
        self.assertEqual(len(qs), sum(len(p["targets"]) for p in DASH["panels"]))
        self.assertFalse(any("$instance" in e or "$device" in e for e in exprs))
        self.assertIn('instance=~"(a\\.b:1)"', exprs[0])
        self.assertIn('device=~"(drive0|drive2)"', exprs[0])
        self.assertTrue(any("$__rate_interval" in e for e in exprs))  # left to Grafana
        self.assertEqual(smoke.format_var("All"), ".*")

    def test_frames_table_and_timeseries(self):
        rows = [{"device": "d", "status": "ok", "Value": 1}]
        self.assertEqual(series(table_frame(rows))[0]["labels"], {"device": "d", "status": "ok"})
        self.assertEqual(series(ts_frame({"device": "d"}, [1, None]))[0]["values"], [1, None])
        self.assertEqual(series(), [])

    def test_grafana_client_refuses_non_loopback(self):
        for base in ("http://0.0.0.0:3000", "http://example.org:3000", "https://127.0.0.1:3000"):
            with self.assertRaises(ValueError):
                smoke.Grafana(base, "u", "p")

    def test_browser_negative_control_must_see_errors(self):
        titles = [p["title"] for p in DASH["panels"]]
        blind = {"headers": len(titles), "panel_errors": 0, "dialog_visible": False, "http_errors": [],
                 "queries": ["x"], "responses": [(200, {"results": {}})], "no_data": 0}
        self.assertIn("would miss", browser_smoke.check_view("v", blind, titles, {"errors": True})[0])
        covered = dict(blind, dialog_visible=True)
        self.assertTrue(any("dialog" in e for e in browser_smoke.check_view("v", covered, titles, {})))
        unsub = dict(blind, queries=['x{device=~"$device"}'])
        self.assertTrue(browser_smoke.check_view("v", unsub, titles, {"device": "drive1"}))


def png(w, h, pixel):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(w)) for y in range(h))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class ScreenshotCheckTests(unittest.TestCase):
    def check(self, data, w=64, h=64):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.png")
            with open(p, "wb") as fh:
                fh.write(data)
            return browser_smoke.check_png(p, w, h)

    def test_blank_wrong_size_and_garbage_fail(self):
        self.assertIn("distinct colours", self.check(png(64, 64, lambda x, y: (20, 20, 20)))[0])
        self.assertIn("expected 64x64", self.check(png(32, 64, lambda x, y: (x * 8, y * 4, 0)))[0])
        self.assertIn("unreadable", self.check(b"not a png")[0])
        self.assertEqual(self.check(png(64, 64, lambda x, y: (x * 4, y * 4, (x * y) % 256))), [])


class FakeDocker:
    """Records docker argv; fails (or raises) on the first argv containing `fail_on`."""

    def __init__(self, fail_on=None, exc=None, leftovers=""):
        self.calls, self.fail_on, self.exc, self.leftovers = [], fail_on, exc, leftovers
        self.env_file_mode = None

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if "--env-file" in argv:
            path = argv[argv.index("--env-file") + 1]
            self.env_file_mode = stat.S_IMODE(os.stat(path).st_mode)
        if self.fail_on and self.fail_on in " ".join(argv):
            if self.exc:
                raise self.exc
            return 1, "", "simulated failure"
        if argv[1] == "port":
            return 0, "127.0.0.1:1\n", ""
        if "--filter" in argv:
            return 0, self.leftovers, ""
        return 0, "", ""

    def removed(self):
        return ({c[-1] for c in self.calls if c[1:4] == ["rm", "-f", "-v"]},
                {c[-1] for c in self.calls if c[1:3] == ["network", "rm"]})


class CleanupTests(unittest.TestCase):
    def run_main(self, fake, extra_patches=()):
        art = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(art, ignore_errors=True))
        tmps = []
        real_mkdtemp = tempfile.mkdtemp

        def track(*a, **kw):
            d = real_mkdtemp(*a, **kw)
            tmps.append(d)
            return d
        with mock.patch.object(smoke.tempfile, "mkdtemp", side_effect=track), \
                mock.patch("builtins.print"):
            for p in extra_patches:
                p.start()
            try:
                rc = smoke.main(["--artifacts", art, "--no-pull"], docker_exec=fake)
            finally:
                for p in extra_patches:
                    p.stop()
        with open(os.path.join(art, "result.json"), encoding="utf-8") as fh:
            result = json.load(fh)
        return rc, result, tmps, art

    def assert_all_removed(self, fake, tmps):
        containers, networks = fake.removed()
        created = {c[c.index("--name") + 1] for c in fake.calls if c[1] == "create"}
        made_nets = {c[-1] for c in fake.calls if c[1:3] == ["network", "create"]}
        self.assertTrue(created <= containers, (created, containers))
        self.assertTrue(made_nets <= networks, (made_nets, networks))
        for d in tmps:
            self.assertFalse(os.path.exists(d), d)

    def test_setup_failure_cleans_up_and_is_not_a_pass(self):
        fake = FakeDocker(fail_on="start lto-smoke")
        rc, result, tmps, _ = self.run_main(fake)
        self.assertEqual(rc, 2)
        self.assertEqual(result["verdict"], "NOT RUN (setup error)")
        self.assert_all_removed(fake, tmps)
        self.assertEqual(len(fake.removed()[0]), 3)
        self.assertEqual(fake.env_file_mode, 0o600)

    def test_failed_network_create_still_removed(self):
        fake = FakeDocker(fail_on="--internal")
        rc, _, tmps, _ = self.run_main(fake)
        self.assertEqual(rc, 2)
        self.assertTrue(fake.removed()[1])
        self.assert_all_removed(fake, tmps)

    def test_sigterm_and_docker_timeout_still_clean_up(self):
        for exc in (smoke.Terminated("signal 15"),
                    __import__("subprocess").TimeoutExpired("docker", 1)):
            fake = FakeDocker(fail_on="create --name lto-smoke-", exc=exc)
            rc, result, tmps, _ = self.run_main(fake)
            self.assertEqual(rc, 2, exc)
            self.assertIn("setup_error", result)
            self.assert_all_removed(fake, tmps)

    def test_verification_failure_cleans_up_and_secret_stays_out(self):
        fake = FakeDocker()
        seen = {}

        def fake_verify(graf, art, browser):
            seen["auth"] = graf._auth
            secret = __import__("base64").b64decode(graf._auth.split()[1]).decode().split(":", 1)[1]
            seen["secret"] = secret
            with open(os.path.join(art, "grafana.log"), "w", encoding="utf-8") as fh:
                fh.write(f"oops {secret}\n")      # a log that leaked it
            return ["[x] panel 1 shown green"], {}
        patches = [mock.patch.object(smoke, "verify", side_effect=fake_verify),
                   mock.patch.object(smoke.Grafana, "get", return_value=(200, {})),
                   mock.patch.object(smoke, "collect_logs")]
        rc, result, tmps, art = self.run_main(fake, patches)
        self.assertEqual(rc, 1)
        self.assertEqual(result["verdict"], "FAIL")
        self.assert_all_removed(fake, tmps)
        argv_text = " ".join(" ".join(c) for c in fake.calls)
        self.assertNotIn(seen["secret"], argv_text)
        for name in os.listdir(art):
            with open(os.path.join(art, name), encoding="utf-8") as fh:
                self.assertNotIn(seen["secret"], fh.read(), name)
        self.assertTrue(any("redacted" in c for c in result["cleanup"]))

    def test_unverifiable_cleanup_fails_the_run(self):
        # `docker ps/network ls/volume ls --filter` itself failing (rc 1) means
        # we can't tell whether anything is left: that is a failure, not clean
        class ListFails(FakeDocker):
            def __call__(self, argv, timeout):
                if "--filter" in argv:
                    self.calls.append(argv)
                    return 1, "", "Cannot connect to the Docker daemon"
                return super().__call__(argv, timeout)
        fake = ListFails()
        problems = smoke.Docker("t", exec_fn=fake).leftovers()
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(all("could not verify" in p for p in problems), problems)
        patches = [mock.patch.object(smoke, "verify", return_value=([], {})),
                   mock.patch.object(smoke.Grafana, "get", return_value=(200, {}))]
        rc, result, _, _ = self.run_main(ListFails(), patches)
        self.assertEqual(rc, 1)
        self.assertEqual(result["verdict"], "FAIL")

    def test_leftover_resources_fail_the_run(self):
        fake = FakeDocker(leftovers="abc123\n")
        patches = [mock.patch.object(smoke, "verify", return_value=([], {})),
                   mock.patch.object(smoke.Grafana, "get", return_value=(200, {}))]
        rc, result, _, _ = self.run_main(fake, patches)
        self.assertEqual(rc, 1)
        self.assertTrue(any("left behind" in c for c in result["cleanup"]))

    def test_containers_are_hardened_and_only_grafana_is_published_on_loopback(self):
        fake = FakeDocker(fail_on="start lto-smoke")
        self.run_main(fake)
        creates = [c for c in fake.calls if c[1] == "create"]
        self.assertEqual(len(creates), 3)
        for c in creates:
            for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only"):
                self.assertIn(flag, c)
            text = " ".join(c)
            for bad in ("--privileged", "--network host", "--network=host", "--device", "/dev"):
                self.assertNotIn(bad, text)
            for m in [c[i + 1] for i, a in enumerate(c) if a == "--mount"]:
                self.assertIn("readonly", m)
        published = [(c[c.index("--name") + 1], c[c.index("-p") + 1]) for c in creates if "-p" in c]
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0][0].endswith("-grafana"))
        self.assertEqual(published[0][1], "127.0.0.1::3000")
        nets = [c for c in fake.calls if c[1:3] == ["network", "create"]]
        self.assertIn("--internal", nets[0])


class RedirectTests(unittest.TestCase):
    """Basic auth must never follow a redirect (to another origin or at all)."""

    def test_redirect_not_followed_and_credential_not_forwarded(self):
        import http.server
        import threading
        seen = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(Sink):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()
        red = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        servers = (sink, red)
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
        for t in threads:
            t.start()
        try:
            graf = smoke.Grafana(f"http://127.0.0.1:{red.server_port}", "u", "secret-pw")
            status, _ = graf.get("/api/health")
        finally:
            for s in servers:
                s.shutdown()
                s.server_close()
        self.assertEqual(status, 302)
        self.assertEqual(seen, [])           # the other origin got nothing

    def test_env_proxy_is_bypassed_for_credentialed_calls(self):
        import http.server
        import threading
        hits = {"proxy": [], "grafana": []}

        def handler(name):
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    hits[name].append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

                def log_message(self, *a):
                    pass
            return H
        proxy = http.server.HTTPServer(("127.0.0.1", 0), handler("proxy"))
        graf_srv = http.server.HTTPServer(("127.0.0.1", 0), handler("grafana"))
        servers = (proxy, graf_srv)
        for srv in servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = {k: v for k, v in os.environ.items() if k.lower() != "no_proxy"}
        env["http_proxy"] = env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy.server_port}"
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                graf = smoke.Grafana(f"http://127.0.0.1:{graf_srv.server_port}", "u", "pw")
                status, _ = graf.get("/api/health")
        finally:
            for srv in servers:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(status, 200)
        self.assertEqual(hits["proxy"], [])
        self.assertEqual(len(hits["grafana"]), 1)


class FakeGrafana:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def post(self, path, body):
        return self.status, self.body


def empty_frame():
    return {"schema": {"fields": [{"name": "Time", "type": "time"},
                                  {"name": "Value", "type": "number"}]},
            "data": {"values": [[], []]}}


class QueryResponseStrictnessTests(unittest.TestCase):
    ABSENT = next(c for c in smoke.CASES if c["name"] == "absent")

    def refs(self):
        return [q["refId"] for _, q in smoke.build_queries(DASH, smoke.DS_OK, "All", "All")]

    def test_http_500_with_partial_empty_results_is_not_absence(self):
        errs, _, _ = smoke.run_case(FakeGrafana(500, {"results": {"p1A": {"frames": []}}}),
                                    DASH, self.ABSENT)
        self.assertTrue(any("HTTP 500" in e for e in errs), errs)
        self.assertTrue(any("no result for" in e for e in errs), errs)

    def test_valid_empty_results_are_absence(self):
        body = {"results": {r: {"status": 200, "frames": [empty_frame()]} for r in self.refs()}}
        errs, _, _ = smoke.run_case(FakeGrafana(200, body), DASH, self.ABSENT)
        self.assertEqual(errs, [])

    def test_grafana13_empty_instant_frame_is_valid_absence(self):
        # shape Grafana 13.0.2 returned for an instant query with no rows
        body = {"results": {r: {"status": 200, "frames": [
            {"schema": {"refId": r, "meta": {"typeVersion": [0, 0]}}, "data": {"values": []}}]}
            for r in self.refs()}}
        errs, _, _ = smoke.run_case(FakeGrafana(200, body), DASH, self.ABSENT)
        self.assertEqual(errs, [])

    def test_missing_unexpected_or_malformed_results_fail(self):
        good = {r: {"status": 200, "frames": [empty_frame()]} for r in self.refs()}
        bad_cases = {
            "missing ref": {k: v for k, v in good.items() if k != "p4A"},
            "extra ref": dict(good, pZZ={"frames": []}),
            "frames not a list": dict(good, p4A={"frames": None}),
            "no frames key": dict(good, p4A={"status": 200}),
            "result not a dict": dict(good, p4A=[]),
            "fields not a list": dict(good, p4A={"frames": [{"schema": {"fields": "x"}, "data": {"values": []}}]}),
            "columns != fields": dict(good, p4A={"frames": [{"schema": empty_frame()["schema"],
                                                             "data": {"values": [[]]}}]}),
            "fields but no values": dict(good, p4A={"frames": [{"schema": empty_frame()["schema"],
                                                                "data": {"values": []}}]}),
            "values but no fields": dict(good, p4A={"frames": [{"schema": {"refId": "p4A"},
                                                                "data": {"values": [[1], [2]]}}]}),
            "frame without schema": dict(good, p4A={"frames": [{"data": {"values": []}}]}),
            "frame not an object": dict(good, p4A={"frames": ["x"]}),
            "ragged columns": dict(good, p4A={"frames": [{"schema": empty_frame()["schema"],
                                                          "data": {"values": [[1, 2], [1]]}}]}),
        }
        for name, results in bad_cases.items():
            errs, _, _ = smoke.run_case(FakeGrafana(200, {"results": results}), DASH, self.ABSENT)
            self.assertTrue(errs, name)
        errs, _, _ = smoke.run_case(FakeGrafana(200, ["not", "a", "dict"]), DASH, self.ABSENT)
        self.assertTrue(errs)


class PullTests(unittest.TestCase):
    def test_present_image_not_pulled_and_retry_is_bounded(self):
        fake = FakeDocker()
        smoke.pull(smoke.Docker("t", exec_fn=fake), "img@sha256:x")
        self.assertEqual([c[1] for c in fake.calls], ["image"])
        fake = FakeDocker(fail_on="img@sha256:x")
        sleeps = []
        with self.assertRaises(smoke.SetupError):
            smoke.pull(smoke.Docker("t", exec_fn=fake), "img@sha256:x", sleep=sleeps.append)
        self.assertEqual([c[1] for c in fake.calls], ["image", "pull", "pull", "pull"])
        self.assertEqual(len(sleeps), 2)


class SyntheticTargetTests(unittest.TestCase):
    def test_synthetic_pages_parse_with_the_real_parser(self):
        import synthetic_target as st
        from lto_exporter.parsers import parse_sg_logs_error_counters
        for direction in ("write", "read"):
            page = parse_sg_logs_error_counters(st.healthy_page("drive0", direction), direction)
            self.assertEqual(page.status, "ok")
            self.assertGreater(page.values["total_uncorrected"], 0)
        partial = parse_sg_logs_error_counters(st.healthy_page("drive1", "read"), "read")
        self.assertEqual(partial.status, "ok")
        self.assertNotIn("total_uncorrected", partial.values)


if __name__ == "__main__":
    unittest.main()
