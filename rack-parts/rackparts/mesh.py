"""STL reading and topology checks (stdlib only), plus an optional admesh run.

Checks: non-empty, no zero-area facets, every edge shared by exactly two
facets with opposite winding (closed, consistently oriented 2-manifold),
positive enclosed volume, and the number of connected shells. A full
triangle-triangle self-intersection test is NOT performed; CGAL-based CSG
output is not expected to self-intersect, and admesh is run as a second
opinion when available.
"""

import hashlib
import math
import re
import shutil
import struct
import subprocess

MAX_FACETS = 2_000_000


class MeshError(Exception):
    pass


def read_stl(path):
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if n <= MAX_FACETS and len(data) == 84 + 50 * n:
            tris = []
            for i in range(n):
                v = struct.unpack_from("<12f", data, 84 + 50 * i)
                tris.append((v[3:6], v[6:9], v[9:12]))
            return tris
    text = data.decode("ascii", errors="replace")
    if not text.lstrip().startswith("solid"):
        raise MeshError("not a recognisable STL file")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if len(nums) % 3:
        raise MeshError("ASCII STL vertex count is not a multiple of 3")
    if len(nums) // 3 > MAX_FACETS:
        raise MeshError("too many facets")
    pts = [tuple(float(c) for c in v) for v in nums]
    return [tuple(pts[i:i + 3]) for i in range(0, len(pts), 3)]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def geometry_hash(tris):
    """sha256 over the facet set, independent of facet order (winding kept)."""
    canon = []
    for t in tris:
        k = t.index(min(t))
        canon.append(t[k:] + t[:k])
    h = hashlib.sha256()
    for t in sorted(canon):
        h.update(struct.pack("<9f", *t[0], *t[1], *t[2]))
    return h.hexdigest()


def analyse(tris):
    rep = {"facets": len(tris)}
    if not tris:
        raise MeshError("empty mesh: no facets")
    rep["geometry_sha256"] = geometry_hash(tris)
    index = {}
    faces = []
    for t in tris:
        faces.append(tuple(index.setdefault(v, len(index)) for v in t))
    verts = list(index)
    if not all(math.isfinite(c) for v in verts for c in v):
        raise MeshError("non-finite vertex coordinate")
    rep["vertices"] = len(verts)

    lo = [min(v[i] for v in verts) for i in range(3)]
    hi = [max(v[i] for v in verts) for i in range(3)]
    rep["bbox_min"] = [round(x, 4) for x in lo]
    rep["bbox_max"] = [round(x, 4) for x in hi]
    rep["bbox_size"] = [round(hi[i] - lo[i], 4) for i in range(3)]

    degenerate = 0
    vol6 = 0.0
    directed = {}
    for a, b, c in faces:
        if a == b or b == c or a == c:
            degenerate += 1
            continue
        pa, pb, pc = verts[a], verts[b], verts[c]
        n = _cross(_sub(pb, pa), _sub(pc, pa))
        if n[0] * n[0] + n[1] * n[1] + n[2] * n[2] == 0.0:
            degenerate += 1
        vol6 += (pa[0] * (pb[1] * pc[2] - pb[2] * pc[1])
                 - pa[1] * (pb[0] * pc[2] - pb[2] * pc[0])
                 + pa[2] * (pb[0] * pc[1] - pb[1] * pc[0]))
        for e in ((a, b), (b, c), (c, a)):
            directed[e] = directed.get(e, 0) + 1
    rep["degenerate_facets"] = degenerate
    dup_directed = sum(1 for k in directed.values() if k != 1)
    unmatched = sum(1 for (a, b) in directed if (b, a) not in directed)
    rep["edges_with_bad_winding_or_multiplicity"] = dup_directed
    rep["open_edges"] = unmatched
    rep["volume_mm3"] = round(vol6 / 6.0, 3)

    # connected shells via union-find over shared vertices
    parent = list(range(len(verts)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for a, b, c in faces:
        for x, y in ((a, b), (b, c)):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry
    rep["shells"] = len({find(i) for f in faces for i in f})

    problems = []
    if degenerate:
        problems.append("%d degenerate facets" % degenerate)
    if dup_directed or unmatched:
        problems.append("not a closed, consistently oriented manifold (%d open edges, %d bad edges)"
                        % (unmatched, dup_directed))
    if rep["volume_mm3"] <= 0:
        problems.append("non-positive volume %.3f (inverted or empty)" % rep["volume_mm3"])
    rep["problems"] = problems
    rep["ok"] = not problems
    return rep


ADMESH_KEYS = {
    "Number of parts": "parts",
    "Degenerate facets": "degenerate_facets",
    "Edges fixed": "edges_fixed",
    "Facets removed": "facets_removed",
    "Facets added": "facets_added",
    "Facets reversed": "facets_reversed",
    "Backwards edges": "backwards_edges",
    "Normals fixed": "normals_fixed",
    "Total disconnected facets": "disconnected_facets",
    "Volume": "volume_mm3",
}


def run_admesh(path, admesh=None, timeout=120):
    exe = admesh or shutil.which("admesh")
    if not exe:
        return None
    # Read-only check: no fix/write options are passed.
    r = subprocess.run([exe, path], capture_output=True, text=True, timeout=timeout)
    out = {"exit_code": r.returncode}
    for line in r.stdout.splitlines():
        # admesh prints "Key : value" pairs, sometimes two per line; for the
        # facet-status table the first number is the original-file value.
        for m in re.finditer(r"([A-Za-z][A-Za-z ]*?)\s*:\s*(-?\d+(?:\.\d+)?)", line):
            key = ADMESH_KEYS.get(m.group(1).strip())
            if key and key not in out:
                out[key] = float(m.group(2)) if key == "volume_mm3" else int(float(m.group(2)))
    repairs = [k for k in ("degenerate_facets", "edges_fixed", "facets_removed", "facets_added",
                           "facets_reversed", "backwards_edges", "disconnected_facets") if out.get(k)]
    out["problems"] = (["admesh reported repairs needed: %s" % ", ".join(repairs)] if repairs else [])
    if r.returncode != 0 or "parts" not in out:
        out["problems"].append("admesh failed or output not understood")
    out["ok"] = not out["problems"]
    return out
