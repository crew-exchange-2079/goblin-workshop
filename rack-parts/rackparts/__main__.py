"""Command line: python3 -m rackparts {schema,validate,generate,check-stl} ...

Exit codes: 0 ok, 2 input refused, 3 tool failure / output exists, 4 mesh rejected.
"""

import argparse
import json
import os
import sys

from . import generate as gen
from . import mesh, spec, validate


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
    c = sub.add_parser("check-stl", help="run the mesh checks on an existing STL")
    c.add_argument("stl")
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
        man = gen.generate(cfg, geo, a.out, openscad=a.openscad, scad_only=a.scad_only, timeout=a.timeout)
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
    if "mesh" in man:
        m = man["mesh"]
        summary["mesh"] = {k: m[k] for k in ("facets", "bbox_min", "bbox_max", "bbox_size", "volume_mm3",
                                             "shells", "ok")}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
