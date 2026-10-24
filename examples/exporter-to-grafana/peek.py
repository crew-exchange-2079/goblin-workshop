"""Read-only HTTP helper for the exporter -> Grafana walkthrough.

Each subcommand does one GET/POST against the stack started by compose.yaml
and prints a short, stable summary instead of raw JSON. The README lists the
equivalent curl command for each. Standard library only.

  wait             until Prometheus has scraped both exporters 3 times and Grafana is up
  metrics          tape_* sample lines from the healthy exporter's /metrics
  targets          Prometheus scrape targets and their health
  query EXPR       an instant PromQL query, one line per series
  alerts           alert rules that are pending or firing
  dashboard        the provisioned dashboard, then one of its panels run
                   through Grafana's datasource proxy (/api/ds/query)

Ports come from EXPORTER_PORT, PROMETHEUS_PORT and GRAFANA_PORT (defaults
9101, 9090, 3000), as in compose.yaml. Exit 1 if a check fails.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXPORTER = f"http://127.0.0.1:{os.environ.get('EXPORTER_PORT', '9101')}"
PROMETHEUS = f"http://127.0.0.1:{os.environ.get('PROMETHEUS_PORT', '9090')}"
GRAFANA = f"http://127.0.0.1:{os.environ.get('GRAFANA_PORT', '3000')}"
DASHBOARD_UID = "lto-exporter-v1"
PANEL = "Cleaning required"


def get(url, data=None, timeout=5):
    headers = {"Accept": "application/json"}
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if resp.headers.get_content_type() == "application/json" else body


def prom(expr):
    q = urllib.parse.urlencode({"query": expr})
    return get(f"{PROMETHEUS}/api/v1/query?{q}")["data"]["result"]


def labels(metric, skip=("__name__", "job")):
    return ",".join(f"{k}={v}" for k, v in sorted(metric.items()) if k not in skip)


def cmd_wait(timeout=180):
    deadline = time.monotonic() + timeout
    last = "nothing tried yet"
    while time.monotonic() < deadline:
        try:
            # three scrapes of each exporter, so rate() has samples to work with
            seen = prom("count_over_time(tape_exporter_build_info[1m]) >= 3")
            if len(seen) < 2:
                raise RuntimeError(f"Prometheus has 3 scrapes of {len(seen)}/2 exporters")
            if get(f"{GRAFANA}/api/health").get("database") != "ok":
                raise RuntimeError("Grafana database not ready")
            print("stack ready: Prometheus scrapes 2 exporters, Grafana is up")
            return 0
        except (OSError, RuntimeError, ValueError, KeyError) as e:
            last = str(e)
        time.sleep(2)
    print(f"stack not ready after {timeout}s: {last}", file=sys.stderr)
    return 1


def cmd_metrics():
    lines = [l for l in get(f"{EXPORTER}/metrics").splitlines()
             if l.startswith(("tape_cleaning_required", "tape_data_bytes_total",
                              "tape_exporter_collector_success"))]
    print("\n".join(lines))
    return 0 if lines else 1


def cmd_targets():
    targets = get(f"{PROMETHEUS}/api/v1/targets")["data"]["activeTargets"]
    for t in sorted(targets, key=lambda t: t["labels"]["instance"]):
        print(f"{t['labels']['job']:<13} {t['labels']['instance']:<15} {t['health']}")
    return 0 if targets and all(t["health"] == "up" for t in targets) else 1


def cmd_query(expr):
    result = prom(expr)
    for r in sorted(result, key=lambda r: labels(r["metric"])):
        print(f"{labels(r['metric']) or '{}'}  {r['value'][1]}")
    return 0 if result else 1


def cmd_alerts():
    alerts = get(f"{PROMETHEUS}/api/v1/alerts")["data"]["alerts"]
    for a in sorted(alerts, key=lambda a: (a["labels"]["alertname"], labels(a["labels"]))):
        rest = labels(a["labels"], skip=("alertname", "job", "severity"))
        print(f"{a['labels']['alertname']:<30} {a['state']:<8} {rest}")
    return 0 if alerts else 1


def cmd_dashboard():
    dash = get(f"{GRAFANA}/api/dashboards/uid/{DASHBOARD_UID}")["dashboard"]
    print(f"dashboard: {dash['title']} ({len(dash['panels'])} panels) "
          f"{GRAFANA}/d/{DASHBOARD_UID}")
    panel = next(p for p in dash["panels"] if p["title"] == PANEL)
    expr = panel["targets"][0]["expr"].replace("$instance", ".*").replace("$device", ".*")
    now = int(time.time() * 1000)
    body = {"from": str(now - 300_000), "to": str(now), "queries": [{
        "refId": "A", "expr": expr, "instant": True,
        "datasource": {"type": "prometheus", "uid": "lto-walkthrough-prom"}}]}
    frames = get(f"{GRAFANA}/api/ds/query", data=body)["results"]["A"].get("frames", [])
    print(f"panel '{PANEL}' via Grafana: {expr}")
    rows = []
    for f in frames:
        fields = f["schema"]["fields"]
        value = fields[-1]
        device = (value.get("labels") or {}).get("device", "?")
        rows.append((device, f["data"]["values"][-1][-1]))
    for device, v in sorted(rows):
        print(f"  device={device}  cleaning_required={v:g}")
    return 0 if rows else 1


def main(argv):
    if not argv or argv[0] not in {"wait", "metrics", "targets", "query", "alerts", "dashboard"}:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        return globals()[f"cmd_{cmd}"](*args)
    except (urllib.error.URLError, OSError, KeyError, StopIteration) as e:
        print(f"{cmd}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
