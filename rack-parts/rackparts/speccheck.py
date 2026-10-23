"""Printability spec check on a rendered STL (stdlib only): wall thickness, overhangs, M6 cage-nut holes.

The checks read the mesh itself, not the input parameters, so they are a second opinion on what the
validator promised:

* Wall thickness: from the centroid of every facet a ray is cast into the material along the inward
  normal; the distance to the next surface is the local wall thickness. The thinnest value must be at
  least ``min_wall_mm`` from the input. This catches thin skins, webs between holes and ribs; it does not
  measure thickness along any other direction.
* Overhangs: for each print orientation (an "up" vector), facets facing down more steeply than
  ``max_overhang_deg`` from vertical and not lying on the bed are grouped into connected regions. A
  boundary edge of a region counts as supported when the neighbouring surface continues downward from it
  (there is material under the edge). Sample points spread over each region are then classified: a point
  is *bridged* if, along one of 8 horizontal lines through it, the region boundary reached first on both
  sides is supported and the two are at most ``max_bridge_mm`` apart; any other point is a *ledge* point:
  its horizontal reach from the nearest supported edge, less what a slope at ``max_overhang_deg`` would
  cover over the height it rises, must be at most ``max_ledge_mm``. A region with no supported edge is an island
  and fails. Sampling is finite (up to 8 x 8 sub-triangles per facet), so a very thin unsupported sliver
  between samples can be missed.
* M6 cage nuts: for parts screwed to the rails through the panel frame, the stated clearance must lie
  between ISO 273 fine and coarse for M6 (6.4-7.0 mm), and rays cast from each rail-hole centre must find
  at least the stated diameter of clear space across the hole. For 19in parts the holes are also compared
  with the EIA-310 nominal pattern (as summarised in the README): any offset must be absorbed by the
  clearance (and slot) around a 6 mm screw.

None of this is a slicer, and passing it does not mean a part prints well or fits a rack.
"""

import json
import math
import os

from . import generate, mesh

MAX_OVERHANG_DEG = 45.0
MAX_BRIDGE_MM = 20.0
MAX_LEDGE_MM = 1.0
EPS = 1e-6
WALL_TOL_MM = 0.005   # float32 STL coordinates and polygon rounding

# ISO 273 clearance holes for M6: fine 6.4, medium 6.6, coarse 7.0 mm.
M6 = {"thread_mm": 6.0, "clearance_min_mm": 6.4, "clearance_max_mm": 7.0}
# EIA-310 nominal values as commonly summarised (see README: the standard itself was not consulted).
EIA_U_MM = 44.45
EIA_HOLES_IN_U_MM = (6.35, 22.225, 38.1)
EIA_RAIL_SPACING_MM = 465.1
PANEL_FRAME_PARTS = ("blanking_panel", "cable_panel", "label_panel", "rack_shelf", "lacing_bar")
AXES = {"+x": [1, 0, 0], "-x": [-1, 0, 0], "+y": [0, 1, 0], "-y": [0, -1, 0], "+z": [0, 0, 1], "-z": [0, 0, -1]}


class SpecError(Exception):
    pass


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 0 else None


def _area(t):
    n = _cross(_sub(t[1], t[0]), _sub(t[2], t[0]))
    return math.sqrt(_dot(n, n)) / 2


class Mesh:
    """Facets with unit normals (from the winding), centroids, and a uniform grid for ray casting."""

    def __init__(self, tris):
        self.tris = []
        self.normals = []
        for t in tris:
            n = _unit(_cross(_sub(t[1], t[0]), _sub(t[2], t[0])))
            if n is not None:  # the mesh check already refuses zero-area facets
                self.tris.append(t)
                self.normals.append(n)
        if not self.tris:
            raise SpecError("mesh has no usable facets")
        self.centroids = [tuple(sum(v[i] for v in t) / 3 for i in range(3)) for t in self.tris]
        self._grids = {}

    def _grid(self, cell):
        g = self._grids.get(cell)
        if g is None:
            g = {}
            for k, t in enumerate(self.tris):
                lo = [int(math.floor(min(v[i] for v in t) / cell)) for i in range(3)]
                hi = [int(math.floor(max(v[i] for v in t) / cell)) for i in range(3)]
                for i in range(lo[0], hi[0] + 1):
                    for j in range(lo[1], hi[1] + 1):
                        for m in range(lo[2], hi[2] + 1):
                            g.setdefault((i, j, m), []).append(k)
            self._grids[cell] = g
        return g

    def first_hit(self, origin, d, max_t, skip=-1):
        """Distance along unit direction d to the nearest facet within max_t (None if none)."""
        cell = max_t
        g = self._grid(cell)
        end = (origin[0] + d[0] * max_t, origin[1] + d[1] * max_t, origin[2] + d[2] * max_t)
        lo = [int(math.floor(min(origin[i], end[i]) / cell)) for i in range(3)]
        hi = [int(math.floor(max(origin[i], end[i]) / cell)) for i in range(3)]
        seen = set()
        best = None
        for i in range(lo[0], hi[0] + 1):
            for j in range(lo[1], hi[1] + 1):
                for m in range(lo[2], hi[2] + 1):
                    for k in g.get((i, j, m), ()):
                        if k == skip or k in seen:
                            continue
                        seen.add(k)
                        t = _ray_tri(origin, d, self.tris[k])
                        if t is not None and t <= max_t and (best is None or t < best):
                            best = t
        return best


def _ray_tri(o, d, tri):
    """Moller-Trumbore; edges inclusive so a ray through a shared edge still hits."""
    a, b, c = tri
    e1, e2 = _sub(b, a), _sub(c, a)
    p = _cross(d, e2)
    det = _dot(e1, p)
    if abs(det) < 1e-12:
        return None
    inv = 1.0 / det
    s = _sub(o, a)
    u = _dot(s, p) * inv
    if u < -1e-9 or u > 1 + 1e-9:
        return None
    q = _cross(s, e1)
    v = _dot(d, q) * inv
    if v < -1e-9 or u + v > 1 + 1e-9:
        return None
    t = _dot(e2, q) * inv
    return t if t > EPS else None


def wall_thickness(m, min_wall):
    cap = max(2 * min_wall, 5.0)
    worst = (cap, None)
    thin = 0
    for k, (c, n) in enumerate(zip(m.centroids, m.normals)):
        t = m.first_hit(c, (-n[0], -n[1], -n[2]), cap, skip=k)
        t = cap if t is None else t
        if t < min_wall - WALL_TOL_MM:
            thin += 1
        if t < worst[0]:
            worst = (t, k)
    t, k = worst
    return {
        "min_wall_mm": min_wall,
        "thinnest_mm": round(t, 4),
        "thinnest_at_mm": [round(x, 3) for x in m.centroids[k]] if k is not None else None,
        "search_depth_mm": cap,
        "facets_below_min_wall": thin,
        "ok": thin == 0,
    }


def _ray_seg_2d(o, d, a, b):
    """Distance along 2D ray o + s*d to segment a-b, or None."""
    e = (b[0] - a[0], b[1] - a[1])
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-12:
        return None
    w = (a[0] - o[0], a[1] - o[1])
    s = (w[0] * e[1] - w[1] * e[0]) / den
    r = (w[0] * d[1] - w[1] * d[0]) / den
    return s if s > 1e-9 and -1e-9 <= r <= 1 + 1e-9 else None


def _ledge(p, a, b, h, flat, tan_lim):
    """Horizontal reach from the supported edge a-b to p beyond what a slope at the overhang limit covers."""
    q, a2, b2 = flat(p), flat(a), flat(b)
    e = (b2[0] - a2[0], b2[1] - a2[1])
    L = e[0] * e[0] + e[1] * e[1]
    s = 0.0 if L == 0 else max(0.0, min(1.0, ((q[0] - a2[0]) * e[0] + (q[1] - a2[1]) * e[1]) / L))
    reach = math.hypot(q[0] - a2[0] - s * e[0], q[1] - a2[1] - s * e[1])
    rise = h(p) - (h(a) + s * (h(b) - h(a)))
    return max(0.0, reach - max(0.0, rise) * tan_lim)


def _samples(t, step=2.0):
    """Interior sample points of a facet: centroids of an n x n subdivision, n from its longest edge."""
    n = max(1, min(8, math.ceil(max(math.dist(t[0], t[1]), math.dist(t[1], t[2]), math.dist(t[2], t[0])) / step)))
    pts = []
    for i in range(n):
        for j in range(n - i):
            for a, b in (((i + 1 / 3) / n, (j + 1 / 3) / n), ((i + 2 / 3) / n, (j + 2 / 3) / n)):
                if a + b < 1:
                    pts.append(tuple(t[0][k] + a * (t[1][k] - t[0][k]) + b * (t[2][k] - t[0][k]) for k in range(3)))
    return pts


BRIDGE_DIRECTIONS = 8   # lines through each sample point, 22.5 degrees apart


def overhangs(m, up, max_overhang_deg=MAX_OVERHANG_DEG, max_bridge_mm=MAX_BRIDGE_MM, max_ledge_mm=MAX_LEDGE_MM):
    up = _unit(tuple(float(x) for x in up))
    sin_lim = math.sin(math.radians(max_overhang_deg))
    tan_lim = math.tan(math.radians(max_overhang_deg))
    h = lambda v: _dot(v, up)  # noqa: E731
    e1 = _unit(_cross(up, (1.0, 0.0, 0.0) if abs(up[0]) < 0.9 else (0.0, 1.0, 0.0)))
    e2 = _cross(up, e1)
    flat = lambda v: (_dot(v, e1), _dot(v, e2))  # noqa: E731  (projection onto the bed plane)
    bed = min(h(v) for t in m.tris for v in t)
    on_bed = lambda k: all(h(v) <= bed + 1e-4 for v in m.tris[k])  # noqa: E731
    down = {k for k, n in enumerate(m.normals) if -_dot(n, up) > sin_lim + 1e-9 and not on_bed(k)}

    edge_faces = {}
    for k, t in enumerate(m.tris):
        for i in range(3):
            edge_faces.setdefault(frozenset((t[i], t[(i + 1) % 3])), []).append(k)

    regions = []
    todo = set(down)
    while todo:
        stack = [todo.pop()]
        faces = []
        while stack:
            k = stack.pop()
            faces.append(k)
            t = m.tris[k]
            for i in range(3):
                for o in edge_faces[frozenset((t[i], t[(i + 1) % 3]))]:
                    if o in todo:
                        todo.remove(o)
                        stack.append(o)
        regions.append(faces)

    dirs = [(math.cos(math.pi * k / BRIDGE_DIRECTIONS), math.sin(math.pi * k / BRIDGE_DIRECTIONS))
            for k in range(BRIDGE_DIRECTIONS)]
    out = []
    for faces in regions:
        fs = set(faces)
        boundary = []   # (a, b, supported)
        for k in faces:
            t = m.tris[k]
            for i in range(3):
                a, b = t[i], t[(i + 1) % 3]
                for o in edge_faces[frozenset((a, b))]:
                    if o in fs:
                        continue
                    opp = [v for v in m.tris[o] if v != a and v != b]
                    # material under the edge: the neighbouring surface runs down from it, or it is the bed
                    boundary.append((a, b, on_bed(o) or bool(opp and h(opp[0]) < min(h(a), h(b)) - 1e-6)))
        supported = [(a, b) for a, b, ok in boundary if ok]
        edges2 = [(flat(a), flat(b), ok) for a, b, ok in boundary]
        worst_ledge = 0.0      # largest distance to a supported edge over points that are not bridged
        worst_at = None
        longest_bridge = 0.0
        for k in faces:
            for p in _samples(m.tris[k]):
                q = flat(p)
                best = None
                for d in dirs:
                    span = 0.0
                    for sgn in (1, -1):
                        dd = (sgn * d[0], sgn * d[1])
                        hit = None
                        for a, b, ok in edges2:
                            s = _ray_seg_2d(q, dd, a, b)
                            if s is not None and (hit is None or s < hit[0]):
                                hit = (s, ok)
                        if hit is None or not hit[1]:
                            span = None
                            break
                        span += hit[0]
                    if span is not None and (best is None or span < best):
                        best = span
                if best is not None and best <= max_bridge_mm + 1e-6:
                    longest_bridge = max(longest_bridge, best)
                    continue
                ledge = min((_ledge(p, a, b, h, flat, tan_lim) for a, b in supported), default=math.inf)
                if ledge > worst_ledge:
                    worst_ledge, worst_at = ledge, p
        lo = [min(v[i] for k in faces for v in m.tris[k]) for i in range(3)]
        hi = [max(v[i] for k in faces for v in m.tris[k]) for i in range(3)]
        out.append({
            "facets": len(faces),
            "area_mm2": round(sum(_area(m.tris[k]) for k in faces), 2),
            "bbox_min": [round(x, 3) for x in lo],
            "bbox_max": [round(x, 3) for x in hi],
            "longest_bridge_mm": round(longest_bridge, 3),
            "unsupported_ledge_mm": None if math.isinf(worst_ledge) else round(worst_ledge, 3),
            "worst_point_mm": [round(x, 3) for x in worst_at] if worst_at else None,
            "island": not supported,
            "ok": worst_ledge <= max_ledge_mm + 1e-6,
        })
    out.sort(key=lambda r: -(math.inf if r["unsupported_ledge_mm"] is None else r["unsupported_ledge_mm"]))
    bad = [r for r in out if not r["ok"]]
    return {
        "up": list(up),
        "max_overhang_deg": max_overhang_deg,
        "max_bridge_mm": max_bridge_mm,
        "max_ledge_mm": max_ledge_mm,
        "overhang_regions": len(out),
        "overhang_area_mm2": round(sum(r["area_mm2"] for r in out), 2),
        "longest_bridge_mm": max((r["longest_bridge_mm"] for r in out), default=0.0),
        "worst_unsupported_ledge_mm": out[0]["unsupported_ledge_mm"] if out else 0.0,  # None: an island
        "failing_regions": bad[:5],
        "failing_region_count": len(bad),
        "ok": not bad,
    }


def _hole_clear(m, x, y, z, max_r, steps=72):
    """Clear radius from (x, y, z) in each direction of the xz plane (the rail-hole plane)."""
    r = []
    for k in range(steps):
        a = 2 * math.pi * k / steps
        t = m.first_hit((x, y, z), (math.cos(a), 0.0, math.sin(a)), max_r)
        r.append(max_r if t is None else t)
    half = steps // 2
    across = [r[k] + r[k + half] for k in range(half)]
    return {"min_across_mm": round(min(across), 4), "horizontal_mm": round(across[0], 4),
            "vertical_mm": round(across[steps // 4], 4)}


def m6_cage_nut(m, part, cfg):
    if part not in PANEL_FRAME_PARTS:
        return {"applies": False, "reason": "rail holes are not in the panel frame for %s; not checked" % part,
                "ok": True}
    d = cfg["rail_screw_clearance_diameter_mm"]
    slot = cfg["rail_slot_extra_length_mm"]
    t = cfg["panel_thickness_mm"]
    S = cfg["rail_hole_center_spacing_mm"]
    problems = []
    if not M6["clearance_min_mm"] <= d <= M6["clearance_max_mm"]:
        problems.append("rail_screw_clearance_diameter_mm %g is outside the M6 clearance range %g-%g mm "
                        "(ISO 273 fine..coarse)" % (d, M6["clearance_min_mm"], M6["clearance_max_mm"]))
    holes = []
    for sx in (-S / 2, S / 2):
        for z in cfg["rail_hole_z_offsets_mm"]:
            c = _hole_clear(m, sx, t / 2, z, d + slot + 10.0)
            c["at_mm"] = [sx, z]
            holes.append(c)
            if c["min_across_mm"] < d - 0.01:
                problems.append("hole at x=%g z=%g measures %.3f mm across; the input states %g"
                                % (sx, z, c["min_across_mm"], d))
            if c["min_across_mm"] < M6["clearance_min_mm"] - 0.01:
                problems.append("hole at x=%g z=%g measures %.3f mm across; an M6 screw needs >= %g"
                                % (sx, z, c["min_across_mm"], M6["clearance_min_mm"]))
    rep = {"applies": True, "fastener": "M6 screw into a cage nut", "clearance_range_mm":
           [M6["clearance_min_mm"], M6["clearance_max_mm"]], "stated_clearance_mm": d, "holes": holes}
    if cfg["rack_width_class"] == "19in":
        play = (d - M6["thread_mm"]) / 2
        H = cfg["panel_height_mm"]
        units = max(1, math.ceil(H / EIA_U_MM - 1e-9))
        base = (units * EIA_U_MM - H) / 2      # panel assumed centred in the units it occupies
        pattern = [u * EIA_U_MM + h for u in range(units) for h in EIA_HOLES_IN_U_MM]
        vert = [min(abs(z + base - q) for q in pattern) for z in cfg["rail_hole_z_offsets_mm"]]
        horiz = abs(S - EIA_RAIL_SPACING_MM) / 2
        rep["eia310_nominal"] = {
            "assumed_gap_below_panel_mm": round(base, 4),
            "vertical_offsets_mm": [round(v, 4) for v in vert],
            "horizontal_offset_mm": round(horiz, 4),
            "vertical_play_mm": round(play, 4),
            "horizontal_play_mm": round(play + slot / 2, 4),
        }
        for z, v in zip(cfg["rail_hole_z_offsets_mm"], vert):
            if v > play + 1e-6:
                problems.append("rail hole at z=%g is %.3f mm off the nearest EIA-310 nominal hole; the %.3f mm "
                                "clearance around a 6 mm screw only absorbs %.3f" % (z, v, d, play))
        if horiz > play + slot / 2 + 1e-6:
            problems.append("rail_hole_center_spacing_mm %g is %.3f mm per side off the EIA-310 nominal %g; "
                            "clearance and slot absorb %.3f" % (S, horiz, EIA_RAIL_SPACING_MM, play + slot / 2))
    else:
        rep["eia310_nominal"] = None
        rep["note"] = "10in: no hole-pattern standard to compare against; only the hole itself is checked"
    rep["problems"] = problems
    rep["ok"] = not problems
    return rep


def check(tris, manifest, max_overhang_deg=MAX_OVERHANG_DEG, max_bridge_mm=MAX_BRIDGE_MM, max_ledge_mm=MAX_LEDGE_MM,
          fastener="m6"):
    cfg = manifest["input"]["parameters"]
    m = Mesh(tris)
    orients = manifest.get("print_orientations") or AXES
    ors = {name: overhangs(m, up, max_overhang_deg, max_bridge_mm, max_ledge_mm) for name, up in orients.items()}
    rep = {
        "part": cfg["part"],
        "name": cfg["name"],
        "wall_thickness": wall_thickness(m, cfg["min_wall_mm"]),
        "overhangs": {
            "orientations_from": "manifest" if manifest.get("print_orientations") else "all six axes",
            "passing": sorted(k for k, v in ors.items() if v["ok"]),
            "by_orientation": ors,
        },
        "m6_cage_nut": (m6_cage_nut(m, cfg["part"], cfg) if fastener == "m6"
                        else {"applies": False, "reason": "--fastener none", "ok": True}),
    }
    rep["overhangs"]["ok"] = bool(rep["overhangs"]["passing"])
    rep["ok"] = rep["wall_thickness"]["ok"] and rep["overhangs"]["ok"] and rep["m6_cage_nut"]["ok"]
    rep["notice"] = ("Heuristic geometry checks on the STL; not a slicer, not a print test, no strength or fit "
                     "rating.")
    return rep


def check_dir(out_dir, **kw):
    """Spec-check a directory written by ``generate``: its manifest and the STL it names (hash-verified)."""
    try:
        with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        stl = manifest["files"]["stl"]
        cfg = manifest["input"]["parameters"]
        missing = [k for k in ("part", "name", "min_wall_mm") if k not in cfg]
        if missing:
            raise KeyError(", ".join(missing))
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SpecError("%s is not a rendered rackparts output (manifest with an STL): %s" % (out_dir, e))
    path = os.path.join(out_dir, os.path.basename(stl["path"]))
    if generate.sha256_file(path) != stl["sha256"]:
        raise SpecError("%s does not match the sha256 recorded in manifest.json" % path)
    return check(mesh.read_stl(path), manifest, **kw)
