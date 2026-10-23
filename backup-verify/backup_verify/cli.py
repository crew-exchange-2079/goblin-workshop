"""Command line: backup-verify run|check-config --config FILE.

Exit status: 0 every check passed, 1 at least one failed, 2 usage or
configuration error (nothing was run, no files were written).
"""

import argparse
import os
import sys
import time

from . import __version__, config, postgres, report, restic


def run(cfg, only=(), now=time.time, environ=None, log=None):
    rep = report.Report(started=now())
    for t in cfg.targets:
        if only and t.name not in only:
            continue
        if log:
            log(f"backup-verify: {t.kind} target {t.name} ...")
        try:
            if t.kind == "restic":
                res = restic.verify(t, cfg.work_dir, now=now, environ=environ)
            else:
                res = postgres.verify(cfg, t, now=now, environ=environ)
        except Exception as e:  # a bug in one target must not lose the others' results
            res = report.Target(t.name, t.kind, checks=[report.Check(
                "internal", error=f"internal error: {type(e).__name__}: {e}")])
        rep.targets.append(res)
        if log:
            for c in res.checks:
                extra = f": {c.error}" if c.error else ""
                log(f"  {c.name:<15} {c.status:<8} {c.duration_seconds:7.1f}s{extra}")
    rep.finished = now()
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="backup-verify",
        description="Prove restic and PostgreSQL backups can be restored.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="run every configured check")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--target", action="append", default=[], metavar="NAME",
                       help="only this target (repeatable)")
    p_run.add_argument("--textfile", help="override textfile_path (.prom)")
    p_run.add_argument("--report", help="override report_path; '-' for stdout")
    p_run.add_argument("--quiet", action="store_true", help="no progress on stderr")
    p_chk = sub.add_parser("check-config", help="validate the config and exit")
    p_chk.add_argument("--config", required=True)
    args = ap.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except config.ConfigError as e:
        print(f"backup-verify: config error: {e}", file=sys.stderr)
        return 2
    if args.command == "check-config":
        print(f"backup-verify: {args.config}: {len(cfg.targets)} target(s) OK")
        return 0

    names = {t.name for t in cfg.targets}
    unknown = [n for n in args.target if n not in names]
    if unknown:
        print(f"backup-verify: unknown target(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    textfile = args.textfile or cfg.textfile_path
    report_path = args.report or cfg.report_path
    if textfile and not textfile.endswith(".prom"):
        print("backup-verify: --textfile must end in .prom", file=sys.stderr)
        return 2
    if cfg.work_dir and not os.path.isdir(cfg.work_dir):
        print(f"backup-verify: work_dir {cfg.work_dir} is not a directory", file=sys.stderr)
        return 2

    log = None if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    rep = run(cfg, only=set(args.target), log=log)

    status = 0 if rep.ok else 1
    if textfile:
        try:
            report.atomic_write(textfile, report.render_metrics(rep))
        except OSError as e:
            print(f"backup-verify: cannot write {textfile}: {e}", file=sys.stderr)
            status = 1
    if report_path == "-":
        sys.stdout.write(report.render_json(rep))
    elif report_path:
        try:
            report.atomic_write(report_path, report.render_json(rep))
        except OSError as e:
            print(f"backup-verify: cannot write {report_path}: {e}", file=sys.stderr)
            status = 1
    if log:
        log(f"backup-verify: {'OK' if rep.ok else 'FAILED'}")
    return status
