"""Command-line entry point: dagu-lint [options] PATH..."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import sys

import yaml

from . import __version__, cron
from .config import ConfigError, load
from . import secrets
from .loader import DuplicateKey, load_documents
from .rules import RULES, Finding, Linter

MAX_BYTES = 1 << 20
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2

_DISABLE_RE = re.compile(r"#\s*dagu-lint:\s*disable(-file)?=([A-Z0-9, ]+)")


def discover(paths, exclude):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for f in sorted(files):
                    if f.endswith((".yaml", ".yml")):
                        out.append(os.path.join(root, f))
        else:
            out.append(p)
    return [f for f in out if not any(fnmatch.fnmatch(f, g) or fnmatch.fnmatch(os.path.basename(f), g)
                                      for g in exclude)]


def suppressions(text):
    """({line: codes}, file_codes). A `# dagu-lint: disable=DL403` comment
    applies to its own line and the line below it."""
    per_line: dict = {}
    whole: set = set()
    for n, line in enumerate(text.splitlines(), 1):
        for m in _DISABLE_RE.finditer(line):
            codes = {c.strip() for c in m.group(2).split(",") if c.strip()}
            if m.group(1):
                whole |= codes
            else:
                per_line.setdefault(n, set()).update(codes)
                per_line.setdefault(n + 1, set()).update(codes)
    return per_line, whole


def lint_file(path, cfg, now):
    """Returns (findings, infos)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as e:
        return [Finding("DL004", f"cannot read file: {e.strerror}")], []
    if len(raw) > MAX_BYTES:
        return [Finding("DL004", f"file is larger than {MAX_BYTES} bytes")], []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Finding("DL004", "file is not UTF-8")], []
    try:
        docs = load_documents(text)
    except DuplicateKey as e:
        return [Finding("DL002", secrets.redact(str(e)), e.line, e.col)], []
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        problem = secrets.redact(getattr(e, "problem", None) or "invalid YAML")
        return [Finding("DL001", problem, mark.line + 1 if mark else 0, mark.column + 1 if mark else 0)], []
    docs = [d for d in docs if d is not None] or [None]
    findings, infos = [], []
    linter = Linter(cfg, now)
    for i, doc in enumerate(docs):
        f, inf = linter.lint_dag(doc, i, is_main=(i == 0))
        findings += f
        infos += inf
    per_line, whole = suppressions(text)
    kept = [f for f in findings
            if f.code not in cfg.disable and f.code not in whole and f.code not in per_line.get(f.line, ())]
    kept.sort(key=lambda f: (f.line, f.col, f.code))
    # Messages and schedule summaries can echo input (a cron parse error, a
    # bad queue name); scrub every value a secret rule matched, file-wide.
    for f in kept:
        f.message = linter.redact(f.message)
    for i in infos:
        i.where, i.expression, i.description = map(linter.redact, (i.where, i.expression, i.description))
    return kept, infos


def _gh_escape(s, prop=False):
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        s = s.replace(":", "%3A").replace(",", "%2C")
    return s


def render(results, fmt, strict):
    lines = []
    if fmt == "json":
        payload = {
            "version": __version__,
            "files": [
                {
                    "path": path,
                    "findings": [
                        {"code": f.code, "severity": f.severity, "title": RULES[f.code][1], "message": f.message,
                         "line": f.line, "column": f.col, "document": f.doc}
                        for f in findings
                    ],
                    "schedules": [
                        {"where": i.where, "expression": i.expression, "description": i.description,
                         "next_runs": i.next_runs, "line": i.line}
                        for i in infos
                    ],
                }
                for path, findings, infos in results
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)
    for path, findings, infos in results:
        for f in findings:
            if fmt == "github":
                level = "error" if f.severity == "error" or strict else "warning"
                props = f"file={_gh_escape(path, True)}"
                if f.line:
                    props += f",line={f.line},col={f.col or 1}"
                props += f",title={_gh_escape(f.code + ' ' + RULES[f.code][1], True)}"
                lines.append(f"::{level} {props}::{_gh_escape(f.message)}")
            else:
                loc = f"{path}:{f.line}:{f.col}" if f.line else path
                lines.append(f"{loc}: {f.severity} {f.code} {f.message}")
        for i in infos:
            runs = ", ".join(i.next_runs)
            text = f"{path}:{i.line}: info {i.where} {i.expression!r}: {i.description}; next: {runs}"
            lines.append(f"::notice file={_gh_escape(path, True)},line={i.line}::{_gh_escape(text)}"
                         if fmt == "github" else text)
    return "\n".join(lines)


def parse_now(text):
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from e
    if t.tzinfo is None:
        raise argparse.ArgumentTypeError("--now needs an explicit offset, e.g. 2026-01-01T00:00:00Z")
    return t


def build_parser():
    p = argparse.ArgumentParser(prog="dagu-lint", description="Policy linter for Dagu DAG YAML files.")
    p.add_argument("paths", nargs="+", help="DAG files or directories (searched for *.yaml / *.yml)")
    p.add_argument("-c", "--config", help="TOML policy file (default: ./dagu-lint.toml if present)")
    p.add_argument("-q", "--queue", action="append", default=[], help="allowed queue (repeatable; adds to config)")
    p.add_argument("--format", choices=("text", "json", "github"), default="text")
    p.add_argument("--strict", action="store_true", help="warnings also fail (exit 1)")
    p.add_argument("--disable", action="append", default=[], metavar="CODE", help="disable a rule (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip matching files")
    p.add_argument("--tz", help="timezone for schedules without CRON_TZ= (the Dagu server's; default UTC)")
    p.add_argument("--next-runs", type=int, help="how many upcoming runs to show per schedule (default 3)")
    p.add_argument("--now", type=parse_now, help="reference time for next runs (ISO 8601; default: now)")
    p.add_argument("--version", action="version", version=f"dagu-lint {__version__}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    config_path = args.config
    if config_path is None and os.path.isfile("dagu-lint.toml"):
        config_path = "dagu-lint.toml"
    try:
        cfg = load(config_path)
    except ConfigError as e:
        print(f"dagu-lint: {e}", file=sys.stderr)
        return EXIT_USAGE
    cfg.queues = list(dict.fromkeys(cfg.queues + args.queue))
    cfg.exclude += args.exclude
    unknown = {c for c in args.disable if c not in RULES} | {c for c in cfg.disable if c not in RULES}
    if unknown:
        print(f"dagu-lint: unknown rule code(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return EXIT_USAGE
    cfg.disable |= set(args.disable)
    if args.tz:
        cfg.timezone = args.tz
    if args.next_runs is not None:
        if args.next_runs < 0:
            print("dagu-lint: --next-runs must not be negative", file=sys.stderr)
            return EXIT_USAGE
        cfg.next_runs = args.next_runs
    try:
        cron.resolve_tz(cfg.timezone)
    except cron.CronError as e:
        print(f"dagu-lint: {e}", file=sys.stderr)
        return EXIT_USAGE
    if not cfg.queues and cfg.require_queue:
        print("dagu-lint: warning: no queue allowlist configured; queues are required but not checked "
              "against a list (use --queue or [queues] allow)", file=sys.stderr)
    files = discover(args.paths, cfg.exclude)
    if not files:
        print("dagu-lint: no DAG files found", file=sys.stderr)
        return EXIT_USAGE
    now = args.now or dt.datetime.now(dt.timezone.utc)
    results = []
    for path in files:
        findings, infos = lint_file(path, cfg, now)
        results.append((path, findings, infos))
    out = render(results, args.format, args.strict)
    if out:
        print(out)
    errors = sum(f.severity == "error" for _, fs, _ in results for f in fs)
    warnings = sum(f.severity == "warning" for _, fs, _ in results for f in fs)
    if args.format != "json":
        print(f"dagu-lint: {len(files)} file(s), {errors} error(s), {warnings} warning(s)", file=sys.stderr)
    return EXIT_FINDINGS if errors or (args.strict and warnings) else EXIT_OK
