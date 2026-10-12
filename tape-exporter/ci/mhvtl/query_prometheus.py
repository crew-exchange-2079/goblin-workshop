"""Evaluate every dashboard query and alert expression against a live
Prometheus that scraped the exporter during the mhvtl run.

Passes if: every query is accepted (status=success); the queries that must
have data do; rules are loaded with health ok; and no alert is pending or
firing for a healthy emulated drive. Writes the raw results to --out.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import check_monitoring as cm  # noqa: E402
from check_evidence import supported_pages  # noqa: E402

ALWAYS_DATA = ("tape_exporter_collector_success", "tape_exporter_collector_status",
               "tape_exporter_unreaped_children", "tape_exporter_collector_duration_seconds")
COUNTER_DATA = ("tape_log_errors_total", "tape_bytes_processed_total")


def get(url, path, **params):
    q = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}{path}?{q}", timeout=15) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--supported", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    sup = supported_pages(args.supported, args.supported[:-4] + ".rc")
    both_pages = sup is not None and {0x02, 0x03} <= sup

    _, dexprs = cm.dashboard_exprs()
    results, problems = [], []
    for where, expr in dexprs + cm.rule_exprs():
        if expr.startswith("__label__:"):
            continue
        q = cm.to_promql(expr)
        res = get(args.url, "/api/v1/query", query=q, time=f"{time.time():.3f}")
        n = len(res.get("data", {}).get("result", []))
        results.append({"where": where, "query": q, "status": res.get("status"), "series": n})
        if res.get("status") != "success":
            problems.append(f"{where}: {res}")
        elif where.startswith("panel") and "== 0" not in q and "== 1" not in q:
            if any(m in q for m in ALWAYS_DATA) and n == 0:
                problems.append(f"{where}: no data")
            if any(m in q for m in COUNTER_DATA) and both_pages and n == 0:
                problems.append(f"{where}: no counter data although pages are supported")
    rules = get(args.url, "/api/v1/rules")
    unhealthy = [r["name"] for g in rules["data"]["groups"] for r in g["rules"] if r.get("health") != "ok"]
    alerts = get(args.url, "/api/v1/alerts")["data"]["alerts"]
    if unhealthy:
        problems.append(f"unhealthy rules: {unhealthy}")
    if not both_pages:
        alerts = [a for a in alerts if a["labels"].get("alertname") != "LTOLogPageUnsupported"]
    if alerts:
        problems.append(f"alerts active on a healthy emulated drive: {alerts}")
    out = {"queries": results, "unhealthy_rules": unhealthy, "active_alerts": alerts,
           "problems": problems, "result": "FAIL" if problems else "PASS"}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps({"result": out["result"], "problems": problems,
                      "queries": len(results)}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
