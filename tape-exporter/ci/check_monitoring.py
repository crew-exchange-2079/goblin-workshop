"""Check that the Grafana dashboard and Prometheus rules only use metric
names, label names and label values that lto-exporter actually emits.

Standard library only. Used by tests/test_monitoring.py and by CI. With
--emit-promql FILE it also writes every dashboard query as a recording rule,
so CI can syntax-check them with `promtool check rules FILE`.

Usage: python3 ci/check_monitoring.py [--emit-promql FILE]
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from lto_exporter.exporter import (COLLECTOR_STATUSES, COLLECTORS,  # noqa: E402
                                   DIRECTIONS, HELP, METRIC_LABELS, SIDES)
from lto_exporter.parsers import PARAM_LABELS, SA_FIELDS  # noqa: E402

DASHBOARD = os.path.join(ROOT, "monitoring", "grafana", "lto-exporter-dashboard.json")
RULES = os.path.join(ROOT, "monitoring", "prometheus", "lto-exporter-alerts.yml")
TARGET_LABELS = {"instance", "job"}
KNOWN_VALUES = {
    "status": set(COLLECTOR_STATUSES),
    "collector": set(COLLECTORS),
    "direction": set(DIRECTIONS),
    "parameter": set(PARAM_LABELS) - {"bytes_processed"},
    "field": set(SA_FIELDS),
    "side": set(SIDES),
}
# Grafana variables -> something PromQL accepts, for syntax checking.
GRAFANA_VARS = {"$__rate_interval": "5m", "$instance": ".*", "$device": ".*"}

_METRIC_RE = re.compile(r"\b(tape_[a-z_]+)\b")
_SELECTOR_RE = re.compile(r"\b(tape_[a-z_]+|up)\s*\{([^}]*)\}")
_MATCHER_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')
_BY_RE = re.compile(r"\b(?:by|without|on|ignoring)\s*\(([^)]*)\)")
_FUNCS = {"increase", "rate", "sum", "max", "min", "count", "avg", "label_values"}


def dashboard_exprs(path=DASHBOARD):
    with open(path, encoding="utf-8") as fh:
        dash = json.load(fh)
    out = []
    for p in dash.get("panels", []):
        for t in p.get("targets", []):
            out.append((f"panel {p.get('id')} {p.get('title')!r}", t["expr"]))
    for v in dash.get("templating", {}).get("list", []):
        q = v.get("query")
        if isinstance(q, dict):
            q = q.get("query")
        if v.get("type") == "query" and q:
            m = re.fullmatch(r"label_values\((.*),\s*([a-z_]+)\)", q)
            if not m:
                raise ValueError(f"variable {v['name']}: unsupported query {q!r}")
            out.append((f"variable {v['name']}", m.group(1)))
            out.append((f"variable {v['name']} label", f"__label__:{m.group(1)}:{m.group(2)}"))
    return dash, out


def rule_exprs(path=RULES):
    """Tiny extractor for `expr:` values (inline or `|` block) in our rules file."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, i, alert = [], 0, None
    while i < len(lines):
        line = lines[i]
        m = re.match(r"\s*- alert:\s*(\S+)", line)
        if m:
            alert = m.group(1)
        m = re.match(r"(\s*)expr:\s*(.*)$", line)
        if m:
            indent, rest = len(m.group(1)), m.group(2).strip()
            if rest in ("|", ">", "|-", ">-"):
                block = []
                i += 1
                while i < len(lines) and (not lines[i].strip() or
                                          len(lines[i]) - len(lines[i].lstrip()) > indent):
                    block.append(lines[i].strip())
                    i += 1
                out.append((f"alert {alert}", " ".join(block).strip()))
                continue
            out.append((f"alert {alert}", rest))
        i += 1
    return out


def _check_values(where, label, op, value, errors):
    known = KNOWN_VALUES.get(label)
    if known is None or op not in ("=", "=~", "!=", "!~"):
        return
    if value.startswith("$"):
        return
    alts = value.split("|") if op in ("=~", "!~") else [value]
    for alt in alts:
        if alt not in known:
            errors.append(f"{where}: {label}={alt!r} is never emitted (known: {sorted(known)})")


def check_expr(where, expr, errors):
    if expr.startswith("__label__:"):
        _, sel, label = expr.split(":", 2)
        metrics = set(_METRIC_RE.findall(sel))
        allowed = TARGET_LABELS.union(*(METRIC_LABELS.get(m, ()) for m in metrics))
        if label not in allowed:
            errors.append(f"{where}: label {label!r} not on {sorted(metrics)}")
        return
    metrics = set(_METRIC_RE.findall(expr))
    if not metrics and "up{" not in expr.replace(" ", ""):
        errors.append(f"{where}: references no exporter metric")
    for m in metrics:
        if m not in HELP:
            errors.append(f"{where}: unknown metric {m!r}")
    for metric, body in _SELECTOR_RE.findall(expr):
        allowed = TARGET_LABELS if metric == "up" else TARGET_LABELS | set(METRIC_LABELS.get(metric, ()))
        for part in [p for p in body.split(",") if p.strip()]:
            mm = _MATCHER_RE.fullmatch(part)
            if not mm:
                errors.append(f"{where}: can't parse matcher {part!r}")
                continue
            label, op, value = mm.groups()
            if label not in allowed:
                errors.append(f"{where}: {metric} has no label {label!r}")
            _check_values(where, label, op, value, errors)
    union = TARGET_LABELS.union(*(METRIC_LABELS.get(m, ()) for m in metrics))
    for group in _BY_RE.findall(expr):
        for label in [g.strip() for g in group.split(",") if g.strip()]:
            if label not in union:
                errors.append(f"{where}: grouping label {label!r} not on {sorted(metrics)}")
    for legend_label in re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", expr):
        if legend_label not in union:
            errors.append(f"{where}: legend label {legend_label!r} not available")


def check_dashboard_structure(dash, errors):
    for key in ("uid", "title", "panels", "schemaVersion", "templating"):
        if key not in dash:
            errors.append(f"dashboard: missing {key!r}")
    ids = [p.get("id") for p in dash.get("panels", [])]
    if len(ids) != len(set(ids)):
        errors.append("dashboard: duplicate panel ids")
    for p in dash.get("panels", []):
        if p.get("datasource", {}).get("uid") != "${datasource}":
            errors.append(f"panel {p.get('id')}: datasource must be ${{datasource}}")
        gp = p.get("gridPos", {})
        if not all(isinstance(gp.get(k), int) for k in ("x", "y", "w", "h")) or gp.get("x", 0) + gp.get("w", 0) > 24:
            errors.append(f"panel {p.get('id')}: bad gridPos {gp}")
        if not p.get("targets"):
            errors.append(f"panel {p.get('id')}: no targets")
        for t in p.get("targets", []):
            union = TARGET_LABELS.union(*(METRIC_LABELS.get(m, ()) for m in _METRIC_RE.findall(t["expr"])))
            for legend_label in re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", t.get("legendFormat", "")):
                if legend_label not in union:
                    errors.append(f"panel {p.get('id')}: legend label {legend_label!r} not available")


def to_promql(expr):
    for k, v in GRAFANA_VARS.items():
        expr = expr.replace(k, v)
    return expr


def run(emit=None):
    errors = []
    dash, dexprs = dashboard_exprs()
    check_dashboard_structure(dash, errors)
    rexprs = rule_exprs()
    if not rexprs:
        errors.append("rules: no expressions found")
    for where, expr in dexprs + rexprs:
        check_expr(where, expr, errors)
    if emit:
        with open(emit, "w", encoding="utf-8") as fh:
            fh.write("groups:\n  - name: dashboard-queries\n    rules:\n")
            for i, (where, expr) in enumerate(e for e in dexprs if not e[1].startswith("__label__:")):
                fh.write(f"      - record: dashboard:query_{i}\n        expr: {json.dumps(to_promql(expr))}\n")
    return errors, len(dexprs), len(rexprs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-promql")
    args = ap.parse_args()
    errors, nd, nr = run(args.emit_promql)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    print(f"checked {nd} dashboard and {nr} alert expressions: "
          f"{'FAIL' if errors else 'OK'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
