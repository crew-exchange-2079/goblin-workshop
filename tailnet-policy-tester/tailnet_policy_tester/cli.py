"""Command line: validate, check, ssh, test, diff.

Exit codes: 0 allowed / all tests pass / no diff; 1 denied / a test failed /
policies differ; 2 bad input (parse, validation, query); 3 inconclusive
(the answer depends on srcPosture or via, which are not evaluated).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .diff import diff
from .engine import (QueryError, check_network, check_ssh, load_inventory,
                     norm_proto, parse_target, resolve)
from .policy import PORTED_PROTOS, PolicyError, load_policy
from .runtests import run_all

OK, NO, BAD, MAYBE = 0, 1, 2, 3


def _read(path):
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def _load(args):
    pol = load_policy(_read(args.policy))
    inv = load_inventory(_read(args.inventory)) if getattr(args, "inventory", None) else None
    return pol, inv


def _emit(args, obj, lines):
    if args.json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        for line in lines:
            print(line)


def _warnings(args, pol):
    if not args.json:
        for w in pol.warnings:
            print(f"warning: {w}", file=sys.stderr)


def cmd_validate(args):
    pol, _ = _load(args)
    _emit(args, {"valid": True, "warnings": pol.warnings,
                 "rules": len(pol.rules), "ssh": len(pol.ssh),
                 "tests": len(pol.tests), "sshTests": len(pol.ssh_tests)},
          [f"valid: {len(pol.rules)} network rules, {len(pol.ssh)} ssh rules, "
           f"{len(pol.tests)} tests, {len(pol.ssh_tests)} sshTests"] +
          [f"warning: {w}" for w in pol.warnings])
    return OK


def cmd_check(args):
    pol, inv = _load(args)
    _warnings(args, pol)
    proto = norm_proto(args.proto)
    alias, port = parse_target(args.dst)
    if proto in PORTED_PROTOS and port is None:
        raise QueryError(f"{proto} needs a port: --dst {alias}:PORT")
    srcs, dsts = resolve(pol, inv, args.src), resolve(pol, inv, alias)
    rows, verdicts = [], []
    for s in srcs:
        for d in dsts:
            dec = check_network(pol, s, d, proto, port)
            verdicts.append(dec.verdict)
            rows.append({"src": s.name, "dst": d.name, "proto": proto, "port": port,
                         "verdict": dec.verdict, "rules": dec.matched,
                         "conditions": dec.conditions})
    lines = [f"{r['verdict']:<11} {r['src']} -> {r['dst']} {proto}"
             f"{'' if port is None else ':' + str(port)}"
             f"{'  via ' + ', '.join(r['rules']) if r['rules'] else ''}"
             f"{'  if ' + '; '.join(r['conditions']) if r['conditions'] else ''}"
             for r in rows]
    _emit(args, {"results": rows}, lines)
    if all(v == "allow" for v in verdicts):
        return OK
    return MAYBE if all(v in ("allow", "conditional") for v in verdicts) else NO


def cmd_ssh(args):
    pol, inv = _load(args)
    _warnings(args, pol)
    srcs, dsts = resolve(pol, inv, args.src), resolve(pol, inv, args.dst)
    rows = []
    for s in srcs:
        for d in dsts:
            dec = check_ssh(pol, s, d, args.user, require_network=not args.ssh_only)
            rows.append({"src": s.name, "dst": d.name, "user": args.user,
                         "verdict": dec.verdict, "action": dec.action,
                         "rules": [{"rule": o, "action": a} for o, a in dec.matched],
                         "checkPeriod": dec.check_period,
                         "network": dec.network.verdict if dec.network else None})
    lines = []
    for r in rows:
        extra = ""
        if r["rules"]:
            extra = "  via " + ", ".join(f"{x['rule']} ({x['action']})" for x in r["rules"])
        if r["action"] == "check":
            extra += f"  checkPeriod={r['checkPeriod']}"
        lines.append(f"{r['verdict']:<6} {r['src']} -> {r['dst']} as {r['user']}{extra}")
    _emit(args, {"results": rows}, lines)
    if all(r["verdict"] in ("accept", "check") for r in rows):
        return OK
    return MAYBE if all(r["verdict"] in ("accept", "check") or r["verdict"].startswith("conditional")
                        for r in rows) else NO


def cmd_test(args):
    pol, inv = _load(args)
    _warnings(args, pol)
    results = run_all(pol, inv)
    lines = []
    for r in results:
        lines.append(f"{r.status:<12} {r.origin}")
        lines += [f"    {f}" for f in r.failures + r.inconclusive]
    fails = sum(not r.passed for r in results)
    maybe = sum(r.passed and bool(r.inconclusive) for r in results)
    lines.append(f"{len(results)} tests: {len(results) - fails - maybe} passed, "
                 f"{fails} failed, {maybe} inconclusive")
    _emit(args, {"results": [{"test": r.origin, "status": r.status,
                              "failures": r.failures, "inconclusive": r.inconclusive}
                             for r in results]}, lines)
    if fails:
        return NO
    return MAYBE if maybe else OK


def cmd_diff(args):
    old = load_policy(_read(args.old))
    new = load_policy(_read(args.new))
    inv = load_inventory(_read(args.inventory)) if args.inventory else None
    changes, notes = diff(old, new, inv.devices if inv else None)
    lines = [str(c) for c in changes] or ["no changes in reachability or SSH access"]
    lines += [f"note: {n}" for n in notes]
    _emit(args, {"changes": [c.__dict__ for c in changes], "notes": notes}, lines)
    return NO if changes else OK


def build_parser():
    ap = argparse.ArgumentParser(prog="tailnet-policy-tester",
                                 description="Offline evaluator for Tailscale policy files (HuJSON).")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, inventory=True):
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if inventory:
            p.add_argument("--inventory", help="HuJSON device inventory (see README)")

    p = sub.add_parser("validate", help="parse and validate a policy")
    p.add_argument("policy")
    common(p, inventory=False)
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("check", help="can SRC reach DST:PORT?")
    p.add_argument("policy")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True, help="host:port, or host for portless protocols")
    p.add_argument("--proto", default="tcp")
    common(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("ssh", help="can SRC ssh as USER to DST?")
    p.add_argument("policy")
    p.add_argument("--src", required=True)
    p.add_argument("--dst", required=True)
    p.add_argument("--user", required=True, help="local login name, e.g. root")
    p.add_argument("--ssh-only", action="store_true",
                   help="ignore whether acls/grants allow tcp/22")
    common(p)
    p.set_defaults(fn=cmd_ssh)

    p = sub.add_parser("test", help="run the policy's tests and sshTests blocks")
    p.add_argument("policy")
    common(p)
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("diff", help="show flows newly allowed or denied by NEW")
    p.add_argument("old")
    p.add_argument("new")
    common(p)
    p.set_defaults(fn=cmd_diff)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except PolicyError as e:
        for msg in e.errors:
            print(f"error: {msg}", file=sys.stderr)
        return BAD
    except QueryError as e:
        print(f"error: {e}", file=sys.stderr)
        return BAD
    except OSError as e:
        print(f"error: {e.filename or ''}: {e.strerror}", file=sys.stderr)
        return BAD
