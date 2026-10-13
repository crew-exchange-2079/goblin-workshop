"""Grafana integration smoke test for monitoring/grafana/lto-exporter-dashboard.json.

Starts three disposable containers from pinned images on a dedicated,
--internal Docker network:

  synthetic  python image running synthetic_target.py: the real exporter
             Collector/HTTP handler with a fake sg_logs runner (no command,
             no device); one "healthy" and one "failing" exporter
  prometheus scrapes both, plus a host name that doesn't exist ("absent")
  grafana    provisions the repo dashboard file unmodified and two
             Prometheus datasources (real + unreachable negative control)

Only Grafana is also attached to a second (non-internal) network, so its
port can be published, and only on 127.0.0.1 with an ephemeral host port.
No host network, no privileged mode, no devices, no bind mount except
read-only source/config files. The admin password is generated per run,
handed to Grafana through a 0600 --env-file (never argv) and sent only in
an HTTP header to 127.0.0.1. Everything the run creates is labelled and
removed in `finally`, also on failure, timeout or SIGTERM.

Then, through Grafana's HTTP API only: datasource health (real one OK,
unreachable one MUST fail), dashboard provisioned unchanged, variable
queries resolved through the datasource, and every panel query evaluated
through /api/ds/query for several variable selections with per-panel
expectations (healthy values, missing-is-not-zero, no data for an absent
exporter, query errors surfaced). With --browser, browser_smoke.py also
loads the dashboard in headless Chromium; that is a smoke check, not a
visual/layout review.

Exit: 0 pass, 1 verification failed, 2 setup error / could not run.

Usage (from tape-exporter/):
  python3 ci/grafana/smoke.py --artifacts DIR [--browser]
"""

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # tape-exporter/
DASHBOARD_DIR = os.path.join(ROOT, "monitoring", "grafana")
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, "lto-exporter-dashboard.json")
DASHBOARD_UID = "lto-exporter-v1"

# Pinned by digest (multi-arch index digest from Docker Hub). Bump together
# with the README.
IMAGES = {
    "grafana": "docker.io/grafana/grafana-oss:13.0.2@sha256:"
               "5dad0df181cb644a14e13617b913b261a54f7d4fd4510721dba420929f35bea2",
    "prometheus": "docker.io/prom/prometheus:v3.14.0@sha256:"
                  "5ce7540c3c00ef4ab0c9d2c995c6a5b9c421f44b4a115d97a2c7af3b1c21cbb0",
    "python": "docker.io/library/python:3.13.16-alpine3.24@sha256:"
              "2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a",
}
LABEL = "lto-grafana-smoke.run"
DS_OK = "lto-smoke-prom"
DS_BROKEN = "lto-smoke-unreachable"
HEALTHY = "lto-synth:9101"
FAILING = "lto-synth:9102"
ABSENT = "lto-absent:9103"
ADMIN_USER = "smoke-admin"
QUERY_RANGE = ("now-15m", "now")    # the run only has minutes of data

DOCKER_TIMEOUT = 120
PULL_TIMEOUT = 600

GRAFANA_ENV = {
    "GF_SECURITY_ADMIN_USER": ADMIN_USER,
    "GF_AUTH_ANONYMOUS_ENABLED": "false",
    "GF_AUTH_DISABLE_LOGIN_FORM": "false",
    "GF_USERS_ALLOW_SIGN_UP": "false",
    "GF_ANALYTICS_REPORTING_ENABLED": "false",
    "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
    "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
    "GF_ANALYTICS_FEEDBACK_LINKS_ENABLED": "false",
    "GF_NEWS_NEWS_FEED_ENABLED": "false",
    "GF_PLUGINS_PREINSTALL_DISABLED": "true",
    "GF_PLUGINS_PUBLIC_KEY_RETRIEVAL_DISABLED": "true",
    "GF_SNAPSHOTS_EXTERNAL_ENABLED": "false",
    "GF_SECURITY_DISABLE_GRAVATAR": "true",
    "GF_LIVE_MAX_CONNECTIONS": "0",
    "GF_LOG_LEVEL": "warn",
}
HARDENING = ["--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--pids-limit=512", "--memory=1g", "--read-only"]


class SetupError(Exception):
    """The stack could not be brought up: the smoke test did NOT run."""


class Terminated(BaseException):
    pass


# --------------------------------------------------------------------------
# Docker resources (all owned resources tracked and removed)
# --------------------------------------------------------------------------

def _real_exec(argv, timeout):
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=timeout, check=False)
    return p.returncode, p.stdout, p.stderr


class Docker:
    """Thin docker CLI wrapper that remembers what it created."""

    def __init__(self, run_id, exec_fn=_real_exec, log=print):
        self.run_id = run_id
        self.exec = exec_fn
        self.log = log
        self.containers = []
        self.networks = []
        self.argv_log = []      # every argv we ran; must never hold a secret

    def __call__(self, *args, timeout=DOCKER_TIMEOUT, check=True):
        argv = ["docker", *args]
        self.argv_log.append(argv)
        try:
            rc, out, err = self.exec(argv, timeout)
        except subprocess.TimeoutExpired:
            raise SetupError(f"timed out after {timeout}s: docker {args[0]}") from None
        if check and rc != 0:
            raise SetupError(f"docker {' '.join(args[:2])} failed (rc {rc}): {err.strip()[:300]}")
        return rc, out, err

    def name(self, role):
        return f"lto-smoke-{self.run_id}-{role}"

    def network(self, role, internal):
        name = self.name(role)
        self.networks.append(name)      # before create: a half-made one is still removed
        args = ["network", "create", "--label", f"{LABEL}={self.run_id}"]
        if internal:
            args.append("--internal")
        self("network", *args[1:], name)
        return name

    def create(self, role, image, args, cmd=()):
        name = self.name(role)
        self.containers.append(name)
        self("create", "--name", name, "--label", f"{LABEL}={self.run_id}",
             *HARDENING, *args, image, *cmd)
        return name

    def cleanup(self):
        """Remove every owned container (with anonymous volumes) and network.

        Never raises; returns a list of problems (empty = clean)."""
        problems = []
        for c in reversed(self.containers):
            try:
                rc, _, err = self("rm", "-f", "-v", c, timeout=60, check=False)
                if rc != 0 and "No such container" not in err:
                    problems.append(f"rm {c}: {err.strip()[:200]}")
            except Exception as e:  # keep going: remove the rest
                problems.append(f"rm {c}: {e}")
        for n in reversed(self.networks):
            try:
                rc, _, err = self("network", "rm", n, timeout=60, check=False)
                if rc != 0 and "not found" not in err:
                    problems.append(f"network rm {n}: {err.strip()[:200]}")
            except Exception as e:
                problems.append(f"network rm {n}: {e}")
        problems += self.leftovers()
        return problems

    def leftovers(self):
        out = []
        for kind in (("ps", "-aq"), ("network", "ls", "-q"), ("volume", "ls", "-q")):
            try:
                rc, stdout, err = self(*kind, "--filter", f"label={LABEL}={self.run_id}",
                                     timeout=60, check=False)
            except Exception as e:
                out.append(f"could not verify cleanup ({kind[0]}): {e}")
                continue
            if rc != 0:
                out.append(f"could not verify cleanup ({kind[0]} rc {rc}): {err.strip()[:200]}")
            elif stdout.strip():
                out.append(f"left behind ({kind[0]}): {stdout.split()}")
        return out


# --------------------------------------------------------------------------
# Grafana HTTP client (127.0.0.1 only)
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx comes back as its own status, unfollowed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Grafana:
    def __init__(self, base, user, password, timeout=20):
        if not re.match(r"^http://127\.0\.0\.1:\d+$", base):
            raise ValueError("Grafana must be reached on 127.0.0.1 only")
        self.base = base
        self.timeout = timeout
        tok = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {tok}"
        # The credential goes to this origin only: no proxy (an empty
        # ProxyHandler also keeps urllib from adding the env-proxy one) and
        # no redirects (urllib would replay Authorization to the new URL).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, _json(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)


def _json(raw):
    try:
        return json.loads(raw or b"null")
    except ValueError:
        return {"_non_json": raw[:200].decode("utf-8", "replace")}


# --------------------------------------------------------------------------
# Pure verification logic (unit-tested in tests/test_grafana_smoke.py)
# --------------------------------------------------------------------------

def check_ds_health(status, body, expect_ok):
    """Grafana GET /api/datasources/uid/<uid>/health."""
    ok = status == 200 and isinstance(body, dict) and body.get("status") == "OK"
    if expect_ok and not ok:
        return [f"datasource health not OK: HTTP {status} {_short(body)}"]
    if not expect_ok and ok:
        return ["unreachable datasource reported healthy: health check proves nothing"]
    return []


def check_provisioned(served, repo):
    """served: GET /api/dashboards/uid/<uid> body; repo: the JSON file."""
    errs = []
    meta = served.get("meta", {}) if isinstance(served, dict) else {}
    dash = served.get("dashboard", {}) if isinstance(served, dict) else {}
    if not meta.get("provisioned"):
        errs.append("dashboard is not marked provisioned")
    if dash.get("uid") != repo["uid"] or dash.get("title") != repo["title"]:
        errs.append(f"served uid/title {dash.get('uid')!r}/{dash.get('title')!r} != repo")

    def shape(d):
        return [(p.get("id"), p.get("type"), p.get("title"),
                 [(t.get("refId"), t.get("expr")) for t in p.get("targets", [])])
                for p in d.get("panels", [])]
    if shape(dash) != shape(repo):
        errs.append("served panels/queries differ from the repo file")
    names = [v.get("name") for v in dash.get("templating", {}).get("list", [])]
    if names != [v["name"] for v in repo["templating"]["list"]]:
        errs.append(f"served variables {names} differ from repo")
    return errs


def static_zero_defaults(dash):
    """Dashboard-level patterns that turn missing data into a healthy zero."""
    errs = []
    for p in dash.get("panels", []):
        nv = p.get("fieldConfig", {}).get("defaults", {}).get("noValue")
        if nv is not None and str(nv).strip() in ("0", "0.0", "0%"):
            errs.append(f"panel {p['id']}: noValue={nv!r} shows missing data as zero")
        for t in p.get("targets", []):
            if re.search(r"\bvector\s*\(\s*0\s*\)", t.get("expr", "")):
                errs.append(f"panel {p['id']} {t.get('refId')}: 'vector(0)' fills missing data with zero")
    return errs


def _regex_escape(v):
    # Grafana's Prometheus datasource escapes regex specials in multi-value vars
    return re.sub(r"([\\^$*+?.()|{}\[\]])", r"\\\1", v)


def format_var(values, all_value=".*"):
    """How Grafana formats a multi/includeAll variable for a =~ matcher."""
    if values == "All":
        return all_value
    return "(" + "|".join(_regex_escape(v) for v in values) + ")"


def build_queries(dash, ds_uid, instance, device):
    sub = {"$instance": format_var(instance), "$device": format_var(device)}
    out = []
    for p in dash["panels"]:
        for t in p["targets"]:
            expr = t["expr"]
            for k, v in sub.items():
                expr = expr.replace(k, v)
            # $__rate_interval etc. are left for Grafana's backend to expand
            out.append((p["id"], {
                "refId": f"p{p['id']}{t['refId']}",
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": expr,
                "instant": bool(t.get("instant")),
                "range": not t.get("instant"),
                "format": t.get("format", "time_series"),
                "legendFormat": t.get("legendFormat", ""),
                "intervalMs": 2000,
                "maxDataPoints": 500,
            }))
    return out


def frames_to_series(result):
    """One Grafana /api/ds/query result -> list of {labels, values}.

    Handles time series frames (labels on the value field) and table frames
    (labels as string columns)."""
    series = []
    frames = result.get("frames") if isinstance(result, dict) else None
    for fr in frames if isinstance(frames, list) else []:
        if not isinstance(fr, dict):
            continue        # malformed: reported by validate_results
        fields = (fr.get("schema") or {}).get("fields") or []
        cols = (fr.get("data") or {}).get("values") or []
        if not isinstance(fields, list) or not isinstance(cols, list) or not fields or not cols:
            continue
        if not all(isinstance(f, dict) for f in fields) or not all(isinstance(c, list) for c in cols):
            continue
        nrows = max((len(c) for c in cols), default=0)
        str_cols = {f["name"]: cols[i] for i, f in enumerate(fields)
                    if f.get("type") == "string" and i < len(cols)}
        for i, f in enumerate(fields):
            if f.get("type") != "number" or i >= len(cols):
                continue
            if str_cols:            # table: one series per row
                for r in range(nrows):
                    labels = {k: v[r] for k, v in str_cols.items() if r < len(v)}
                    labels.update(f.get("labels") or {})
                    series.append({"labels": labels,
                                   "values": [cols[i][r] if r < len(cols[i]) else None]})
            else:
                series.append({"labels": dict(f.get("labels") or {}),
                               "values": list(cols[i])})
    return series


def validate_results(results, requested):
    """Shape check for a /api/ds/query body. "No series" only counts as
    absence when every requested ref came back as a well-formed result."""
    if not isinstance(results, dict):
        return [f"response has no 'results' object: {_short(results)}"]
    errs = []
    missing = [r for r in requested if r not in results]
    extra = sorted(set(results) - set(requested))
    if missing:
        errs.append(f"no result for {missing}")
    if extra:
        errs.append(f"unrequested results {extra}")
    for ref in requested:
        r = results.get(ref)
        if ref not in results:
            continue
        if not isinstance(r, dict):
            errs.append(f"{ref}: result is not an object")
            continue
        frames = r.get("frames")
        if not isinstance(frames, list):
            errs.append(f"{ref}: 'frames' missing or not a list")
            continue
        for i, fr in enumerate(frames):
            fields = fr.get("schema", {}).get("fields") if isinstance(fr, dict) else None
            values = fr.get("data", {}).get("values") if isinstance(fr, dict) else None
            if not isinstance(fields, list) or not fields or \
                    not all(isinstance(f, dict) and "name" in f and "type" in f for f in fields):
                errs.append(f"{ref} frame {i}: bad schema.fields")
                continue
            if not isinstance(values, list) or len(values) != len(fields) or \
                    not all(isinstance(c, list) for c in values) or len({len(c) for c in values}) > 1:
                errs.append(f"{ref} frame {i}: data.values does not match schema "
                            f"({len(fields)} fields)")
    return errs


def query_errors(results):
    errs = []
    for ref, r in results.items():
        if r.get("error") or (r.get("status") not in (None, 200)):
            errs.append(f"{ref}: {r.get('status')} {str(r.get('error'))[:200]}")
        frames = r.get("frames")
        for fr in frames if isinstance(frames, list) else []:
            if not isinstance(fr, dict) or not isinstance(fr.get("schema"), dict):
                continue
            for n in (fr["schema"].get("meta") or {}).get("notices") or []:
                if n.get("severity") == "error":
                    errs.append(f"{ref}: notice {n.get('text', '')[:200]}")
    return errs


DEFAULT_THRESHOLDS = [{"color": "green", "value": None}, {"color": "red", "value": 80}]
HEALTHY_COLOURS = {"green", "semi-dark-green", "dark-green", "light-green", "super-light-green"}


def stat_colour(panel, ref_id, value):
    """Colour Grafana's stat panel picks for `value` from the panel's own
    field config (byFrameRefID fixed-colour overrides, else thresholds,
    else Grafana's default green/red@80). Derived from config, not pixels."""
    fc = panel.get("fieldConfig", {})
    for o in fc.get("overrides", []):
        m = o.get("matcher", {})
        if m.get("id") == "byFrameRefID" and m.get("options") == ref_id:
            for prop in o.get("properties", []):
                if prop.get("id") == "color" and prop.get("value", {}).get("mode") == "fixed":
                    return prop["value"].get("fixedColor", "text")
    steps = fc.get("defaults", {}).get("thresholds", {}).get("steps") or DEFAULT_THRESHOLDS
    colour = steps[0]["color"]
    for s in steps:
        if s.get("value") is None or value >= s["value"]:
            colour = s["color"]
    return colour


def _last(values):
    vals = [v for v in values if v is not None]
    return vals[-1] if vals else None


def evaluate_panel(panel, expect, series_by_ref):
    """Check one panel's query results against its expectation for a case.

    expect keys (all optional):
      none          True: must return no series at all (missing != zero)
      min_series    at least this many series
      nonzero       at least one series with a non-zero last value
      devices       every series' device label must be in this set
      statuses_ok   True: every row status=="ok"; False: some row not ok
      values        {ref: v}: each ref must return exactly one series, last value v
      not_healthy   True: stat values must not be shown in a healthy colour
    """
    errs = []
    pid = panel["id"]
    all_series = [s for ss in series_by_ref.values() for s in ss]
    with_value = [s for s in all_series if _last(s["values"]) is not None]
    if expect.get("none") and with_value:
        vals = sorted({_last(s['values']) for s in with_value})[:5]
        errs.append(f"panel {pid}: expected no data, got {len(with_value)} series (values {vals})"
                    + (" -- missing shown as zero" if vals == [0] else ""))
    if len(with_value) < expect.get("min_series", 0):
        errs.append(f"panel {pid}: expected >= {expect['min_series']} series, got {len(with_value)}")
    if expect.get("nonzero") and not any(_last(s["values"]) for s in with_value):
        errs.append(f"panel {pid}: expected a non-zero value, got only zeros/none")
    if "devices" in expect:
        seen = {s["labels"].get("device") for s in with_value if "device" in s["labels"]}
        extra = seen - set(expect["devices"])
        if extra:
            errs.append(f"panel {pid}: devices {sorted(extra)} outside selection {sorted(expect['devices'])}")
    if "statuses_ok" in expect and with_value:
        sts = {s["labels"].get("status") for s in with_value}
        if expect["statuses_ok"] and sts != {"ok"}:
            errs.append(f"panel {pid}: expected only status ok, got {sorted(map(str, sts))}")
        if not expect["statuses_ok"] and sts <= {"ok"}:
            errs.append(f"panel {pid}: expected failing statuses, got only ok")
    for ref, want in expect.get("values", {}).items():
        got = [_last(s["values"]) for s in series_by_ref.get(ref, [])]
        if got != [want]:
            errs.append(f"panel {pid} {ref}: expected [{want}], got {got}")
    if expect.get("not_healthy"):
        for ref, ss in series_by_ref.items():
            for s in ss:
                v = _last(s["values"])
                if v is None:
                    continue
                c = stat_colour(panel, ref, v)
                if c in HEALTHY_COLOURS:
                    errs.append(f"panel {pid} {ref}: value {v} shown {c} although collectors are failing"
                                " (zero/partial shown as healthy)")
    return errs


# Per-case expectations. "case" = variable selection; ids are dashboard panels.
CASES = [
    {"name": "healthy", "instance": [HEALTHY], "device": "All", "expect": {
        1: {"values": {"A": 0, "B": 4}},
        2: {"min_series": 1},
        3: {"min_series": 4, "statuses_ok": True},
        4: {"nonzero": True}, 5: {"nonzero": True}, 6: {"nonzero": True},
        7: {"nonzero": True},
        8: {"min_series": 1, "devices": {"drive1"}},
        9: {"min_series": 4}}},
    {"name": "failing", "instance": [FAILING], "device": "All", "expect": {
        1: {"values": {"A": 4, "B": 4}, "not_healthy": True},
        2: {"min_series": 1},
        3: {"min_series": 4, "statuses_ok": False},
        4: {"none": True}, 5: {"none": True}, 6: {"none": True}, 7: {"none": True},
        8: {"none": True},
        9: {"min_series": 4}}},
    {"name": "absent", "instance": [ABSENT], "device": "All", "expect": {
        i: {"none": True} for i in range(1, 10)}},
    {"name": "device=drive1", "instance": "All", "device": ["drive1"], "expect": {
        3: {"min_series": 2, "devices": {"drive1"}},
        4: {"min_series": 1, "devices": {"drive1"}},
        7: {"min_series": 2, "devices": {"drive1"}},
        8: {"min_series": 1, "devices": {"drive1"}},
        9: {"min_series": 2, "devices": {"drive1"}}}},
    {"name": "device=drive0+drive2", "instance": "All", "device": ["drive0", "drive2"], "expect": {
        3: {"min_series": 4, "devices": {"drive0", "drive2"}},
        4: {"nonzero": True, "devices": {"drive0"}},
        9: {"min_series": 4, "devices": {"drive0", "drive2"}}}},
]


def run_case(graf, dash, case, ds_uid=DS_OK):
    """Evaluate every panel query of `dash` for one variable selection."""
    queries = build_queries(dash, ds_uid, case["instance"], case["device"])
    status, body = graf.post("/api/ds/query", {
        "queries": [q for _, q in queries], "from": QUERY_RANGE[0], "to": QUERY_RANGE[1]})
    results = body.get("results") if isinstance(body, dict) else None
    errs = []
    if status != 200:
        errs.append(f"/api/ds/query HTTP {status}: {_short(body)}")
    errs += validate_results(results, [q["refId"] for _, q in queries])
    if not isinstance(results, dict):
        results = {}
    errs += query_errors({k: v for k, v in results.items() if isinstance(v, dict)})
    summary = {}
    for p in dash["panels"]:
        by_ref = {}
        for pid, q in queries:
            if pid == p["id"]:
                by_ref[q["refId"][len(f"p{pid}"):]] = frames_to_series(results.get(q["refId"], {}))
        summary[p["id"]] = {ref: [{"labels": s["labels"], "last": _last(s["values"])} for s in ss]
                            for ref, ss in by_ref.items()}
        exp = case.get("expect", {}).get(p["id"])
        if exp:
            errs += evaluate_panel(p, exp, by_ref)
    return errs, summary, [q for _, q in queries]


def check_variables(graf, dash, ds_uid=DS_OK):
    """Resolve the dashboard's label_values() variables through the datasource
    (the same resource API the Grafana frontend uses)."""
    errs, got = [], {}
    rng = _epoch_range()
    for v in dash["templating"]["list"]:
        if v.get("type") != "query":
            continue
        q = v["query"]["query"] if isinstance(v["query"], dict) else v["query"]
        m = re.fullmatch(r"label_values\((.*),\s*([a-z_]+)\)", q)
        match = m.group(1).replace("$instance", format_var("All"))
        path = (f"/api/datasources/uid/{ds_uid}/resources/api/v1/label/{m.group(2)}/values?"
                + urllib.parse.urlencode({"match[]": match, "start": rng[0], "end": rng[1]}))
        status, body = graf.get(path)
        vals = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(vals, list):
            errs.append(f"variable {v['name']}: HTTP {status} {_short(body)}")
            continue
        got[v["name"]] = sorted(vals)
    want = {"instance": sorted([HEALTHY, FAILING]),
            "device": ["drive0", "drive1", "drive2", "drive3"]}
    for k, w in want.items():
        if k in got and got[k] != w:
            errs.append(f"variable {k}: got {got[k]}, expected {w}")
    return errs, got


def _epoch_range():
    now = int(time.time())
    return str(now - 900), str(now)


def _short(body):
    return json.dumps(body)[:300] if not isinstance(body, str) else body[:300]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def wait_for(fn, what, timeout, interval=1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            ok, last = fn()
            if ok:
                return last
        except (OSError, ValueError) as e:  # connection refused etc. while starting
            last = repr(e)
        time.sleep(interval)
    raise SetupError(f"timed out after {timeout}s waiting for {what}: {str(last)[:300]}")


def pull(docker, image, attempts=3, sleep=time.sleep):
    """Pull a digest-pinned image unless present; bounded retry (registry 429s)."""
    if docker("image", "inspect", "--format", "{{.Id}}", image, check=False)[0] == 0:
        return
    for i in range(attempts):
        rc, _, err = docker("pull", "-q", image, timeout=PULL_TIMEOUT, check=False)
        if rc == 0:
            return
        if i + 1 < attempts:
            sleep(10 * (i + 1))
    raise SetupError(f"docker pull {image.split('@')[0]} failed {attempts} times: {err.strip()[:300]}")


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def provision(docker, tmp, password):
    """Create networks/containers and start them. Returns the Grafana base URL."""
    env_file = os.path.join(tmp, "grafana.env")
    write_private(env_file, "".join(f"{k}={v}\n" for k, v in GRAFANA_ENV.items())
                  + f"GF_SECURITY_ADMIN_PASSWORD={password}\n")
    internal = docker.network("int", internal=True)
    public = docker.network("pub", internal=False)
    ro = "type=bind,readonly"
    docker.create("synthetic", IMAGES["python"], [
        "--network", internal, "--network-alias", "lto-synth", "--user", "65534:65534",
        "--mount", f"{ro},src={os.path.join(ROOT, 'lto_exporter')},dst=/src/lto_exporter",
        "--mount", f"{ro},src={os.path.join(HERE, 'synthetic_target.py')},dst=/smoke/synthetic_target.py",
    ], ["python3", "-I", "-B", "/smoke/synthetic_target.py", "/src"])
    docker.create("prometheus", IMAGES["prometheus"], [
        "--network", internal, "--network-alias", "prometheus",
        "--tmpfs", "/prometheus:rw,size=128m,uid=65534",
        "--mount", f"{ro},src={os.path.join(HERE, 'prometheus.yml')},dst=/etc/prometheus/prometheus.yml",
    ], ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus",
        "--web.listen-address=:9090"])
    docker.create("grafana", IMAGES["grafana"], [
        "--network", public, "-p", "127.0.0.1::3000", "--env-file", env_file,
        "--tmpfs", "/var/lib/grafana:rw,size=256m,uid=472",
        "--tmpfs", "/tmp:rw,size=64m,uid=472",
        "--mount", f"{ro},src={os.path.join(HERE, 'provisioning')},dst=/etc/grafana/provisioning",
        "--mount", f"{ro},src={DASHBOARD_DIR},dst=/var/lib/lto-dashboards",
    ])
    docker("network", "connect", internal, docker.name("grafana"))
    for role in ("synthetic", "prometheus", "grafana"):
        docker("start", docker.name(role))
    _, out, _ = docker("port", docker.name("grafana"), "3000/tcp")
    m = re.search(r"^127\.0\.0\.1:(\d+)$", out.strip().splitlines()[0] if out.strip() else "")
    if not m:
        raise SetupError(f"Grafana port not published on 127.0.0.1 only: {out.strip()!r}")
    return f"http://127.0.0.1:{m.group(1)}"


def collect_logs(docker, art):
    for role in ("synthetic", "prometheus", "grafana"):
        name = docker.name(role)
        if name not in docker.containers:
            continue
        try:
            _, out, err = docker("logs", "--tail", "400", name, timeout=60, check=False)
            with open(os.path.join(art, f"{role}.log"), "w", encoding="utf-8") as fh:
                fh.write(out + err)
        except Exception as e:  # logs are best-effort evidence
            print(f"warning: could not save {role} logs: {e}", file=sys.stderr)


def scrub(art, secret):
    """Redact the run's secret from every artifact. Returns files that held it."""
    hits = []
    needle = secret.encode()
    for dirpath, _, files in os.walk(art):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                data = fh.read()
            if needle in data:
                hits.append(os.path.relpath(p, art))
                with open(p, "wb") as fh:
                    fh.write(data.replace(needle, b"[REDACTED]"))
    return hits


def verify(graf, art, browser):
    """All checks against a running stack. Returns (failures, report)."""
    fails, report = [], {}
    with open(DASHBOARD_FILE, encoding="utf-8") as fh:
        repo = json.load(fh)

    def step(name, errs, detail=None):
        report[name] = {"ok": not errs, "errors": errs, **({"detail": detail} if detail else {})}
        fails.extend(f"[{name}] {e}" for e in errs)
        print(f"{'PASS' if not errs else 'FAIL'}  {name}" + "".join(f"\n      {e}" for e in errs),
              flush=True)

    _, info = graf.get("/api/health")
    report["grafana_version"] = (info or {}).get("version")
    st, body = graf.get(f"/api/datasources/uid/{DS_OK}/health")
    step("datasource health (real Prometheus)", check_ds_health(st, body, True), body)
    st, body = graf.get(f"/api/datasources/uid/{DS_BROKEN}/health")
    step("datasource health (unreachable, must fail)", check_ds_health(st, body, False), body)

    st, served = graf.get(f"/api/dashboards/uid/{DASHBOARD_UID}")
    _dump(art, "dashboard-served.json", served)
    step("dashboard provisioned from repo file",
         [f"HTTP {st}"] if st != 200 else check_provisioned(served, repo))
    step("no zero-filling defaults in dashboard", static_zero_defaults(repo))

    # wait until counters have >= 3 samples so increase()/rate() are non-zero
    def ready():
        errs, summ, _ = run_case(graf, repo, {"instance": [HEALTHY], "device": ["drive0"],
                                              "expect": {4: {"nonzero": True}, 7: {"nonzero": True}}})
        return not errs, errs
    wait_for(ready, "scraped data (non-zero increase on the healthy exporter)", 120, 2)

    errs, got = check_variables(graf, repo)
    step("variables resolved through datasource", errs, got)

    for case in CASES:
        errs, summary, queries = run_case(graf, repo, case)
        _dump(art, f"case-{_slug(case['name'])}.json", {"queries": queries, "results": summary})
        step(f"panel queries: {case['name']}", errs)

    # Negative control: the same verifier must report every query as an error
    errs, _, _ = run_case(graf, repo, {"instance": "All", "device": "All"}, ds_uid=DS_BROKEN)
    want = sum(len(p["targets"]) for p in repo["panels"])
    _dump(art, "case-unreachable-datasource.json", {"errors": errs})
    step("unreachable datasource: every panel query errors (negative control)",
         [] if len(errs) >= want
         else [f"only {len(errs)}/{want} query errors reported; verifier would miss datasource failures"])

    if browser:
        try:
            import browser_smoke
        except ImportError as e:
            step("browser smoke", [f"browser requested but unavailable: {e}"])
        else:
            errs, detail = browser_smoke.run(graf.base, ADMIN_USER, graf._auth, repo, art,
                                             {"healthy": HEALTHY, "failing": FAILING})
            step("browser smoke (headless Chromium; NOT a visual/layout review)", errs, detail)
    return fails, report


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _dump(art, name, obj):
    with open(os.path.join(art, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)


def write_summary(art, result):
    lines = [f"## Grafana dashboard smoke: {result['verdict']}", ""]
    lines.append("Real containers on this runner (not mocked):")
    lines += [f"- `{IMAGES[k]}`" for k in ("grafana", "prometheus", "python")]
    if result.get("grafana_version"):
        lines.append(f"- Grafana reported version `{result['grafana_version']}`")
    lines.append("")
    for name, r in result.get("steps", {}).items():
        if isinstance(r, dict) and "ok" in r:
            lines.append(f"- {'PASS' if r['ok'] else 'FAIL'} {name}")
            lines += [f"  - {e}" for e in r["errors"][:10]]
    if result.get("setup_error"):
        lines.append(f"- SETUP ERROR (smoke test did not complete): {result['setup_error']}")
    lines.append(f"- cleanup: {'clean' if not result['cleanup'] else result['cleanup']}")
    lines += ["", "Synthetic metrics only; no tape hardware. The browser check (if run) "
              "confirms the page loads and panels report no errors; it is not a visual review."]
    with open(os.path.join(art, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main(argv=None, docker_exec=_real_exec):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifacts", required=True)
    ap.add_argument("--browser", action="store_true", help="also run browser_smoke.py (Playwright)")
    ap.add_argument("--no-pull", action="store_true", help="images are already present")
    args = ap.parse_args(argv)
    os.makedirs(args.artifacts, exist_ok=True)
    art = os.path.abspath(args.artifacts)

    def on_term(signum, frame):
        raise Terminated(f"signal {signum}")
    old = {s: signal.signal(s, on_term) for s in (signal.SIGTERM, signal.SIGHUP)}

    run_id = secrets.token_hex(4)
    password = secrets.token_urlsafe(24)
    docker = Docker(run_id, exec_fn=docker_exec)
    tmp = tempfile.mkdtemp(prefix="lto-grafana-smoke-")
    result = {"run_id": run_id, "images": IMAGES, "steps": {}, "cleanup": []}
    rc = 2
    try:
        if not args.no_pull:
            for img in IMAGES.values():
                pull(docker, img)
        base = provision(docker, tmp, password)
        graf = Grafana(base, ADMIN_USER, password)
        wait_for(lambda: (graf.get("/api/health")[0] == 200, "starting"), "Grafana /api/health", 120)
        fails, report = verify(graf, art, args.browser)
        result["grafana_version"] = report.pop("grafana_version", None)
        result["steps"] = report
        result["failures"] = fails
        rc = 1 if fails else 0
    except (SetupError, Terminated) as e:
        result["setup_error"] = str(e)
        print(f"SETUP ERROR: {e}", file=sys.stderr)
        rc = 2
    finally:
        collect_logs(docker, art)
        result["cleanup"] = docker.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(tmp):
            result["cleanup"].append(f"temp dir not removed: {tmp}")
        for s, h in old.items():
            signal.signal(s, h)
        if any(password in " ".join(a) for a in docker.argv_log):
            result["cleanup"].append("secret appeared in docker argv")
        leaked = scrub(art, password)
        if leaked:
            result["cleanup"].append(f"secret found and redacted in artifacts: {leaked}")
        if result["cleanup"] and rc == 0:
            rc = 1
        result["verdict"] = {0: "PASS", 1: "FAIL", 2: "NOT RUN (setup error)"}[rc]
        _dump(art, "result.json", result)
        write_summary(art, result)
        print(f"result: {result['verdict']}; cleanup: {result['cleanup'] or 'clean'}")
    return rc


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    sys.exit(main())
