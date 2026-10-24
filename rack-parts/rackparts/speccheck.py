"""Per-part spec checks on the rendered mesh (stdlib only).

The validator proves the *numbers* are consistent; this module checks that the *rendered STL* actually has
the features those numbers describe. Each part has a checker that turns its validated geometry into probes:
points that must be inside material (a stop tab, a latch ledge, a divider) or outside it (a hole, a slot, the
space a device, cable, jack or card occupies). A probe is answered by ray casting against the mesh, so a SCAD
module that silently drops a cut or a feature fails here even when the mesh is closed and its bounding box is
right.

Probes are placed a small distance (PROBE_INSET_MM) inside the feature they test, never on a face, and
each point is decided by three axis-aligned rays (majority vote), so a ray grazing an edge cannot flip the
answer. A pass means "the features exist where specified", not that the part fits or is strong.
"""

import math

from . import validate

PROBE_INSET_MM = 0.3
GRID = 48
# Irrational nudges keep probe rays off the many axis-aligned vertices and edges of CSG output.
_NUDGE = (math.sqrt(2) * 1e-4, math.sqrt(3) * 1e-4, math.sqrt(5) * 1e-4)


class Solid:
    """Point-in-mesh queries on a closed, consistently oriented triangle mesh."""

    def __init__(self, tris):
        self.tris = tris
        self.lo = [min(v[i] for t in tris for v in t) for i in range(3)]
        self.hi = [max(v[i] for t in tris for v in t) for i in range(3)]
        self.grids = [self._grid(axis) for axis in range(3)]

    def _plane(self, axis):
        return [i for i in range(3) if i != axis]

    def _cell(self, axis, k, value):
        lo, hi = self.lo[k], self.hi[k]
        if hi - lo <= 0:
            return 0
        return min(GRID - 1, max(0, int((value - lo) / (hi - lo) * GRID)))

    def _grid(self, axis):
        a, b = self._plane(axis)
        cells = {}
        for t in self.tris:
            ia0 = self._cell(axis, a, min(v[a] for v in t))
            ia1 = self._cell(axis, a, max(v[a] for v in t))
            ib0 = self._cell(axis, b, min(v[b] for v in t))
            ib1 = self._cell(axis, b, max(v[b] for v in t))
            for i in range(ia0, ia1 + 1):
                for j in range(ib0, ib1 + 1):
                    cells.setdefault((i, j), []).append(t)
        return cells

    def _crossings(self, p, axis):
        """Facets crossed by the ray from p toward +axis."""
        a, b = self._plane(axis)
        pa, pb = p[a], p[b]
        if not (self.lo[a] <= pa <= self.hi[a] and self.lo[b] <= pb <= self.hi[b]):
            return 0
        n = 0
        for t in self.grids[axis].get((self._cell(axis, a, pa), self._cell(axis, b, pb)), ()):
            (a0, b0, c0), (a1, b1, c1), (a2, b2, c2) = ((v[a], v[b], v[axis]) for v in t)
            d = (a1 - a0) * (b2 - b0) - (a2 - a0) * (b1 - b0)
            if d == 0:
                continue   # facet is edge-on to the ray
            l1 = ((pa - a0) * (b2 - b0) - (a2 - a0) * (pb - b0)) / d
            l2 = ((a1 - a0) * (pb - b0) - (pa - a0) * (b1 - b0)) / d
            if l1 < 0 or l2 < 0 or l1 + l2 > 1:
                continue
            if c0 + l1 * (c1 - c0) + l2 * (c2 - c0) > p[axis]:
                n += 1
        return n

    def inside(self, p):
        q = (p[0] + _NUDGE[0], p[1] + _NUDGE[1], p[2] + _NUDGE[2])
        votes = sum(self._crossings(q, axis) % 2 for axis in range(3))
        return votes >= 2


def _probe(label, point, solid):
    return {"label": label, "point_mm": [round(c, 4) for c in point], "expect": "material" if solid else "empty"}


E = PROBE_INSET_MM


def _rail_holes(a, y_mid, probes, x_key="hole_x", zs_key="hole_zs"):
    for sx in (-a[x_key], a[x_key]):
        for z in a[zs_key]:
            probes.append(_probe("rail hole at x=%g z=%g is open" % (sx, z), (sx, y_mid, z), False))
            probes.append(_probe("panel material beside rail hole x=%g z=%g" % (sx, z),
                                 (sx - math.copysign(a["slot_len"] / 2 + a["rail_d"] / 2 + E, sx), y_mid, z), True))


def check_bracket(c, a):
    p = []
    t, h = a["t"], a["h"]
    sgn = -1 if a["left"] else 1
    p.append(_probe("front flange", (sgn * (a["ear_w"] - E), t / 2, h - E), True))
    p.append(_probe("side flange at its rear end", (sgn * t / 2, a["side_depth"] - E, h / 2), True))
    p.append(_probe("open space behind the front flange, beside the side flange",
                    (sgn * (a["ear_w"] - E), min(a["side_depth"] - E, t + 5.0), h / 2), False))
    if a["gusset"] > 0:
        g = a["gusset"]
        p.append(_probe("gusset", (sgn * (t + g / 4), t + g / 4, h / 2), True))
    for z in a["rail_hole_zs"]:
        p.append(_probe("rail hole z=%g is open" % z, (sgn * a["rail_hole_x"], t / 2, z), False))
    for y, z in a["dev_holes"]:
        p.append(_probe("device screw hole y=%g z=%g is open" % (y, z), (sgn * t / 2, y, z), False))
    return p


def check_cradle(c, a):
    p = []
    iw, L, tb, tw = a["inner_w"], a["length"], a["t_base"], a["t_wall"]
    p.append(_probe("base", (0.0 if not a["holes"] else iw / 2 - E, L / 2, tb / 2), True))
    for s in (-1, 1):
        p.append(_probe("side wall", (s * (iw / 2 + tw / 2), L / 2, tb + a["clear_h"] / 2), True))
        if a["lip"] > 0:
            p.append(_probe("retention lip", (s * (iw / 2 - a["lip"] / 2), L / 2, tb + a["clear_h"] + tw / 2), True))
    p.append(_probe("device space above the base is empty", (0.0, L / 2, tb + a["clear_h"] / 2), False))
    p.append(_probe("device space next to the wall is empty", (iw / 2 - E, L / 2, tb + a["clear_h"] - E), False))
    for x, y in a["holes"]:
        p.append(_probe("shelf screw hole x=%g y=%g is open" % (x, y), (x, y, E), False))
        if a["cb_depth"] > 0:
            p.append(_probe("counterbore x=%g y=%g" % (x, y), (x + (a["cb_d"] / 2 + a["hole_d"] / 2) / 2, y,
                                                               tb - a["cb_depth"] / 2), False))
            p.append(_probe("base under counterbore x=%g y=%g" % (x, y),
                            (x + (a["cb_d"] / 2 + a["hole_d"] / 2) / 2, y, (tb - a["cb_depth"]) / 2), True))
    return p


def check_switch(c, a):
    p = []
    iw, L, tb, tw = a["inner_w"], a["length"], a["t_base"], a["t_wall"]
    tab_w, tab_h = a["tab_w"], a["tab_h"]
    for s in (-1, 1):
        for y in (tw / 2, L - tw / 2):
            p.append(_probe("stop tab", (s * (iw / 2 - tab_w + E), y, tb + tab_h - E), True))
        for y0, y1 in a["slots"]:
            p.append(_probe("strap slot", (s * (iw / 2 + tw / 2), (y0 + y1) / 2, a["slot_z0"] + a["slot_h"] / 2), False))
            p.append(_probe("strap bar above slot", (s * (iw / 2 + tw / 2), (y0 + y1) / 2, a["wall_h"] - E), True))
        for y0, y1, z0, z1 in a["windows"]:
            p.append(_probe("side vent window", (s * (iw / 2 + tw / 2), (y0 + y1) / 2, (z0 + z1) / 2), False))
    p.append(_probe("switch space between the stop tabs is empty", (0.0, tw / 2, tb + tab_h / 2), False))
    p.append(_probe("switch space under the strap is empty", (iw / 2 - E, L / 2, a["slot_z0"] - E), False))
    for vx, vy0, vy1 in a["vents"]:
        p.append(_probe("base vent slot x=%g" % vx, (vx, (vy0 + vy1) / 2, tb / 2), False))
    for x, y in a["holes"]:
        p.append(_probe("shelf screw hole x=%g y=%g is open" % (x, y), (x, y, E), False))
    return p


def check_panel(c, a):
    p = []
    w, h, t = a["w"], a["h"], a["t"]
    p.append(_probe("panel face near the side edge", (w / 2 - E, t / 2, h / 2), True))
    _rail_holes(a, t / 2, p)
    if a["rib_depth"] > 0:
        for z in (a["rib_t"] / 2, h - a["rib_t"] / 2):
            p.append(_probe("stiffening rib", (a["rib_half"] - E, t + a["rib_depth"] - E, z), True))
        p.append(_probe("open space between the ribs", (0.0, t + a["rib_depth"] / 2, h / 2), False))
        p.append(_probe("no rib outside the rail opening", (a["rib_half"] + E, t + a["rib_depth"] / 2,
                                                            a["rib_t"] / 2), False))
    if a["cuts"]:
        r = a["cut_h"] / 2
        half = max(a["cut_len"] - a["cut_h"], 0.0) / 2
        f = a["fillet"]
        for x, z in a["cuts"]:
            p.append(_probe("pass-through neck x=%g is open" % x, (x, t / 2, z + r - E), False))
            p.append(_probe("pass-through end x=%g is open" % x, (x + half + r - E, t / 2, z), False))
            p.append(_probe("material above the pass-through neck x=%g" % x, (x, t / 2, z + r + E), True))
            # rounded face edge: just above the neck at the face the cutter is flared open
            p.append(_probe("rounded front edge x=%g" % x, (x, E / 2, z + r + E), False))
            p.append(_probe("rounded back edge x=%g" % x, (x, t - E / 2, z + r + E), False))
            p.append(_probe("material outside the flare x=%g" % x, (x, E / 2, z + r + f + E), True))
    return p


def check_shelf(c, a):
    p = []
    w, h, t, half, D = a["w"], a["h"], a["t"], a["half"], a["depth"]
    tf, tw = a["t_floor"], a["t_wall"]
    _rail_holes(a, t / 2, p)
    p.append(_probe("front plate", (w / 2 - E, t / 2, h / 2), True))
    p.append(_probe("floor at the rear corner", (half - E, t + D - E, tf / 2), True))
    for s in (-1, 1):
        p.append(_probe("side wall", (s * (half - tw / 2), t + D / 2, tf + a["wall_h"] - E), True))
    if a["lip_h"] > 0:
        p.append(_probe("rear lip", (0.0, t + D - tw / 2, tf + a["lip_h"] - E), True))
    p.append(_probe("device space above the floor is empty", (0.0, t + (D - (tw if a["lip_h"] > 0 else 0)) / 2,
                                                              tf + E), False))
    p.append(_probe("nothing above the walls", (half - tw / 2, t + D / 2, tf + a["wall_h"] + E), False))
    for vx, vy0, vy1 in a["vents"]:
        p.append(_probe("floor vent slot x=%g" % vx, (vx, (vy0 + vy1) / 2, tf / 2), False))
    return p


def check_pdu(c, a):
    p = []
    L, iw, tb, tw, ear = a["length"], a["inner_w"], a["t_base"], a["t_wall"], a["ear"]
    p.append(_probe("base under the strip", (L / 2 - E, 0.0, tb / 2), True))
    p.append(_probe("strip channel is empty", (0.0, 0.0, tb + E), False))
    p.append(_probe("strip channel is empty at the slot height", (0.0, iw / 2 - E, a["slot_z0"] + a["slot_h"] / 2),
                    False))
    for s in (-1, 1):
        y_wall = s * (iw / 2 + tw / 2)
        p.append(_probe("strap slot", (0.0, y_wall, a["slot_z0"] + a["slot_h"] / 2), False))
        p.append(_probe("wall below the slot", (0.0, y_wall, a["slot_z0"] - E), True))
        p.append(_probe("strap bar", (0.0, y_wall, a["wall_h"] - E), True))
        p.append(_probe("mount hole", (0.0, s * a["hole_y"], tb / 2), False))
        p.append(_probe("ear", (L / 2 - E, s * (iw / 2 + tw + ear - E), tb / 2), True))
    return p


def check_keystone(c, a):
    p = []
    w, h, t, seat = a["w"], a["h"], a["t"], a["seat_t"]
    cw, ch, pw, ph = a["cut_w"], a["cut_h"], a["pocket_w"], a["pocket_h"]
    _rail_holes(a, t / 2, p)
    p.append(_probe("panel face near the side edge", (w / 2 - E, t / 2, h / 2), True))
    for z, x in ((z, x) for z in a["rows"] for x in a["jack_x"]):
        p.append(_probe("keystone cutout x=%g is open through the face" % x, (x, E, z), False))
        p.append(_probe("keystone cutout corner x=%g" % x, (x + cw / 2 - E, seat / 2, z + ch / 2 - E), False))
        p.append(_probe("latch ledge above the cutout x=%g" % x, (x, seat / 2, z + ch / 2 + E), True))
        p.append(_probe("latch ledge below the cutout x=%g" % x, (x, seat / 2, z - ch / 2 - E), True))
        p.append(_probe("face beside the cutout x=%g" % x, (x + cw / 2 + E, seat / 2, z), True))
        if seat < t:
            p.append(_probe("jack body pocket behind the ledge x=%g" % x, (x, (seat + t) / 2, z + ch / 2 + E), False))
            # mid-depth of the pocket: it can be much shallower than the probe inset (seat close to t)
            p.append(_probe("jack body pocket corner x=%g" % x,
                            (x + pw / 2 - E, (seat + t) / 2, z + ph / 2 - E), False))
            p.append(_probe("full-thickness panel outside the pocket x=%g" % x,
                            (x, (seat + t) / 2, z + ph / 2 + E), True))
    if len(a["jack_x"]) > 1:
        xm = (a["jack_x"][0] + a["jack_x"][1]) / 2
        p.append(_probe("web between neighbouring jacks", (xm, t / 2, a["rows"][0]), True))
    if a["rib_depth"] > 0:
        for zr in (a["rib_t"] / 2, h - a["rib_t"] / 2):
            p.append(_probe("stiffening rib", (a["rib_half"] - E, t + a["rib_depth"] - E, zr), True))
    return p


def check_drawer_housing(c, a):
    p = []
    w, h, t, half, D = a["w"], a["h"], a["t"], a["half"], a["depth"]
    tf, tw, troof, tback = a["t_floor"], a["t_wall"], a["t_roof"], a["t_back"]
    sh = a["sleeve_h"]
    _rail_holes(a, t / 2, p)
    p.append(_probe("front plate", (w / 2 - E, t / 2, h / 2), True))
    p.append(_probe("drawer opening through the front plate", (0.0, t / 2, tf + E), False))
    p.append(_probe("drawer opening corner", (half - tw - E, t / 2, sh - troof - E), False))
    p.append(_probe("sleeve floor", (0.0, t + D / 2, tf / 2), True))
    p.append(_probe("sleeve roof", (0.0, t + D / 2, sh - troof / 2), True))
    for s in (-1, 1):
        p.append(_probe("sleeve wall", (s * (half - tw / 2), t + D / 2, sh / 2), True))
    p.append(_probe("back stop wall", (0.0, t + D - tback / 2, sh / 2), True))
    p.append(_probe("drawer space inside the sleeve is empty", (0.0, t + D - tback - E, sh / 2), False))
    p.append(_probe("drawer space inside the sleeve corner", (half - tw - E, t + E, sh - troof - E), False))
    return p


def check_drawer_box(c, a):
    p = []
    bw, bl, bh = a["box_w"], a["box_l"], a["box_h"]
    tf, tw, tfr = a["t_floor"], a["t_wall"], a["t_front"]
    fw, fh = a["front_w"], a["front_h"]
    p.append(_probe("drawer front", (fw / 2 - E, tfr / 2, fh - E), True))
    p.append(_probe("drawer floor", (0.0, tfr + bl / 2, tf / 2), True))
    for s in (-1, 1):
        p.append(_probe("drawer side wall", (s * (bw / 2 - tw / 2), tfr + bl / 2, bh - E), True))
    p.append(_probe("drawer back wall", (0.0, tfr + bl - tw / 2, bh - E), True))
    # centre of the first compartment: a divider may stand at x = 0
    iw = bw - 2 * tw
    k = len(a["dividers"])
    x_open = -iw / 2 + (iw - k * tw) / (k + 1) / 2
    p.append(_probe("drawer is open at the top", (x_open, tfr + bl / 2, bh - E), False))
    p.append(_probe("drawer interior is empty", (bw / 2 - tw - E, tfr + E, tf + E), False))
    p.append(_probe("nothing behind the drawer", (0.0, tfr + bl + E, bh / 2), False))
    if a["pull_w"] > 0:
        p.append(_probe("finger pull", (0.0, tfr / 2, a["pull_z"]), False))
    for x in a["dividers"]:
        p.append(_probe("divider x=%g" % x, (x, tfr + bl / 2, bh - E), True))
    return p


def check_label(c, a):
    p = []
    ow, oh = a["outer_w"], a["outer_h"]
    tb, tf = a["t_back"], a["t_front"]
    pw, ph, pt = a["pocket_w"], a["pocket_h"], a["pocket_t"]
    zb = a["bottom_wall"]
    p.append(_probe("back plate", (0.0, tb / 2, oh / 2), True))
    p.append(_probe("back plate at its outer edge", (ow / 2 - E, tb / 2, E), True))
    p.append(_probe("card pocket", (0.0, tb + pt / 2, zb + ph / 2), False))
    p.append(_probe("card pocket corner", (pw / 2 - E, tb + pt / 2, zb + E), False))
    p.append(_probe("card pocket open at the top", (0.0, tb + pt / 2, oh - E), False))
    p.append(_probe("window through the front frame", (0.0, tb + pt + tf / 2, zb + ph / 2), False))
    p.append(_probe("front lip over the card edge", (pw / 2 - E, tb + pt + tf / 2, zb + ph / 2), True))
    p.append(_probe("bottom lip", (0.0, tb + pt + tf / 2, zb + a["lip"] - E), True))
    p.append(_probe("bottom wall under the pocket", (0.0, tb + pt / 2, zb - E), True))
    for x in a["holes_x"]:
        p.append(_probe("mount hole x=%g" % x, (x, tb / 2, oh / 2), False))
        p.append(_probe("ear beside hole x=%g" % x, (x, tb / 2, oh - E), True))
    return p


def check_comb(c, a):
    p = []
    L, t, bh = a["length"], a["t"], a["base_h"]
    r, zc, th = a["slot_d"] / 2, a["slot_z"], a["throat"]
    top = a["height"]
    p.append(_probe("base bar", (L / 2 - E, t / 2, bh / 2), True))
    for s in (-1, 1):
        p.append(_probe("end finger", (s * (L / 2 - E), t / 2, top - E), True))
    for x in a["slots_x"]:
        p.append(_probe("cable seat x=%g is open" % x, (x, t / 2, zc), False))
        p.append(_probe("cable seat rim x=%g" % x, (x + r - E, t / 2, zc), False))
        p.append(_probe("base under cable seat x=%g" % x, (x, t / 2, zc - r - E), True))
        p.append(_probe("entry throat x=%g is open" % x, (x, t / 2, top - E), False))
        if th < 2 * r - 2 * E and top - (zc + r) > 2 * E:
            p.append(_probe("retention shoulder beside throat x=%g" % x, (x + th / 2 + E, t / 2, zc + r + E), True))
    xs = a["slots_x"]
    for i in range(len(xs) - 1):
        p.append(_probe("finger between slots %d and %d" % (i, i + 1), ((xs[i] + xs[i + 1]) / 2, t / 2, top - E), True))
    for x in a["holes_x"]:
        p.append(_probe("mount hole x=%g" % x, (x, t / 2, bh / 2), False))
    return p


def check_cartridge(c, a):
    p = []
    pw, pd, H, tf = a["pocket_w"], a["pocket_d"], a["height"], a["t_floor"]
    depth = H - tf
    for i, (x, y) in enumerate(a["pockets"]):
        p.append(_probe("pocket %d is open" % i, (x, y, tf + depth / 2), False))
        p.append(_probe("pocket %d corner" % i, (x + pw / 2 - E, y + pd / 2 - E, tf + E), False))
        p.append(_probe("floor under pocket %d" % i, (x + pw / 2 - E, y + pd / 2 - E, tf / 2), True))
        if a["floor_hole_d"] > 0:
            p.append(_probe("push-out hole under pocket %d" % i, (x, y, tf / 2), False))
        if a["notches"]:
            p.append(_probe("finger notch in front of pocket %d" % i, (x, y - pd / 2 - E, H - E), False))
            p.append(_probe("wall below the notch at pocket %d" % i,
                            (x, y - pd / 2 - E, H - c["finger_notch_depth_mm"] - E), True))
    xs = sorted({x for x, _ in a["pockets"]})
    ys = sorted({y for _, y in a["pockets"]})
    if len(xs) > 1:
        p.append(_probe("divider between columns", ((xs[0] + xs[1]) / 2, ys[0], H - E), True))
    if len(ys) > 1:
        p.append(_probe("divider between rows", (xs[0] + pw / 2 - E, (ys[0] + ys[1]) / 2, H - E), True))
    ow, od = a["outer_w"], a["outer_d"]
    for sx in (-1, 1):
        for y in (E, od - E):   # corners: clear of finger notches, which sit in front of each pocket
            p.append(_probe("outer wall corner", (sx * (ow / 2 - E), y, H - E), True))
    if a["floor_hole_d"] == 0:
        p.append(_probe("solid floor under pocket 0", (a["pockets"][0][0], a["pockets"][0][1], tf / 2), True))
    return p


CHECKERS = {
    "rack_bracket": check_bracket, "sff_cradle": check_cradle, "switch_cradle": check_switch,
    "blanking_panel": check_panel, "cable_panel": check_panel, "rack_shelf": check_shelf, "pdu_mount": check_pdu,
    "keystone_panel": check_keystone, "label_holder": check_label, "cable_comb": check_comb,
    "cartridge_holder": check_cartridge,
}


def probes_for(cfg, geo):
    if cfg["part"] == "rack_drawer":
        fn = check_drawer_housing if cfg["component"] == "housing" else check_drawer_box
    else:
        fn = CHECKERS[cfg["part"]]
    return fn(cfg, geo["args"])


def run(cfg, geo, tris):
    """Evaluate every probe for this part against the mesh. Returns a report dict with ``ok``."""
    solid = Solid(tris)
    results = []
    for pr in probes_for(cfg, geo):
        got = "material" if solid.inside(pr["point_mm"]) else "empty"
        results.append(dict(pr, got=got, ok=got == pr["expect"]))
    failed = [r for r in results if not r["ok"]]
    return {
        "checker": cfg["part"] if cfg["part"] != "rack_drawer" else "rack_drawer/" + cfg["component"],
        "probes": len(results),
        "failed": len(failed),
        "problems": ["spec probe failed: %s (expected %s at %s, found %s)"
                     % (r["label"], r["expect"], r["point_mm"], r["got"]) for r in failed],
        "results": results,
        "ok": not failed and bool(results),
    }


def check_output(out_dir):
    """Re-run the spec check on a generate output directory (manifest.json + STL)."""
    import json
    import os
    from . import mesh
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    if "stl" not in man["files"]:
        raise mesh.MeshError("no STL in this output (generated with --scad-only)")
    cfg, geo = validate.validate(man["input"]["parameters"])
    return run(cfg, geo, mesh.read_stl(os.path.join(out_dir, man["files"]["stl"]["path"])))
