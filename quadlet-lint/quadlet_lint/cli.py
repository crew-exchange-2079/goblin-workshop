"""Command-line entry point: quadlet-lint [options] PATH..."""

import argparse
import json
import os
import sys

from . import __version__, unitfile
from .keydb import KeyDB, VersionError, fmt, parse_version
from .rules import KINDS, RULES, SEVERITY_ORDER, Context, run_rules

DOCS_URL = "https://github.com/crew-exchange-2079/goblin-workshop/blob/main/quadlet-lint/docs/rules.md"
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2


def discover(paths):
    """Expand directories into the Quadlet files below them, sorted; keep files as given."""
    files, errors = [], []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not d.endswith(".d"))
                for n in sorted(names):
                    if n.rsplit(".", 1)[-1] in KINDS:
                        files.append(os.path.join(root, n))
        elif os.path.exists(p):
            if p.rsplit(".", 1)[-1] in KINDS:
                files.append(p)
            else:
                errors.append(f"{p}: not a .container, .volume, .network or .pod file")
        else:
            errors.append(f"{p}: no such file or directory")
    return files, errors


def lint_text(text, path, target, db, selected=None):
    kind = path.rsplit(".", 1)[-1]
    unit = unitfile.parse(text, path)
    ctx = Context(unit, kind, target, db)
    findings = run_rules(ctx, set(RULES) if selected is None else selected)
    return sorted(findings, key=lambda f: (f.line, f.rule))


def _rule_set(spec, parser):
    ids = {s.strip().upper() for s in spec.split(",") if s.strip()}
    unknown = ids - set(RULES)
    if unknown:
        parser.error(f"unknown rule id(s): {', '.join(sorted(unknown))}")
    return ids


def build_parser():
    p = argparse.ArgumentParser(
        prog="quadlet-lint",
        description="Lint Podman Quadlet .container/.volume/.network/.pod files.",
        epilog=f"Rule reference: {DOCS_URL}")
    p.add_argument("paths", nargs="*", metavar="PATH", help="files or directories (searched recursively)")
    p.add_argument("--podman-version", metavar="X.Y",
                   help="Podman release the units must work with (default: newest known, %(default)s)")
    p.add_argument("--format", choices=("text", "json", "github"), default="text",
                   help="output format; 'github' prints workflow-command annotations")
    p.add_argument("--fail-on", choices=("error", "warning", "info", "never"), default="warning",
                   help="lowest severity that makes the exit status 1 (default: %(default)s)")
    p.add_argument("--min-severity", choices=("error", "warning", "info"), default="info",
                   help="hide findings below this severity (default: %(default)s)")
    p.add_argument("--select", metavar="IDS", help="only run these comma-separated rule ids")
    p.add_argument("--disable", metavar="IDS", help="skip these comma-separated rule ids")
    p.add_argument("--list-rules", action="store_true", help="print the rules and exit")
    p.add_argument("--explain", metavar="ID", help="print one rule's description and exit")
    p.add_argument("--version", action="version", version=f"quadlet-lint {__version__}")
    return p


def format_text(findings):
    return [f"{f.path}:{f.line}: {f.severity} {f.rule} [{f.name}] {f.message}" for f in findings]


def _gh_escape(s, prop=False):
    s = s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        s = s.replace(":", "%3A").replace(",", "%2C")
    return s


def format_github(findings):
    level = {"error": "error", "warning": "warning", "info": "notice"}
    return [f"::{level[f.severity]} file={_gh_escape(f.path, True)},line={f.line},"
            f"title={_gh_escape(f.rule + ' ' + f.name, True)}::{_gh_escape(f.message)}" for f in findings]


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    db = KeyDB.load()

    if args.list_rules:
        for r in RULES.values():
            print(f"{r.id}  {r.severity:<7}  {r.name:<32}  {r.summary}", file=out)
        return EXIT_OK
    if args.explain:
        r = RULES.get(args.explain.upper())
        if r is None:
            parser.error(f"unknown rule id: {args.explain}")
        kinds = ", ".join("." + k for k in KINDS if k in r.kinds)
        print(f"{r.id} {r.name} ({r.severity})\n{r.summary}\nApplies to: {kinds}\n"
              f"Details: {DOCS_URL}#{r.id.lower()}-{r.name}", file=out)
        return EXIT_OK
    if not args.paths:
        parser.error("no PATH given")

    if args.podman_version:
        try:
            target = parse_version(args.podman_version)
        except VersionError as e:
            parser.error(str(e))
        if target[:2] < db.oldest[:2]:
            parser.error(f"Quadlet first shipped in Podman {fmt(db.oldest)}; {args.podman_version} has no Quadlet")
        if target[:2] > db.newest[:2]:
            print(f"quadlet-lint: key table stops at Podman {fmt(db.newest)}; keys added after it "
                  "will be reported as unknown", file=err)
    else:
        target = db.newest

    selected = _rule_set(args.select, parser) if args.select else set(RULES)
    if args.disable:
        selected -= _rule_set(args.disable, parser)

    files, errors = discover(args.paths)
    for e in errors:
        print(f"quadlet-lint: {e}", file=err)
    if errors:
        return EXIT_USAGE

    floor = SEVERITY_ORDER[args.min_severity]
    findings = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"quadlet-lint: {path}: {e}", file=err)
            return EXIT_USAGE
        findings += [f for f in lint_text(text, path, target, db, selected) if SEVERITY_ORDER[f.severity] >= floor]

    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("error", "warning", "info")}
    if args.format == "json":
        json.dump({"version": 1, "tool": f"quadlet-lint {__version__}", "podman_version": fmt(target),
                   "files": files, "findings": [f.as_dict() for f in findings], "summary": counts},
                  out, indent=2)
        out.write("\n")
    else:
        lines = format_github(findings) if args.format == "github" else format_text(findings)
        for line in lines:
            print(line, file=out)
        print(f"{len(files)} file(s) checked against Podman {fmt(target)}: "
              f"{counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info", file=err)

    if args.fail_on == "never":
        return EXIT_OK
    threshold = SEVERITY_ORDER[args.fail_on]
    return EXIT_FINDINGS if any(SEVERITY_ORDER[f.severity] >= threshold for f in findings) else EXIT_OK
