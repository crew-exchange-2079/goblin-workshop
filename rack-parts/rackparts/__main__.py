"""Command line: python3 -m rackparts {schema,validate,generate,check-stl,spec-check,catalog} ...

Exit codes: 0 ok, 2 input refused, 3 tool failure / output exists, 4 mesh rejected.
"""

import argparse
import json
import os
import sys

from . import generate as gen
from . import catalog, mesh, spec, speccheck, validate


def _refused(e):
    print("REFUSED: input failed validation:", file=sys.stderr)
    for p in e.problems:
        print("  - " + p, file=sys.stderr)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rackparts", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the JSON Schema for input files")
    v = sub.add_parser("validate", help="validate an input file and print derived dimensions")
    v.add_argument("config")
    g = sub.add_parser("generate", help="validate, write SCAD, render STL, check mesh, write manifest")
    g.add_argument("config")
    g.add_argument("--out", required=True, help="output directory; must not exist yet")
    g.add_argument("--scad-only", action="store_true", help="write SCAD + manifest without rendering")
    g.add_argument("--openscad", help="path to the openscad binary (default: from PATH)")
    g.add_argument("--timeout", type=int, default=300, help="render timeout in seconds (default 300)")
    g.add_argument("--png", action="store_true", help="also write an orthographic PNG preview")
    c = sub.add_parser("check-stl", help="run the mesh checks on an existing STL")
    c.add_argument("stl")
    s = sub.add_parser("spec-check", help="re-run the part's spec probes on a generate output directory")
    s.add_argument("out_dir")
    k = sub.add_parser("catalog", help="render every example and write the catalog page + images")
    kg = k.add_mutually_exclusive_group(required=True)
    kg.add_argument("--out", help="output directory; must not exist yet")
    kg.add_argument("--check", metavar="DIR", help="rebuild and compare with a committed catalog (exit 1 if stale)")
    k.add_argument("--openscad", help="path to the openscad binary (default: from PATH)")
    a = ap.parse_args(argv)

    if a.cmd == "schema":
        print(json.dumps(spec.json_schema(), indent=2))
        return 0
    if a.cmd == "check-stl":
        try:
            rep = mesh.analyse(mesh.read_stl(a.stl))
        except (OSError, mesh.MeshError) as e:
            print("REJECTED: %s" % e, file=sys.stderr)
            return 4
        rep["admesh"] = mesh.run_admesh(a.stl)
        if rep["admesh"] and not rep["admesh"]["ok"]:
            rep["ok"] = False
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 4

    if a.cmd == "spec-check":
        try:
            rep = speccheck.check_output(a.out_dir)
        except (OSError, ValueError, KeyError, mesh.MeshError, validate.ConfigError) as e:
            print("REJECTED: cannot spec-check %s: %s" % (a.out_dir, e), file=sys.stderr)
            return 4
        print(json.dumps(rep, indent=2))
        return 0 if rep["ok"] else 4
    if a.cmd == "catalog" and a.check:
        try:
            problems = catalog.check(a.check, openscad=a.openscad)
        except (OSError, ValueError, gen.ToolError, mesh.MeshError, validate.ConfigError) as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 3
        for pr in problems:
            print("STALE: " + pr, file=sys.stderr)
        if problems:
            print("regenerate: python3 -m rackparts catalog --out build/catalog, then copy build/catalog/ to %s"
                  % a.check, file=sys.stderr)
            return 1
        print("catalog is current: %s" % a.check)
        return 0
    if a.cmd == "catalog":
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in a.out):
            print("REFUSED: --out contains a control character (newline, tab, ...)", file=sys.stderr)
            return 2
        try:
            pages = catalog.build(a.out, openscad=a.openscad)
        except FileExistsError:
            print("REFUSED: %s already exists; will not overwrite" % a.out, file=sys.stderr)
            return 3
        except FileNotFoundError as e:
            print("ERROR: %s (parent directory must exist)" % e, file=sys.stderr)
            return 3
        except (gen.ToolError, mesh.MeshError, validate.ConfigError) as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 3
        print(json.dumps(pages, indent=2))
        return 0

    if a.cmd == "generate" and any(ord(ch) < 32 or ord(ch) == 127 for ch in a.out):
        print("REFUSED: --out contains a control character (newline, tab, ...)", file=sys.stderr)
        return 2
    try:
        cfg, geo = validate.validate(validate.load_config(a.config))
    except validate.ConfigError as e:
        return _refused(e)
    except OSError as e:
        print("ERROR: cannot read input: %s" % e, file=sys.stderr)
        return 2
    if a.cmd == "validate":
        print(json.dumps({"ok": True, "derived": geo["derived"], "expected_bbox_mm": geo["expected_bbox"]}, indent=2))
        return 0

    if not 1 <= a.timeout <= 3600:
        print("ERROR: --timeout must be 1-3600", file=sys.stderr)
        return 2
    try:
        man = gen.generate(cfg, geo, a.out, openscad=a.openscad, scad_only=a.scad_only, timeout=a.timeout,
                           png=a.png)
    except FileExistsError:
        print("REFUSED: %s already exists; will not overwrite" % a.out, file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print("ERROR: %s (parent directory must exist)" % e, file=sys.stderr)
        return 3
    except gen.ToolError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 3
    except mesh.MeshError as e:
        print("REJECTED: %s" % e, file=sys.stderr)
        return 4
    summary = {"out": os.path.abspath(a.out), "files": man["files"]}
    if "spec_check" in man:
        summary["spec_check"] = {k: man["spec_check"][k] for k in ("checker", "probes", "failed", "ok")}
    if "mesh" in man:
        m = man["mesh"]
        summary["mesh"] = {k: m[k] for k in ("facets", "bbox_min", "bbox_max", "bbox_size", "volume_mm3",
                                             "shells", "ok")}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
