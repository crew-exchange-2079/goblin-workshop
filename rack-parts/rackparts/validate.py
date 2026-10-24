"""Strict input loading, per-field checks and cross-field geometry checks.

Input JSON is treated purely as data: it is parsed with the stdlib parser,
non-finite constants and duplicate keys are rejected, and only values listed
in spec.py are accepted. Nothing from it reaches a shell or a file path except
``name``, which is restricted to [a-z0-9-].
"""

import json
import math
import re

from . import spec

MAX_INPUT_BYTES = 64 * 1024
MAX_DEPTH = 4            # object -> list -> [y, z] needs 3
MAX_NUMBER_CHARS = 32    # longest numeric literal accepted
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")  # always used with fullmatch


class ConfigError(Exception):
    """Input refused. ``problems`` lists every reason found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _reject_constant(name):
    raise ConfigError(["non-finite number literal %s is not allowed" % name])


def _parse_number(text):
    if len(text) > MAX_NUMBER_CHARS:
        raise ConfigError(["numeric literal longer than %d characters" % MAX_NUMBER_CHARS])
    v = float(text)
    if not math.isfinite(v):
        raise ConfigError(["numeric literal %s is out of range" % text[:MAX_NUMBER_CHARS]])
    return v


def _parse_int(text):
    if len(text) > MAX_NUMBER_CHARS:
        raise ConfigError(["numeric literal longer than %d characters" % MAX_NUMBER_CHARS])
    return int(text)


def _check_depth(text):
    """Refuse deeply nested input before the recursive JSON parser sees it."""
    depth = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ConfigError(["input nested deeper than %d levels" % MAX_DEPTH])
        elif ch in "]}":
            depth -= 1


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ConfigError(["duplicate key %r" % k])
        out[k] = v
    return out


def load_config(path):
    with open(path, "rb") as f:
        raw = f.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ConfigError(["input larger than %d bytes" % MAX_INPUT_BYTES])
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(["input is not UTF-8"])
    _check_depth(text)
    try:
        data = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicates,
                          parse_float=_parse_number, parse_int=_parse_int)
    except (ValueError, RecursionError) as e:  # JSONDecodeError is a ValueError
        raise ConfigError(["invalid JSON: %s" % str(e)[:200]])
    return data


def _is_num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    if isinstance(v, int):  # ints from Python callers can exceed float range
        return abs(v) <= 2 ** 53
    return math.isfinite(v)


def _check_num(name, v, lo, hi, problems):
    if not _is_num(v):
        problems.append("%s must be a finite number" % name)
        return
    if isinstance(lo, tuple):
        if not v > lo[1]:
            problems.append("%s must be > %g (got %g)" % (name, lo[1], v))
    elif lo is not None and v < lo:
        problems.append("%s must be >= %g (got %g)" % (name, lo, v))
    if hi is not None and v > hi:
        problems.append("%s must be <= %g (got %g)" % (name, hi, v))


def check_fields(data):
    """Field-level checks equivalent to the JSON schema. Returns a plain dict."""
    if not isinstance(data, dict):
        raise ConfigError(["top level must be a JSON object"])
    part = data.get("part")
    if not isinstance(part, str) or part not in spec.PARTS:
        raise ConfigError(["part must be one of %s" % sorted(spec.PARTS)])
    fields = spec.PARTS[part]
    known = {f[0] for f in fields}
    problems = []
    for k in data:
        if k not in known:
            problems.append("unknown key %r for part %s" % (k, part))
    for name, ftype, lo, hi, _kind, _desc in fields:
        if name not in data:
            problems.append("missing required key %r" % name)
            continue
        v = data[name]
        if ftype.startswith("const:"):
            want = ftype[6:]
            want = int(want) if want.isdigit() else want
            if type(v) is not type(want) or v != want:
                problems.append("%s must be %r" % (name, want))
        elif ftype.startswith("enum:"):
            if v not in ftype[5:].split("|"):
                problems.append("%s must be one of %s" % (name, ftype[5:].split("|")))
        elif ftype == "name":
            if not isinstance(v, str) or not NAME_RE.fullmatch(v):
                problems.append("name must be 1-48 chars of [a-z0-9-], starting with a letter or digit")
        elif ftype == "bool":
            if not isinstance(v, bool):
                problems.append("%s must be true or false" % name)
        elif ftype == "integer":
            if not isinstance(v, int) or isinstance(v, bool):
                problems.append("%s must be an integer" % name)
            else:
                _check_num(name, v, lo, hi, problems)
        elif ftype == "number":
            _check_num(name, v, lo, hi, problems)
        elif ftype in spec.LIST_LIMITS:
            n_lo, n_hi = spec.LIST_LIMITS[ftype]
            if not isinstance(v, list) or not (n_lo <= len(v) <= n_hi):
                problems.append("%s must be a list of %d-%d entries" % (name, n_lo, n_hi))
                continue
            for i, item in enumerate(v):
                if ftype in spec.SCALAR_LISTS:
                    _check_num("%s[%d]" % (name, i), item, lo, hi, problems)
                elif not isinstance(item, list) or len(item) != spec.TUPLE_SIZE[ftype]:
                    problems.append("%s[%d] must be a %d-element list" % (name, i, spec.TUPLE_SIZE[ftype]))
                else:
                    for j, c in enumerate(item):
                        _check_num("%s[%d][%d]" % (name, i, j), c, lo, hi, problems)
    if problems:
        raise ConfigError(problems)
    return dict(data)


def hole_d(d, facets):
    """Diameter to model so an n-gon still clears a round screw of diameter d."""
    return d / math.cos(math.pi / facets)


def _spacing_ok(points, min_dist, label, problems):
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            a, b = points[i], points[j]
            if math.dist(a, b) < min_dist - 1e-9:
                problems.append("%s %d and %d are %.2f mm apart; need >= %.2f mm (hole/head size + min wall)"
                                % (label, i, j, math.dist(a, b), min_dist))


def derive_bracket(c):
    """Return geometry for the right-hand bracket in local coordinates.

    Local frame: x = 0 at the inner (device-side) face of the side flange,
    +x outward toward the rail; y = 0 at the bracket front face, +y into the
    rack; z = 0 at the bracket bottom edge.
    """
    p = []
    fn = c["facets"]
    t = c["thickness_mm"]
    w = c["min_wall_mm"]
    H = c["flange_height_mm"]
    depth = c["side_flange_depth_mm"]
    g = c["gusset_mm"]
    S = c["rail_hole_center_spacing_mm"]
    opening = c["rail_opening_width_mm"]
    rd = hole_d(c["rail_screw_clearance_diameter_mm"], fn)
    rr = rd / 2
    slot = c["rail_slot_extra_length_mm"]
    rhead = c["rail_screw_head_diameter_mm"]
    dd = hole_d(c["device_screw_clearance_diameter_mm"], fn)
    dr = dd / 2
    dhead = c["device_screw_head_diameter_mm"]
    dhead_h = c["device_screw_head_height_mm"]
    side_clr = c["rail_side_clearance_mm"]

    lo, hi = spec.RAIL_SPACING_WINDOW[c["rack_width_class"]]
    if not lo <= S <= hi:
        p.append("rail_hole_center_spacing_mm %g is outside the %s sanity window %g-%g mm "
                 "(check units; this window is not a standard)" % (S, c["rack_width_class"], lo, hi))
    if S <= opening:
        p.append("rail_hole_center_spacing_mm (%g) must exceed rail_opening_width_mm (%g): "
                 "holes are in the rails, outside the opening" % (S, opening))
    if c["max_overall_width_mm"] < S:
        p.append("max_overall_width_mm (%g) is smaller than the rail hole spacing (%g)" % (c["max_overall_width_mm"], S))
    if rhead < c["rail_screw_clearance_diameter_mm"] or dhead < c["device_screw_clearance_diameter_mm"]:
        p.append("screw head diameters must be at least the clearance hole diameters")

    x_inner = c["device_width_mm"] / 2 + c["device_side_gap_mm"]   # absolute, from device centreline
    x_hole = S / 2 - x_inner                                         # local
    rail_inner = opening / 2 - x_inner                               # local x of the rail inner edge

    # Everything behind the front flange must pass between the rails.
    behind = t + max(dhead_h, g)
    if behind + side_clr > rail_inner + 1e-9:
        p.append("unsafe rail clearance: side flange + %s reaches %.2f mm outward but the rail inner edge is at "
                 "%.2f mm (with rail_side_clearance_mm %.2f). Device + brackets would not pass between the rails"
                 % ("gusset" if g > dhead_h else "device screw head", behind, rail_inner, side_clr))
    # The rack screw shank must clear the side flange and gusset.
    shank_inner = x_hole - slot / 2 - rr
    if shank_inner < t + g + w - 1e-9:
        p.append("rail hole inner edge at %.2f mm collides with the side flange/gusset (needs >= %.2f mm)"
                 % (shank_inner, t + g + w))
    ear_w = x_hole + slot / 2 + max(rhead / 2, rr + w)
    overall = 2 * (x_inner + ear_w)
    if overall > c["max_overall_width_mm"] + 1e-9:
        p.append("overall width %.2f mm exceeds max_overall_width_mm %.2f" % (overall, c["max_overall_width_mm"]))

    for i, z in enumerate(c["rail_hole_z_offsets_mm"]):
        if z - rr < w - 1e-9 or z + rr > H - w + 1e-9:
            p.append("rail_hole_z_offsets_mm[%d]=%g leaves less than min_wall_mm to the flange edge "
                     "(flange height %g, hole dia %.2f)" % (i, z, H, rd))
    _spacing_ok([(0.0, z) for z in c["rail_hole_z_offsets_mm"]], rd + w, "rail holes", p)

    dev_holes = []
    for i, (yd, zd) in enumerate(c["device_screw_positions_mm"]):
        y = yd + c["device_setback_mm"]
        z = zd + c["device_bottom_offset_mm"]
        dev_holes.append((y, z))
        if y - dhead / 2 < t + g - 1e-9:
            p.append("device screw %d head (y=%.2f, dia %g) would sit under the front flange/gusset; "
                     "needs y >= %.2f" % (i, y, dhead, t + g + dhead / 2))
        if y - dr < t + w - 1e-9 or y + dr > depth - w + 1e-9:
            p.append("device screw %d at y=%.2f leaves less than min_wall_mm inside side flange depth %g" % (i, y, depth))
        if z - dr < w - 1e-9 or z + dr > H - w + 1e-9:
            p.append("device screw %d at z=%.2f leaves less than min_wall_mm inside flange height %g" % (i, z, H))
    _spacing_ok(dev_holes, max(dhead, dd + w), "device screws", p)

    if p:
        raise ConfigError(p)
    mirror = c["hand"] == "left"
    bbox_min = [-ear_w if mirror else 0.0, 0.0, 0.0]
    bbox_max = [0.0 if mirror else ear_w, depth, H]
    return {
        "module": "rack_bracket",
        "args": {
            "t": t, "h": H, "ear_w": ear_w, "side_depth": depth, "gusset": g,
            "rail_hole_x": x_hole, "rail_hole_zs": list(c["rail_hole_z_offsets_mm"]),
            "rail_d": rd, "slot_len": slot,
            "dev_holes": [list(h) for h in dev_holes], "dev_d": dd,
            "left": mirror, "fn": fn,
        },
        "derived": {
            "inner_face_x_from_device_centre_mm": x_inner,
            "rail_hole_x_local_mm": x_hole,
            "rail_inner_edge_x_local_mm": rail_inner,
            "front_flange_width_mm": ear_w,
            "overall_pair_width_mm": overall,
            "modelled_rail_hole_diameter_mm": rd,
            "modelled_device_hole_diameter_mm": dd,
        },
        "expected_bbox": {"min": bbox_min, "max": bbox_max},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: lay the side flange flat on the bed (bracket rotated so +x points down) "
            "so layer lines run along the flange length; add supports for the front flange only if your slicer "
            "needs them. Not tested on any physical printer."
        ),
    }


def derive_cradle(c):
    """Cradle frame: x centred on the cradle, y = 0 at the front edge, z = 0 at the base bottom."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    tb = c["base_thickness_mm"]
    tw = c["wall_thickness_mm"]
    clr = c["fit_clearance_mm"]
    L = c["cradle_length_mm"]
    lip = c["retention_lip_mm"]
    clear_h = c["wall_clear_height_mm"]
    if c["device_orientation"] == "flat":
        fw, fh = c["device_width_mm"], c["device_height_mm"]
    else:
        fw, fh = c["device_height_mm"], c["device_width_mm"]
    inner_w = fw + 2 * clr
    outer_w = inner_w + 2 * tw
    total_h = tb + clear_h + (tw if lip > 0 else 0)
    hd = hole_d(c["shelf_screw_clearance_diameter_mm"], fn)
    head = c["shelf_screw_head_diameter_mm"]
    cb_d = hole_d(head, fn)
    cb_depth = c["shelf_screw_head_height_mm"]

    if L > c["device_depth_mm"] + 1e-9:
        p.append("cradle_length_mm (%g) is longer than device_depth_mm (%g)" % (L, c["device_depth_mm"]))
    if lip > 0:
        if clear_h < fh + clr - 1e-9:
            p.append("retention lip would press on the device: wall_clear_height_mm %g < device height in this "
                     "orientation (%g) + fit_clearance_mm (%g)" % (clear_h, fh, clr))
        if inner_w - 2 * lip < w:
            p.append("retention_lip_mm %g closes the cradle opening (inner width %.2f)" % (lip, inner_w))
    if outer_w > c["max_overall_width_mm"] + 1e-9:
        p.append("cradle width %.2f exceeds max_overall_width_mm %g" % (outer_w, c["max_overall_width_mm"]))
    if L > c["max_overall_depth_mm"] + 1e-9:
        p.append("cradle length %g exceeds max_overall_depth_mm %g" % (L, c["max_overall_depth_mm"]))
    if total_h > c["max_overall_height_mm"] + 1e-9:
        p.append("cradle height %.2f exceeds max_overall_height_mm %g" % (total_h, c["max_overall_height_mm"]))
    if head < c["shelf_screw_clearance_diameter_mm"]:
        p.append("shelf_screw_head_diameter_mm must be at least the clearance hole diameter")
    if tb - cb_depth < w - 1e-9:
        p.append("unsafe screw clearance: counterbore depth %g leaves %.2f mm of base under the head; "
                 "min_wall_mm is %g" % (cb_depth, tb - cb_depth, w))
    for i, (x, y) in enumerate(c["shelf_hole_positions_mm"]):
        if abs(x) + cb_d / 2 > inner_w / 2 + 1e-9:
            p.append("shelf hole %d head/counterbore at x=%g would cut into a side wall (inner half-width %.2f)"
                     % (i, x, inner_w / 2))
        if y - cb_d / 2 < w - 1e-9 or y + cb_d / 2 > L - w + 1e-9:
            p.append("shelf hole %d at y=%g leaves less than min_wall_mm to the front/back edge" % (i, y))
    _spacing_ok([tuple(h) for h in c["shelf_hole_positions_mm"]], cb_d + w, "shelf holes", p)

    if p:
        raise ConfigError(p)
    return {
        "module": "sff_cradle",
        "args": {
            "t_base": tb, "t_wall": tw, "inner_w": inner_w, "length": L, "clear_h": clear_h,
            "lip": lip, "holes": [list(h) for h in c["shelf_hole_positions_mm"]],
            "hole_d": hd, "cb_d": cb_d, "cb_depth": cb_depth, "fn": fn,
        },
        "derived": {
            "footprint_width_mm": fw,
            "footprint_height_mm": fh,
            "inner_width_mm": inner_w,
            "outer_width_mm": outer_w,
            "overall_height_mm": total_h,
            "modelled_shelf_hole_diameter_mm": hd,
            "modelled_counterbore_diameter_mm": cb_d,
        },
        "expected_bbox": {"min": [-outer_w / 2, 0.0, 0.0], "max": [outer_w / 2, L, total_h]},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print base-down as modelled; the retention lip is an overhang and "
            "will likely need supports or a chamfer you add yourself. Not tested on any physical printer."
        ),
    }


def _fits_plate(a, b, c):
    """True if an a x b footprint fits the build plate in either rotation."""
    px, py = c["build_plate_x_mm"], c["build_plate_y_mm"]
    return (a <= px + 1e-9 and b <= py + 1e-9) or (a <= py + 1e-9 and b <= px + 1e-9)


def _rects(name, rects, problems):
    out = []
    for i, (a0, a1, b0, b1) in enumerate(rects):
        if not (a0 < a1 and b0 < b1):
            problems.append("%s[%d] must be [lo, hi, lo, hi] with lo < hi in both axes" % (name, i))
        out.append((a0, a1, b0, b1))
    return out


def _overlap(a0, a1, b0, b1, gap=0.0):
    """Open intervals [a0,a1] and [b0,b1] are closer than ``gap`` (overlap if gap == 0)."""
    return a0 < b1 + gap - 1e-9 and b0 < a1 + gap - 1e-9


def _point_rect_dist(px, py, x0, x1, y0, y1):
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return math.hypot(dx, dy)


def _seg_dist_h(c1, c2, half):
    """Distance between two horizontal segments of half-length ``half`` centred at c1, c2 (x, z)."""
    dx = max(0.0, abs(c1[0] - c2[0]) - 2 * half)
    return math.hypot(dx, c1[1] - c2[1])


def _rail_front_checks(c, W, H, p):
    """Rail-hole checks shared by every part whose front plate screws to the rails (panels, shelf)."""
    fn = c["facets"]
    w = c["min_wall_mm"]
    S = c["rail_hole_center_spacing_mm"]
    opening = c["rail_opening_width_mm"]
    rd = hole_d(c["rail_screw_clearance_diameter_mm"], fn)
    rr = rd / 2
    slot = c["rail_slot_extra_length_mm"]
    head = c["rail_screw_head_diameter_mm"]
    lo, hi = spec.RAIL_SPACING_WINDOW[c["rack_width_class"]]
    if not lo <= S <= hi:
        p.append("rail_hole_center_spacing_mm %g is outside the %s sanity window %g-%g mm "
                 "(check units; this window is not a standard)" % (S, c["rack_width_class"], lo, hi))
    if S <= opening:
        p.append("rail_hole_center_spacing_mm (%g) must exceed rail_opening_width_mm (%g): "
                 "holes are in the rails, outside the opening" % (S, opening))
    if W <= opening:
        p.append("panel_width_mm (%g) must exceed rail_opening_width_mm (%g) so the panel covers the rails"
                 % (W, opening))
    if head < c["rail_screw_clearance_diameter_mm"]:
        p.append("rail_screw_head_diameter_mm must be at least rail_screw_clearance_diameter_mm")
    outer_hole_edge = S / 2 + slot / 2 + max(head / 2, rr + w)
    if outer_hole_edge > W / 2 + 1e-9:
        p.append("rail hole/head at x=+-%.2f reaches %.2f mm from the centre but the panel half-width is %.2f "
                 "(needs head bearing and min_wall_mm to the side edge)" % (S / 2, outer_hole_edge, W / 2))
    for i, z in enumerate(c["rail_hole_z_offsets_mm"]):
        # Vertically only hole + min_wall is required: on 1U EIA spacing a 12 mm head overhangs the edge
        # slightly, as it does on commercial panels. Horizontally the head must bear fully (checked above).
        if z - rr < w - 1e-9 or z + rr > H - w + 1e-9:
            p.append("rail_hole_z_offsets_mm[%d]=%g leaves less than min_wall_mm to the panel edge "
                     "(panel height %g, hole dia %.2f)" % (i, z, H, rd))
    _spacing_ok([(0.0, z) for z in c["rail_hole_z_offsets_mm"]], rd + w, "rail holes", p)

    return rd, rr


def _stiffener_checks(c, p):
    """Stiffening-rib checks shared by the flat panels (blanking, cable, keystone). Returns the rib half-width."""
    fn = c["facets"]
    w = c["min_wall_mm"]
    H = c["panel_height_mm"]
    rr = hole_d(c["rail_screw_clearance_diameter_mm"], fn) / 2
    rib_depth = c["stiffener_depth_mm"]
    rib_t = c["stiffener_thickness_mm"] if rib_depth > 0 else 0.0
    rib_half = c["rail_opening_width_mm"] / 2 - c["rail_side_clearance_mm"]
    shank_inner = c["rail_hole_center_spacing_mm"] / 2 - c["rail_slot_extra_length_mm"] / 2 - rr
    if rib_depth > 0:
        if rib_half <= 0:
            p.append("rail_side_clearance_mm leaves no room for stiffeners inside the rail opening")
        if shank_inner < rib_half - 1e-9:
            p.append("stiffener (half-width %.2f) would obstruct the rail screw shank (inner edge at %.2f)"
                     % (rib_half, shank_inner))
        if rib_t < w - 1e-9:
            p.append("stiffener_thickness_mm %g is thinner than min_wall_mm %g" % (rib_t, w))
        if 2 * rib_t > H - w + 1e-9:
            p.append("two stiffeners of %g mm leave less than min_wall_mm of open height in a %g mm panel" % (rib_t, H))
    return rib_half


def _panel_window(c, rib_half):
    """Region of a panel face that cut-outs may use: |x| <= x_lim and z_lo <= z <= z_hi.

    Inside the rail opening minus rail_side_clearance_mm (or the ribs), min_wall_mm clear of the rail screw
    heads, and min_wall_mm clear of the panel edges or stiffeners.
    """
    w = c["min_wall_mm"]
    rib_depth = c["stiffener_depth_mm"]
    rib_t = c["stiffener_thickness_mm"] if rib_depth > 0 else 0.0
    S = c["rail_hole_center_spacing_mm"]
    x_lim = min(rib_half if rib_depth > 0 else c["rail_opening_width_mm"] / 2 - c["rail_side_clearance_mm"],
                S / 2 - c["rail_slot_extra_length_mm"] / 2 - c["rail_screw_head_diameter_mm"] / 2 - w)
    z_lo = rib_t + w
    return x_lim, z_lo, c["panel_height_mm"] - z_lo


FILLET_STEPS = 6  # straight-sided steps used to approximate each quarter-round edge


def derive_panel(c):
    """Blanking or cable panel. Frame: x centred, y = 0 at the front face, z = 0 at the bottom edge."""
    p = []
    cable = c["part"] == "cable_panel"
    fn = c["facets"]
    w = c["min_wall_mm"]
    W = c["panel_width_mm"]
    H = c["panel_height_mm"]
    t = c["panel_thickness_mm"]
    S = c["rail_hole_center_spacing_mm"]
    opening = c["rail_opening_width_mm"]
    rd = hole_d(c["rail_screw_clearance_diameter_mm"], fn)
    slot = c["rail_slot_extra_length_mm"]
    head = c["rail_screw_head_diameter_mm"]
    side_clr = c["rail_side_clearance_mm"]
    rib_depth = c["stiffener_depth_mm"]
    rib_t = c["stiffener_thickness_mm"] if rib_depth > 0 else 0.0

    _rail_front_checks(c, W, H, p)
    rib_half = _stiffener_checks(c, p)

    cuts = []
    cut_len = cut_h = fillet = 0.0
    face = {}
    if cable:
        cut_len = c["pass_through_width_mm"]
        cut_h = c["pass_through_height_mm"]
        fillet = c["pass_through_edge_radius_mm"]
        if cut_h > cut_len + 1e-9:
            p.append("pass_through_height_mm (%g) must not exceed pass_through_width_mm (%g)" % (cut_h, cut_len))
        if 2 * fillet > t + 1e-9:
            p.append("pass_through_edge_radius_mm %g is more than half the panel thickness %g" % (fillet, t))
        if cut_h < c["cable_bundle_diameter_mm"] - 1e-9:
            p.append("obstructed cable path: pass_through_height_mm %g is smaller than cable_bundle_diameter_mm %g"
                     % (cut_h, c["cable_bundle_diameter_mm"]))
        cut_h_m = hole_d(cut_h, fn)             # modelled so the polygon still clears cut_h
        R = cut_h_m / 2 + fillet                # half-height of the opening on each face
        half = max(cut_len - cut_h_m, 0.0) / 2  # half-length of the stadium's straight part
        x_lim = min(rib_half if rib_depth > 0 else opening / 2 - side_clr,
                    S / 2 - slot / 2 - head / 2 - w)
        z_lo = (rib_t if rib_depth > 0 else 0.0) + w
        z_hi = H - z_lo
        for i, (x, z) in enumerate(c["pass_through_centers_mm"]):
            cuts.append([x, z])
            if abs(x) + half + R > x_lim + 1e-9:
                p.append("pass-through %d (x=%g) reaches %.2f mm from the centre; it must stay within %.2f mm "
                         "(inside the rail opening minus rail_side_clearance_mm, and min_wall_mm from screw heads)"
                         % (i, x, abs(x) + half + R, x_lim))
            if z - R < z_lo - 1e-9 or z + R > z_hi + 1e-9:
                p.append("pass-through %d (z=%g, face height %.2f) must stay between z=%.2f and z=%.2f "
                         "(panel edges/stiffeners + min_wall_mm)" % (i, z, 2 * R, z_lo, z_hi))
        for i in range(len(cuts)):
            for j in range(i + 1, len(cuts)):
                gap = _seg_dist_h(cuts[i], cuts[j], half) - 2 * R
                if gap < w - 1e-9:
                    p.append("pass-throughs %d and %d leave %.2f mm of material between them on the face; "
                             "need >= min_wall_mm %g" % (i, j, gap, w))
        face = {"pass_through_face_width_mm": 2 * (half + R), "pass_through_face_height_mm": 2 * R}

    fits = _fits_plate(W, H, c)
    if not fits and not c["allow_oversize_print"]:
        p.append("panel face %g x %g mm does not fit the %g x %g mm build plate in either rotation; set "
                 "allow_oversize_print to true to generate it anyway (no split is produced)"
                 % (W, H, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    if p:
        raise ConfigError(p)

    depth = t + rib_depth
    derived = {
        "panel_face_mm": [W, H],
        "overall_depth_mm": depth,
        "rail_hole_x_mm": [-S / 2, S / 2],
        "modelled_rail_hole_diameter_mm": rd,
        "stiffener_half_width_mm": rib_half if rib_depth > 0 else 0.0,
        "fits_build_plate": fits,
        "build_plate_mm": [c["build_plate_x_mm"], c["build_plate_y_mm"]],
    }
    derived.update(face)
    if cable:
        derived["modelled_pass_through_neck_height_mm"] = hole_d(cut_h, fn)
        derived["fillet_steps"] = FILLET_STEPS
    orient = ("UNVALIDATED suggestion: print front face down (rotate so -y points to the bed); stiffeners then "
              "point up and need no supports, and the face gets the smooth bed finish. ")
    if cable:
        orient += ("The far-side rounded edge of each pass-through is a shallow overhang; most slicers print it "
                   "without supports but check the preview. ")
    if not fits:
        orient += ("WARNING: the %g x %g mm face does NOT fit the stated %g x %g mm build plate; this STL is one "
                   "piece and cannot be printed on that printer. No modular joint is provided. " %
                   (W, H, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    orient += "Not tested on any physical printer."
    return {
        "module": "rack_panel",
        "args": {
            "w": W, "h": H, "t": t, "hole_x": S / 2, "hole_zs": list(c["rail_hole_z_offsets_mm"]),
            "rail_d": rd, "slot_len": slot, "rib_half": rib_half, "rib_depth": rib_depth, "rib_t": rib_t,
            "cuts": cuts, "cut_len": cut_len, "cut_h": hole_d(cut_h, fn) if cable else 0.0,
            "fillet": fillet, "steps": FILLET_STEPS, "fn": fn,
        },
        "derived": derived,
        "expected_bbox": {"min": [-W / 2, 0.0, 0.0], "max": [W / 2, depth, H]},
        "print_orientation_suggestion": orient,
    }


def derive_switch(c):
    """Switch cradle frame: x centred, y = 0 at the cradle front edge, z = 0 at the base bottom."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    tb = c["base_thickness_mm"]
    tw = c["wall_thickness_mm"]
    clr = c["fit_clearance_mm"]
    dw, dh, dd = c["device_width_mm"], c["device_height_mm"], c["device_depth_mm"]
    inner_w = dw + 2 * clr
    outer_w = inner_w + 2 * tw
    L = dd + 2 * clr + 2 * tw               # stop tabs (wall_thickness deep) at both ends
    y_dev = tw + clr                        # device front face, cradle y
    tab_w = c["stop_tab_width_mm"]
    tab_h = c["stop_tab_height_mm"]
    s_len = c["strap_slot_length_mm"]
    s_h = c["strap_slot_height_mm"]
    bar = c["strap_bar_height_mm"]
    z_slot = tb + dh + clr                  # strap slot bottom
    wall_h = z_slot + s_h + bar
    hd = hole_d(c["shelf_screw_clearance_diameter_mm"], fn)
    head = c["shelf_screw_head_diameter_mm"]
    cb_d = hole_d(head, fn)
    cb_depth = c["shelf_screw_head_height_mm"]

    # retention
    if tab_w - clr < w - 1e-9:
        p.append("missing retention: stop tabs overlap the device face by %.2f mm (stop_tab_width_mm - "
                 "fit_clearance_mm); need >= min_wall_mm %g" % (tab_w - clr, w))
    if 2 * tab_w > inner_w - w + 1e-9:
        p.append("stop tabs (%g mm each) close the front/rear opening (inner width %.2f)" % (tab_w, inner_w))
    if bar < w - 1e-9:
        p.append("strap_bar_height_mm %g is below min_wall_mm %g" % (bar, w))
    if tab_h > dh + 1e-9:
        p.append("stop_tab_height_mm %g is taller than the device (%g)" % (tab_h, dh))

    # port clearances: tabs occupy x in [inner_w/2 - tab_w, inner_w/2] (mirrored), z in [0, tab_h] (device frame)
    for face_name in ("front_port_zone_mm", "rear_port_zone_mm"):
        for i, (x0, x1, z0, z1) in enumerate(_rects(face_name, c[face_name], p)):
            if x0 < -dw / 2 - 1e-9 or x1 > dw / 2 + 1e-9 or z0 < -1e-9 or z1 > dh + 1e-9:
                p.append("%s[%d] lies outside the %g x %g mm device face" % (face_name, i, dw, dh))
            for sgn in (-1, 1):
                tx0, tx1 = sorted((sgn * (inner_w / 2 - tab_w), sgn * inner_w / 2))
                if _overlap(x0, x1, tx0, tx1) and z0 < tab_h - 1e-9:
                    p.append("obstructed port: %s stop tab (x %.2f..%.2f, up to z=%.2f above the device bottom) "
                             "covers %s[%d]" % ("front" if face_name.startswith("front") else "rear",
                                                tx0, tx1, tab_h, face_name, i))

    # side vents -> wall windows
    windows = []
    for i, (y0, y1, z0, z1) in enumerate(_rects("side_vent_zone_mm", c["side_vent_zone_mm"], p)):
        if y0 < -1e-9 or y1 > dd + 1e-9 or z0 < -1e-9 or z1 > dh + 1e-9:
            p.append("side_vent_zone_mm[%d] lies outside the %g x %g mm device side" % (i, dd, dh))
        wy0, wy1 = y_dev + y0, y_dev + y1
        edge = max(w, tw)
        if wy0 < edge - 1e-9 or wy1 > L - edge + 1e-9:
            p.append("obstructed vent: side_vent_zone_mm[%d] window (y %.2f..%.2f) leaves less than "
                     "max(min_wall_mm, wall_thickness_mm) of wall at the cradle ends (length %.2f)" % (i, wy0, wy1, L))
        windows.append([wy0, wy1, tb + z0, tb + z1])
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            a, b = windows[i], windows[j]
            if _overlap(a[0], a[1], b[0], b[1], w) and _overlap(a[2], a[3], b[2], b[3], w):
                p.append("vent windows %d and %d are closer than min_wall_mm" % (i, j))

    # strap slots
    slots = []
    for i, ys in enumerate(c["strap_slot_y_mm"]):
        sy0, sy1 = y_dev + ys - s_len / 2, y_dev + ys + s_len / 2
        if sy0 < y_dev - 1e-9 or sy1 > y_dev + dd + 1e-9:
            p.append("missing retention: strap slot %d (y %.2f..%.2f) is not over the device top" % (i, sy0, sy1))
        if sy0 < max(w, tw) - 1e-9 or sy1 > L - max(w, tw) + 1e-9:
            p.append("strap slot %d leaves too little wall at the cradle ends" % i)
        for j, wd in enumerate(windows):
            if _overlap(sy0, sy1, wd[0], wd[1], w) and wd[3] > z_slot - w + 1e-9:
                p.append("strap slot %d and vent window %d leave less than min_wall_mm of wall between them" % (i, j))
        slots.append([sy0, sy1])
    for i in range(len(slots)):
        for j in range(i + 1, len(slots)):
            if _overlap(slots[i][0], slots[i][1], slots[j][0], slots[j][1], w):
                p.append("strap slots %d and %d are closer than min_wall_mm" % (i, j))

    # base vents
    n = c["base_vent_slot_count"]
    vw = c["base_vent_slot_width_mm"]
    vl = c["base_vent_slot_length_mm"]
    pitch = c["base_vent_slot_pitch_mm"]
    vents = []
    if n > 0:
        if vl < vw - 1e-9:
            p.append("base_vent_slot_length_mm must be at least base_vent_slot_width_mm")
        if n > 1 and pitch - vw < w - 1e-9:
            p.append("base vent slots %g mm wide at %g mm pitch leave less than min_wall_mm between them" % (vw, pitch))
        span = (n - 1) * pitch + vw
        if span > inner_w - 2 * w + 1e-9:
            p.append("base vent slots span %.2f mm but only %.2f mm fits inside the walls with min_wall_mm"
                     % (span, inner_w - 2 * w))
        vy0, vy1 = L / 2 - vl / 2, L / 2 + vl / 2
        if vy0 < tw + w - 1e-9:
            p.append("base vent slots (length %g) run into the stop tabs / cradle ends" % vl)
        for k in range(n):
            vents.append([(k - (n - 1) / 2) * pitch, vy0, vy1])

    # shelf holes
    if head < c["shelf_screw_clearance_diameter_mm"]:
        p.append("shelf_screw_head_diameter_mm must be at least the clearance hole diameter")
    if tb - cb_depth < w - 1e-9:
        p.append("unsafe screw clearance: counterbore depth %g leaves %.2f mm of base under the head; "
                 "min_wall_mm is %g" % (cb_depth, tb - cb_depth, w))
    for i, (x, y) in enumerate(c["shelf_hole_positions_mm"]):
        rcb = cb_d / 2
        if abs(x) + rcb > inner_w / 2 + 1e-9:
            p.append("shelf hole %d head/counterbore at x=%g would cut into a side wall (inner half-width %.2f)"
                     % (i, x, inner_w / 2))
        if y - rcb < w - 1e-9 or y + rcb > L - w + 1e-9:
            p.append("shelf hole %d at y=%g leaves less than min_wall_mm to the front/back edge" % (i, y))
        for ty0, ty1 in ((0.0, tw), (L - tw, L)):
            for sgn in (-1, 1):
                tx0, tx1 = sorted((sgn * (inner_w / 2 - tab_w), sgn * inner_w / 2))
                if _point_rect_dist(x, y, tx0, tx1, ty0, ty1) < rcb - 1e-9:
                    p.append("shelf hole %d counterbore runs under a stop tab (screw head would be blocked)" % i)
        for k, (vx, vy0, vy1) in enumerate(vents):
            if _point_rect_dist(x, y, vx - vw / 2, vx + vw / 2, vy0, vy1) < rcb + w - 1e-9:
                p.append("shelf hole %d and base vent slot %d are closer than min_wall_mm" % (i, k))
    _spacing_ok([tuple(h) for h in c["shelf_hole_positions_mm"]], cb_d + w, "shelf holes", p)

    # envelope
    if outer_w > c["max_overall_width_mm"] + 1e-9:
        p.append("cradle width %.2f exceeds max_overall_width_mm %g" % (outer_w, c["max_overall_width_mm"]))
    if L > c["max_overall_depth_mm"] + 1e-9:
        p.append("cradle length %.2f exceeds max_overall_depth_mm %g" % (L, c["max_overall_depth_mm"]))
    if wall_h > c["max_overall_height_mm"] + 1e-9:
        p.append("cradle height %.2f exceeds max_overall_height_mm %g" % (wall_h, c["max_overall_height_mm"]))
    if not _fits_plate(outer_w, L, c):
        p.append("cradle footprint %.2f x %.2f mm does not fit the %g x %g mm build plate"
                 % (outer_w, L, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    if p:
        raise ConfigError(p)
    return {
        "module": "switch_cradle",
        "args": {
            "t_base": tb, "t_wall": tw, "inner_w": inner_w, "length": L, "wall_h": wall_h,
            "tab_w": tab_w, "tab_h": tab_h, "windows": windows, "slots": slots, "slot_z0": z_slot,
            "slot_h": s_h, "vents": vents, "vent_w": vw, "holes": [list(h) for h in c["shelf_hole_positions_mm"]],
            "hole_d": hd, "cb_d": cb_d, "cb_depth": cb_depth, "fn": fn,
        },
        "derived": {
            "inner_width_mm": inner_w,
            "outer_width_mm": outer_w,
            "cradle_length_mm": L,
            "overall_height_mm": wall_h,
            "device_front_face_y_mm": y_dev,
            "strap_slot_bottom_z_mm": z_slot,
            "stop_tab_overlap_on_device_mm": tab_w - clr,
            "side_vent_windows_yz_mm": windows,
            "modelled_shelf_hole_diameter_mm": hd,
            "modelled_counterbore_diameter_mm": cb_d,
        },
        "expected_bbox": {"min": [-outer_w / 2, 0.0, 0.0], "max": [outer_w / 2, L, wall_h]},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print base-down as modelled. The tops of the side vent windows and strap "
            "slots are bridges; keep them short or enable supports if your printer bridges poorly. "
            "Not tested on any physical printer."
        ),
    }

PHYSICAL_USE = ("UNVALIDATED: geometry only. Not printed, fitted, load-, heat-, fire- or electrically tested; "
                "no rating of any kind is given or implied.")


def derive_shelf(c):
    """Shallow ventilated 10-inch shelf. Frame: x centred, y = 0 at the front face, z = 0 at the bottom edge."""
    p = []
    w = c["min_wall_mm"]
    W = c["panel_width_mm"]
    H = c["panel_height_mm"]
    t = c["panel_thickness_mm"]
    S = c["rail_hole_center_spacing_mm"]
    opening = c["rail_opening_width_mm"]
    slot = c["rail_slot_extra_length_mm"]
    side_clr = c["rail_side_clearance_mm"]
    D = c["shelf_depth_mm"]
    tf = c["floor_thickness_mm"]
    wall_h = c["side_wall_height_mm"]
    tw = c["side_wall_thickness_mm"]
    lip_h = c["rear_lip_height_mm"]
    lip_t = tw if lip_h > 0 else 0.0
    clr = c["fit_clearance_mm"]

    if c["rack_width_class"] == "19in":
        # Deferred, not silently oversized: a ~483 mm one-piece shelf does not fit common build plates and
        # no bolt-together joint is implemented or geometrically checked here.
        raise ConfigError(["rack_shelf for 19in racks is deferred and refused: the front plate is too wide for "
                           "common build plates and no bolt-together split joint is implemented or checked; "
                           "use rack_width_class 10in or a commercial 19-inch shelf"])
    rd, rr = _rail_front_checks(c, W, H, p)

    half = opening / 2 - side_clr            # floor/wall outer half-width
    inner_half = half - tw                   # usable half-width between the walls
    shank_inner = S / 2 - slot / 2 - rr
    if inner_half <= w:
        p.append("rail_opening_width_mm minus rail_side_clearance_mm and side walls leaves no floor")
    if shank_inner < half - 1e-9:
        p.append("shelf side wall (half-width %.2f) would obstruct the rail screw shank (inner edge at %.2f)"
                 % (half, shank_inner))
    if tf < w - 1e-9 or tw < w - 1e-9:
        p.append("floor_thickness_mm and side_wall_thickness_mm must be at least min_wall_mm %g" % w)
    if tf + wall_h > H + 1e-9:
        p.append("side walls (floor %g + wall %g mm) rise above the front plate (panel_height_mm %g) into the "
                 "rack space above" % (tf, wall_h, H))
    if lip_h > wall_h + 1e-9:
        p.append("rear_lip_height_mm %g exceeds side_wall_height_mm %g" % (lip_h, wall_h))
    if t + D > c["rack_clear_depth_mm"] + 1e-9:
        p.append("shelf reaches %.2f mm behind the front face but rack_clear_depth_mm is %g" % (t + D, c["rack_clear_depth_mm"]))

    # the measured device must sit on the floor between the walls, in front of the lip, inside the shelf's units
    dw, dd, dh = c["device_width_mm"], c["device_depth_mm"], c["device_height_mm"]
    if dw + 2 * clr > 2 * inner_half + 1e-9:
        p.append("device does not fit: device_width_mm %g + 2 x fit_clearance_mm needs %.2f mm but the walls "
                 "leave %.2f mm" % (dw, dw + 2 * clr, 2 * inner_half))
    if dd + (clr if lip_h > 0 else 0.0) > D - lip_t + 1e-9:
        p.append("device does not fit: device_depth_mm %g%s exceeds the usable floor depth %.2f mm (no overhang "
                 "is allowed)" % (dd, " + fit_clearance_mm" if lip_h > 0 else "", D - lip_t))
    if tf + dh > H + 1e-9:
        p.append("device top (floor %g + device %g mm) would rise above the shelf's own panel height %g into the "
                 "rack space above" % (tf, dh, H))

    # floor vents: rounded slots along the depth, centred across the rack
    n = c["vent_slot_count"]
    vw = c["vent_slot_width_mm"]
    vl = c["vent_slot_length_mm"]
    pitch = c["vent_slot_pitch_mm"]
    vents = []
    if n > 0:
        if vl < vw - 1e-9:
            p.append("vent_slot_length_mm must be at least vent_slot_width_mm")
        if n > 1 and pitch - vw < w - 1e-9:
            p.append("vent slots %g mm wide at %g mm pitch leave less than min_wall_mm between them" % (vw, pitch))
        span = (n - 1) * pitch + vw
        if span > 2 * inner_half - 2 * w + 1e-9:
            p.append("vent slots span %.2f mm but only %.2f mm fits between the side walls with min_wall_mm"
                     % (span, 2 * inner_half - 2 * w))
        y_mid = t + (D - lip_t) / 2
        vy0, vy1 = y_mid - vl / 2, y_mid + vl / 2
        if vy0 < t + w - 1e-9 or vy1 > t + D - lip_t - w + 1e-9:
            p.append("vent slots (length %g) leave less than min_wall_mm to the front plate / rear edge" % vl)
        for k in range(n):
            vents.append([(k - (n - 1) / 2) * pitch, vy0, vy1])

    # printing: either front face down (floor and walls stand up) or floor down (front plate stands up)
    Z = c["build_plate_z_mm"]
    orients = []
    if _fits_plate(W, H, c) and t + D <= Z + 1e-9:
        orients.append("front_face_down")
    if _fits_plate(W, t + D, c) and H <= Z + 1e-9:
        orients.append("floor_down")
    if not orients:
        p.append("shelf (%g wide x %g deep x %g tall) fits the %g x %g x %g mm build volume in neither "
                 "front-face-down nor floor-down orientation; no split is produced"
                 % (W, t + D, H, c["build_plate_x_mm"], c["build_plate_y_mm"], Z))
    if p:
        raise ConfigError(p)

    # Overhangs are listed per orientation; neither is claimed to print without supports.
    orient = "UNVALIDATED suggestion; fitting orientations and their overhangs: "
    if "floor_down" in orients:
        orient += ("floor down (as modelled): the rail holes are horizontal round or slotted holes in the standing front plate "
                   "(their tops overhang). ")
    if "front_face_down" in orients:
        orient += ("front face down (-y to the bed): vent slots become vertical slots with rounded overhanging "
                   "tops (depth becomes build height)")
        if lip_h > 0:
            orient += ("; the rear lip becomes a %.1f mm long horizontal bridge between the side walls, %.1f mm deep, "
                       "with nothing under it: it needs supports or reliable long-span bridging"
                       % (2 * inner_half, lip_h))
        orient += ". "
    orient += "Check your slicer's overhang preview. "
    orient += "Not tested on any physical printer."
    return {
        "module": "rack_shelf",
        "args": {
            "w": W, "h": H, "t": t, "hole_x": S / 2, "hole_zs": list(c["rail_hole_z_offsets_mm"]),
            "rail_d": rd, "slot_len": slot, "half": half, "depth": D, "t_floor": tf, "wall_h": wall_h,
            "t_wall": tw, "lip_h": lip_h, "vents": vents, "vent_w": vw, "fn": c["facets"],
        },
        "derived": {
            "front_plate_mm": [W, H],
            "overall_depth_mm": t + D,
            "rail_hole_x_mm": [-S / 2, S / 2],
            "modelled_rail_hole_diameter_mm": rd,
            "floor_outer_width_mm": 2 * half,
            "usable_width_between_walls_mm": 2 * inner_half,
            "usable_floor_depth_mm": D - lip_t,
            "clear_height_above_floor_within_panel_mm": H - tf,
            "printable_orientations": orients,
            "build_volume_mm": [c["build_plate_x_mm"], c["build_plate_y_mm"], Z],
        },
        "expected_bbox": {"min": [-W / 2, 0.0, 0.0], "max": [W / 2, t + D, H]},
        "print_orientation_suggestion": orient,
        "physical_use": {
            "status": PHYSICAL_USE,
            "required_before_use": [
                "Fit coupon: first print a blanking_panel with identical rail_* and panel_* values (stiffener_depth_mm "
                "0) and confirm it screws to your rails without forcing.",
                "Owner inspection: print the shelf, look for layer splits at the front-plate/floor/wall joints, and "
                "test it empty, then with the device, before leaving anything on it unattended.",
                "The shelf hangs only from the front rail screws (cantilever). No load rating exists; keep heavy "
                "or hot devices off it.",
            ],
        },
    }


def derive_pdu(c):
    """One strap-retained power-strip bracket. Frame: x along the strip (bracket centred), y across, z up."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    Ld = c["device_length_mm"]
    dw, dh = c["device_width_mm"], c["device_height_mm"]
    clr = c["fit_clearance_mm"]
    L = c["bracket_length_mm"]
    tb = c["base_thickness_mm"]
    tw = c["wall_thickness_mm"]
    s_len = c["strap_width_mm"] + c["strap_clearance_mm"]
    s_h = c["strap_thickness_mm"] + c["strap_clearance_mm"]
    bar = c["strap_bar_height_mm"]
    ear = c["ear_width_mm"]
    hd = hole_d(c["mount_screw_clearance_diameter_mm"], fn)
    head = c["mount_screw_head_diameter_mm"]
    margin = c["keepout_margin_mm"]
    end_clr = c["end_clearance_mm"]

    inner_w = dw + 2 * clr
    outer_w = inner_w + 2 * tw + 2 * ear
    z_slot = tb + dh + clr                  # strap slot bottom: the strap only touches the top face
    wall_h = z_slot + s_h + bar
    hole_y = inner_w / 2 + tw + ear / 2     # ear hole centre, both sides

    if tb < w - 1e-9 or tw < w - 1e-9 or bar < w - 1e-9:
        p.append("base_thickness_mm, wall_thickness_mm and strap_bar_height_mm must be at least min_wall_mm %g" % w)
    if s_len > L - 2 * w + 1e-9:
        p.append("strap slot %.2f mm (strap width + clearance) leaves less than min_wall_mm of wall at the bracket "
                 "ends (bracket_length_mm %g)" % (s_len, L))
    if head < c["mount_screw_clearance_diameter_mm"]:
        p.append("mount_screw_head_diameter_mm must be at least mount_screw_clearance_diameter_mm")
    if head > ear + 1e-9 or head > L + 1e-9:
        p.append("unsafe screw clearance: a %g mm screw head does not bear fully on the %g x %g mm ear clear of "
                 "the wall" % (head, ear, L))
    if ear / 2 - hd / 2 < w - 1e-9 or L / 2 - hd / 2 < w - 1e-9:
        p.append("mount screw hole (%.2f mm) leaves less than min_wall_mm in the ear" % hd)

    # keepouts: the bracket, its walls and its strap cover the strip's whole cross-section along [x0, x1]
    faces = (("top_face_keepouts_mm", dw, "outlet/top"), ("side_face_keepouts_mm", dh, "side"),
             ("base_face_keepouts_mm", dw, "base"))
    keepouts = []
    for key, across, label in faces:
        for i, (x0, x1, a0, a1) in enumerate(_rects(key, c[key], p)):
            if x0 < -1e-9 or x1 > Ld + 1e-9 or a0 < -1e-9 or a1 > across + 1e-9:
                p.append("%s[%d] lies outside the %g x %g mm face" % (key, i, Ld, across))
            keepouts.append((key, i, label, x0, x1))
    spans = []
    for i, xc in enumerate(c["bracket_x_positions_mm"]):
        x0, x1 = xc - L / 2, xc + L / 2
        spans.append((x0, x1))
        if x0 < end_clr - 1e-9 or x1 > Ld - end_clr + 1e-9:
            p.append("cord/end clearance: bracket %d (x %.2f..%.2f) is closer than end_clearance_mm %g to a strip "
                     "end (length %g)" % (i, x0, x1, end_clr, Ld))
        for key, k, label, k0, k1 in keepouts:
            if _overlap(x0, x1, k0, k1, margin):
                p.append("keepout violated: bracket %d and its strap (x %.2f..%.2f) come within keepout_margin_mm "
                         "%g of %s[%d] (x %g..%g, %s face)" % (i, x0, x1, margin, key, k, k0, k1, label))
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if _overlap(spans[i][0], spans[i][1], spans[j][0], spans[j][1]):
                p.append("brackets %d and %d overlap along the strip" % (i, j))

    if outer_w > c["max_overall_width_mm"] + 1e-9:
        p.append("bracket width %.2f exceeds max_overall_width_mm %g" % (outer_w, c["max_overall_width_mm"]))
    strap_top = wall_h + c["strap_thickness_mm"]
    if strap_top > c["max_overall_height_mm"] + 1e-9:
        p.append("bracket height with the strap looped over the bar (%.2f) exceeds max_overall_height_mm %g"
                 % (strap_top, c["max_overall_height_mm"]))
    if not (_fits_plate(L, outer_w, c) and wall_h <= c["build_plate_z_mm"] + 1e-9):
        p.append("bracket %.2f x %.2f x %.2f mm does not fit the %g x %g x %g mm build volume base-down"
                 % (L, outer_w, wall_h, c["build_plate_x_mm"], c["build_plate_y_mm"], c["build_plate_z_mm"]))
    if p:
        raise ConfigError(p)
    n = len(c["bracket_x_positions_mm"])
    return {
        "module": "pdu_mount",
        "args": {
            "length": L, "inner_w": inner_w, "t_base": tb, "t_wall": tw, "ear": ear, "wall_h": wall_h,
            "slot_z0": z_slot, "slot_len": s_len, "slot_h": s_h, "hole_y": hole_y, "hole_d": hd, "fn": fn,
        },
        "derived": {
            "brackets_to_print": n,
            "bracket_spans_along_strip_mm": [list(sp) for sp in spans],
            "inner_width_mm": inner_w,
            "outer_width_mm": outer_w,
            "overall_height_mm": wall_h,
            "height_with_strap_loop_mm": strap_top,
            "strap_slot_mm": [s_len, s_h],
            "strap_slot_bottom_z_mm": z_slot,
            "mount_hole_y_mm": [-hole_y, hole_y],
            "modelled_mount_hole_diameter_mm": hd,
        },
        "expected_bbox": {"min": [-L / 2, -outer_w / 2, 0.0], "max": [L / 2, outer_w / 2, wall_h]},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print %d copies base-down as modelled. The strap slot tops are %.1f mm "
            "bridges. Not tested on any physical printer." % (n, s_len)),
        "physical_use": {
            "status": PHYSICAL_USE + " Non-electrical accessory: it does not open, drill, cover or modify the strip.",
            "required_before_use": [
                "Fit coupon: print ONE bracket first, seat the unplugged strip in it, strap it, and confirm the strip "
                "is not squeezed, every outlet/switch/vent stays uncovered and the cord exit is free.",
                "Owner inspection: with plugs inserted, check that no plug body, cord or strap is pinched or bent "
                "against the bracket; re-check after a few days (straps creep).",
                "Mount only to a surface and with screws you have judged suitable; no holding force is rated.",
            ],
        },
    }


LATCH_LEDGE_MM = 0.5   # thinned panel needed past the cutout edge for a keystone latch to hook behind


def derive_keystone(c):
    """Keystone patch panel. Frame: x centred, y = 0 at the front face, z = 0 at the bottom edge."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    W = c["panel_width_mm"]
    H = c["panel_height_mm"]
    t = c["panel_thickness_mm"]
    S = c["rail_hole_center_spacing_mm"]
    rib_depth = c["stiffener_depth_mm"]
    rib_t = c["stiffener_thickness_mm"] if rib_depth > 0 else 0.0
    n = c["keystone_count"]
    pitch = c["keystone_pitch_mm"]
    cw, ch = c["keystone_cutout_width_mm"], c["keystone_cutout_height_mm"]
    bw, bh = c["jack_body_width_mm"], c["jack_body_height_mm"]
    jc = c["jack_clearance_mm"]
    seat = c["jack_latch_panel_thickness_mm"]
    rows = list(c["keystone_row_z_mm"])

    rd, _ = _rail_front_checks(c, W, H, p)
    rib_half = _stiffener_checks(c, p)
    if seat > t + 1e-9:
        p.append("jack_latch_panel_thickness_mm %g is thicker than panel_thickness_mm %g: the latch could not "
                 "reach the back of the panel" % (seat, t))
    if bw < cw - 1e-9 or bh < ch - 1e-9:
        p.append("jack body (%g x %g) is smaller than its cutout (%g x %g): measure the body behind the panel, "
                 "including the latch" % (bw, bh, cw, ch))
    pocket = seat < t - 1e-9
    pw, ph = (bw + 2 * jc, bh + 2 * jc) if pocket else (cw, ch)
    if pocket and (ph - ch) / 2 < LATCH_LEDGE_MM - 1e-9:
        # the latch hooks behind the cutout's top/bottom edge: the thinned area must reach past that edge
        p.append("the back pocket reaches only %.2f mm past the cutout's top/bottom edge; the latch would hook "
                 "full-thickness panel. Need >= %g mm (jack_body_height_mm + 2 x jack_clearance_mm - "
                 "keystone_cutout_height_mm)" % ((ph - ch) / 2, LATCH_LEDGE_MM))
    if n > 1 and pitch - pw < w - 1e-9:
        p.append("jacks at %g mm pitch leave %.2f mm between %s; need >= min_wall_mm %g"
                 % (pitch, pitch - pw, "back pockets" if pocket else "cutouts", w))
    xs = [(k - (n - 1) / 2) * pitch for k in range(n)]
    x_lim, z_lo, z_hi = _panel_window(c, rib_half)
    if xs[-1] + pw / 2 > x_lim + 1e-9:
        p.append("row of %d jacks reaches %.2f mm from the centre; it must stay within %.2f mm (inside the rail "
                 "opening minus rail_side_clearance_mm, and min_wall_mm from screw heads)" % (n, xs[-1] + pw / 2, x_lim))
    for i, z in enumerate(rows):
        if z - ph / 2 < z_lo - 1e-9 or z + ph / 2 > z_hi + 1e-9:
            p.append("keystone row %d (z=%g, %s height %.2f) must stay between z=%.2f and z=%.2f (panel edges/"
                     "stiffeners + min_wall_mm)" % (i, z, "pocket" if pocket else "cutout", ph, z_lo, z_hi))
    srows = sorted(rows)
    for i in range(len(srows) - 1):
        if srows[i + 1] - srows[i] - ph < w - 1e-9:
            p.append("keystone rows at z=%g and z=%g leave less than min_wall_mm between them" % (srows[i], srows[i + 1]))
    fits = _fits_plate(W, H, c)
    if not fits and not c["allow_oversize_print"]:
        p.append("panel face %g x %g mm does not fit the %g x %g mm build plate in either rotation; set "
                 "allow_oversize_print to true to generate it anyway (no split is produced)"
                 % (W, H, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    if p:
        raise ConfigError(p)

    orient = ("UNVALIDATED suggestion: print front face down (-y to the bed): the face gets the bed finish and "
              "the cutouts' edges stay square; the back pockets are open toward the top and need no supports. ")
    if not fits:
        orient += ("WARNING: the %g x %g mm face does NOT fit the stated %g x %g mm build plate; this STL is one "
                   "piece and cannot be printed on that printer. No modular joint is provided. " %
                   (W, H, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    orient += "Not tested on any physical printer."
    return {
        "module": "keystone_panel",
        "args": {
            "w": W, "h": H, "t": t, "hole_x": S / 2, "hole_zs": list(c["rail_hole_z_offsets_mm"]),
            "rail_d": rd, "slot_len": c["rail_slot_extra_length_mm"], "rib_half": rib_half,
            "rib_depth": rib_depth, "rib_t": rib_t, "jack_x": xs, "rows": rows,
            "cut_w": cw, "cut_h": ch, "pocket_w": pw, "pocket_h": ph, "seat_t": seat if pocket else t,
            "fn": fn,
        },
        "derived": {
            "panel_face_mm": [W, H],
            "overall_depth_mm": t + rib_depth,
            "rail_hole_x_mm": [-S / 2, S / 2],
            "modelled_rail_hole_diameter_mm": rd,
            "jacks": n * len(rows),
            "jack_x_mm": xs,
            "back_pocket_mm": [pw, ph] if pocket else None,
            "latch_ledge_per_side_mm": [(pw - cw) / 2, (ph - ch) / 2] if pocket else None,
            "panel_thickness_at_latch_mm": seat if pocket else t,
            "fits_build_plate": fits,
            "build_plate_mm": [c["build_plate_x_mm"], c["build_plate_y_mm"]],
        },
        "expected_bbox": {"min": [-W / 2, 0.0, 0.0], "max": [W / 2, t + rib_depth, H]},
        "print_orientation_suggestion": orient,
        "physical_use": {
            "status": PHYSICAL_USE + " Not a cabling or electrical product: it only holds jacks you supply.",
            "required_before_use": [
                "Fit coupon: print a short strip with one cutout (e.g. cut the model in your slicer) and snap a real "
                "jack in and out; adjust keystone_cutout_*_mm and jack_latch_panel_thickness_mm until it holds "
                "without forcing.",
                "Owner inspection: check the thin latch ledges for layer splits after inserting every jack.",
            ],
        },
    }


def _deferred_19in(c, part):
    if c["rack_width_class"] == "19in":
        raise ConfigError(["%s for 19in racks is deferred and refused: the front plate is too wide for common build "
                           "plates and no bolt-together split joint is implemented or checked; use rack_width_class "
                           "10in or a commercial 19-inch part" % part])


def derive_drawer(c):
    """Rack drawer: housing (shelf frame) or drawer box (x centred, y = 0 at its front face, z = 0 at its bottom).

    Both components are derived from the same numbers every run, so the drawer is always checked against
    the housing it must slide into.
    """
    p = []
    w = c["min_wall_mm"]
    W = c["panel_width_mm"]
    H = c["panel_height_mm"]
    t = c["panel_thickness_mm"]
    S = c["rail_hole_center_spacing_mm"]
    slot = c["rail_slot_extra_length_mm"]
    head = c["rail_screw_head_diameter_mm"]
    D = c["sleeve_depth_mm"]
    sh = c["sleeve_height_mm"]
    th = c["housing_wall_thickness_mm"]
    td = c["drawer_wall_thickness_mm"]
    tfr = c["drawer_front_thickness_mm"]
    clr = c["slide_clearance_mm"]
    ov = c["front_overlap_mm"]
    pull_w, pull_h = c["pull_width_mm"], c["pull_height_mm"]
    k = c["divider_count"]

    _deferred_19in(c, "rack_drawer")
    rd, rr = _rail_front_checks(c, W, H, p)
    half = c["rail_opening_width_mm"] / 2 - c["rail_side_clearance_mm"]   # sleeve outer half-width
    shank_inner = S / 2 - slot / 2 - rr
    if shank_inner < half - 1e-9:
        p.append("sleeve side wall (half-width %.2f) would obstruct the rail screw shank (inner edge at %.2f)"
                 % (half, shank_inner))
    if th < w - 1e-9 or td < w - 1e-9 or tfr < w - 1e-9:
        p.append("housing_wall_thickness_mm, drawer_wall_thickness_mm and drawer_front_thickness_mm must be at "
                 "least min_wall_mm %g" % w)
    if sh > H + 1e-9:
        p.append("sleeve_height_mm %g rises above the front plate (panel_height_mm %g) into the rack space above"
                 % (sh, H))
    if t + D > c["rack_clear_depth_mm"] + 1e-9:
        p.append("housing reaches %.2f mm behind the front face but rack_clear_depth_mm is %g" % (t + D, c["rack_clear_depth_mm"]))

    # opening = sleeve interior; the drawer box is the opening minus slide clearance
    open_half = half - th
    open_z0, open_z1 = th, sh - th
    bw = 2 * open_half - 2 * clr
    bh = open_z1 - open_z0 - clr
    bl = t + D - th - clr                    # from the housing front face to clr in front of the back wall
    iw = bw - 2 * td                         # drawer interior
    ih = bh - td
    il = bl - td
    if iw < 2 * w - 1e-9 or ih < w - 1e-9 or il < 2 * w - 1e-9:
        p.append("no usable drawer space: interior %.2f x %.2f x %.2f mm (width x depth x height) after walls and "
                 "clearance" % (iw, il, ih))
    # drawer front: bottom flush with the box bottom, reaches front_overlap_mm past the opening sides and top
    fw = 2 * open_half + 2 * ov
    fh = bh + clr + ov
    screw_clear = S / 2 - slot / 2 - head / 2 - w
    if fw / 2 > screw_clear + 1e-9:
        p.append("drawer front (half-width %.2f) would cover the rail screw heads; keep it within %.2f mm of the "
                 "centre (reduce front_overlap_mm)" % (fw / 2, screw_clear))
    if open_z0 + fh > H + 1e-9:
        p.append("drawer front top (z=%.2f) rises above the panel (panel_height_mm %g) into the rack space above; "
                 "reduce front_overlap_mm" % (open_z0 + fh, H))
    pull_z = fh / 2
    if pull_w > 0:
        if pull_h > pull_w + 1e-9:
            p.append("pull_height_mm (%g) must not exceed pull_width_mm (%g)" % (pull_h, pull_w))
        if pull_w / 2 > iw / 2 - w + 1e-9:
            p.append("finger pull (%g mm) is wider than the drawer interior minus min_wall_mm (%.2f)" % (pull_w, iw - 2 * w))
        if pull_z - pull_h / 2 < td + w - 1e-9 or pull_z + pull_h / 2 > fh - w + 1e-9:
            p.append("finger pull (%g mm tall, centred at z=%.2f) leaves less than min_wall_mm above the drawer "
                     "floor or below the top of the %.2f mm drawer front" % (pull_h, pull_z, fh))
    dividers = []
    if k > 0:
        comp = (iw - k * td) / (k + 1)
        if comp < 2 * w - 1e-9:
            p.append("%d dividers leave %.2f mm wide compartments; need >= 2 x min_wall_mm" % (k, comp))
        dividers = [-iw / 2 + (i + 1) * comp + i * td + td / 2 for i in range(k)]
        for i, x in enumerate(dividers):
            if pull_w > 0 and abs(x) - td / 2 < pull_w / 2 - 1e-9:
                p.append("divider %d (x=%.2f) stands behind the finger pull and would block it; change "
                         "divider_count or pull_width_mm" % (i, x))

    # printing: housing like the shelf; drawer base down
    Z = c["build_plate_z_mm"]
    orients = []
    if _fits_plate(W, H, c) and t + D <= Z + 1e-9:
        orients.append("front_face_down")
    if _fits_plate(W, t + D, c) and H <= Z + 1e-9:
        orients.append("floor_down")
    if not orients:
        p.append("housing (%g wide x %g deep x %g tall) fits the %g x %g x %g mm build volume in neither "
                 "front-face-down nor floor-down orientation; no split is produced"
                 % (W, t + D, H, c["build_plate_x_mm"], c["build_plate_y_mm"], Z))
    if not (_fits_plate(fw, tfr + bl, c) and fh <= Z + 1e-9):
        p.append("drawer (%.2f wide x %.2f deep x %.2f tall) does not fit the %g x %g x %g mm build volume base-down"
                 % (fw, tfr + bl, fh, c["build_plate_x_mm"], c["build_plate_y_mm"], Z))
    if p:
        raise ConfigError(p)

    derived = {
        "front_plate_mm": [W, H],
        "rail_hole_x_mm": [-S / 2, S / 2],
        "modelled_rail_hole_diameter_mm": rd,
        "housing_depth_mm": t + D,
        "opening_mm": [2 * open_half, open_z1 - open_z0],
        "drawer_box_mm": [bw, bl, bh],
        "drawer_front_mm": [fw, fh],
        "drawer_interior_mm": [iw, il, ih],
        "compartment_width_mm": (iw - k * td) / (k + 1),
        "housing_printable_orientations": orients,
        "build_volume_mm": [c["build_plate_x_mm"], c["build_plate_y_mm"], Z],
    }
    physical = {
        "status": PHYSICAL_USE,
        "required_before_use": [
            "Fit coupon: first print a blanking_panel with identical rail_* and panel_* values (stiffener_depth_mm 0) "
            "and confirm it screws to your rails without forcing.",
            "Print the housing and the drawer from the same file (only component and name changed) and check the "
            "drawer slides with slide_clearance_mm on your printer before installing.",
            "The housing hangs only from the front rail screws (cantilever) and the drawer has no pull-out stop. "
            "No load rating exists; keep it light and do not rest anything on the housing roof.",
        ],
    }
    if c["component"] == "housing":
        orient = "UNVALIDATED suggestion; fitting housing orientations and their overhangs: "
        if "floor_down" in orients:
            orient += ("floor down: the sleeve roof is a %.1f mm horizontal bridge over the drawer space and needs "
                       "supports (or long-span bridging); the rail holes are horizontal round holes. " % (2 * open_half))
        if "front_face_down" in orients:
            orient += ("front face down (-y to the bed): the back wall becomes a %.1f x %.1f mm horizontal bridge "
                       "over the sleeve and needs supports. " % (2 * open_half, open_z1 - open_z0))
        orient += "Check your slicer's overhang preview. Not tested on any physical printer."
        return {
            "module": "drawer_housing",
            "args": {"w": W, "h": H, "t": t, "hole_x": S / 2, "hole_zs": list(c["rail_hole_z_offsets_mm"]),
                     "rail_d": rd, "slot_len": slot, "half": half, "depth": D, "sleeve_h": sh,
                     "t_floor": th, "t_wall": th, "t_roof": th, "t_back": th, "fn": c["facets"]},
            "derived": derived,
            "expected_bbox": {"min": [-W / 2, 0.0, 0.0], "max": [W / 2, t + D, H]},
            "print_orientation_suggestion": orient,
            "physical_use": physical,
        }
    return {
        "module": "drawer_box",
        "args": {"box_w": bw, "box_l": bl, "box_h": bh, "t_floor": td, "t_wall": td, "t_front": tfr,
                 "front_w": fw, "front_h": fh, "pull_w": pull_w, "pull_h": pull_h, "pull_z": pull_z,
                 "dividers": dividers, "fn": c["facets"]},
        "derived": derived,
        "expected_bbox": {"min": [-fw / 2, 0.0, 0.0], "max": [fw / 2, tfr + bl, fh]},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print the drawer base down as modelled; the top of the finger pull is a short "
            "%.1f mm bridge. Not tested on any physical printer." % pull_w if pull_w > 0 else
            "UNVALIDATED suggestion: print the drawer base down as modelled. Not tested on any physical printer."),
        "physical_use": physical,
    }


def derive_label(c):
    """Slide-in label holder. Frame: x centred, y = 0 at the back face, z = 0 at the bottom edge."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    cw, chh, ct = c["card_width_mm"], c["card_height_mm"], c["card_thickness_mm"]
    clr = c["card_clearance_mm"]
    lip = c["lip_overlap_mm"]
    b = c["frame_border_mm"]
    tb, tf = c["back_thickness_mm"], c["front_thickness_mm"]
    ear = c["ear_width_mm"]
    hd = hole_d(c["mount_screw_clearance_diameter_mm"], fn)
    head = c["mount_screw_head_diameter_mm"]

    pw, ph, pt = cw + 2 * clr, chh + clr, ct + clr
    body_w = pw + 2 * b
    outer_w = body_w + 2 * ear
    outer_h = ph + b
    win_w = cw - 2 * lip
    if lip - clr < w - 1e-9:
        p.append("missing retention: the lips cover the card edge by %.2f mm once it shifts by card_clearance_mm "
                 "(lip_overlap_mm - card_clearance_mm); need >= min_wall_mm %g" % (lip - clr, w))
    if win_w < w - 1e-9 or chh - lip < w - 1e-9:
        p.append("lip_overlap_mm %g leaves no readable window on a %g x %g mm card" % (lip, cw, chh))
    if b < w - 1e-9 or tb < w - 1e-9 or tf < w - 1e-9:
        p.append("frame_border_mm, back_thickness_mm and front_thickness_mm must be at least min_wall_mm %g" % w)
    holes = []
    if ear > 0:
        if head < c["mount_screw_clearance_diameter_mm"]:
            p.append("mount_screw_head_diameter_mm must be at least mount_screw_clearance_diameter_mm")
        if head > ear + 1e-9 or head > outer_h + 1e-9:
            p.append("unsafe screw clearance: a %g mm screw head does not bear fully on the %g x %.2f mm ear"
                     % (head, ear, outer_h))
        if ear / 2 - hd / 2 < w - 1e-9 or outer_h / 2 - hd / 2 < w - 1e-9:
            p.append("mount screw hole (%.2f mm) leaves less than min_wall_mm in the ear" % hd)
        holes = [-(body_w / 2 + ear / 2), body_w / 2 + ear / 2]
    if outer_w > c["max_overall_width_mm"] + 1e-9:
        p.append("label holder width %.2f exceeds max_overall_width_mm %g" % (outer_w, c["max_overall_width_mm"]))
    if outer_h > c["max_overall_height_mm"] + 1e-9:
        p.append("label holder height %.2f exceeds max_overall_height_mm %g" % (outer_h, c["max_overall_height_mm"]))
    if not _fits_plate(outer_w, outer_h, c):
        p.append("label holder %.2f x %.2f mm does not fit the %g x %g mm build plate back-down"
                 % (outer_w, outer_h, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    if p:
        raise ConfigError(p)
    depth = tb + pt + tf
    return {
        "module": "label_holder",
        "args": {"outer_w": outer_w, "body_w": body_w, "outer_h": outer_h, "t_back": tb, "t_front": tf,
                 "pocket_w": pw, "pocket_h": ph, "pocket_t": pt, "bottom_wall": b, "win_w": win_w, "lip": lip,
                 "holes_x": holes, "hole_d": hd, "fn": fn},
        "derived": {
            "outer_mm": [outer_w, depth, outer_h],
            "card_pocket_mm": [pw, pt, ph],
            "window_mm": [win_w, chh - lip],
            "retention_overlap_mm": lip - clr,
            "mount_hole_x_mm": holes,
            "modelled_mount_hole_diameter_mm": hd if holes else None,
        },
        "expected_bbox": {"min": [-outer_w / 2, 0.0, 0.0], "max": [outer_w / 2, depth, outer_h]},
        "preview": {"azimuth_deg": 145.0},   # the reader's side is +y
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print back face down (y = 0 on the bed). The front lips overhang the card "
            "pocket by %.1f mm from the frame; most slicers print that without supports, check the preview. "
            "Not tested on any physical printer." % (clr + lip)),
    }


def derive_comb(c):
    """Cable comb. Frame: x along the comb (centred), y = 0 at the back face, z = 0 at the bottom edge."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    cd = c["cable_diameter_mm"]
    sd = cd + c["cable_clearance_mm"]
    sd_m = hole_d(sd, fn)                    # modelled so the polygon still clears sd
    n = c["slot_count"]
    pitch = c["slot_pitch_mm"]
    throat = c["entry_throat_mm"]
    tip = c["finger_length_mm"]
    bh = c["base_height_mm"]
    t = c["comb_thickness_mm"]
    em = c["end_margin_mm"]
    spacing = c["mount_hole_spacing_mm"]
    hd = hole_d(c["mount_screw_clearance_diameter_mm"], fn)
    head = c["mount_screw_head_diameter_mm"]

    if throat > sd + 1e-9:
        p.append("entry_throat_mm %g is wider than the seat (cable_diameter_mm + cable_clearance_mm = %g)" % (throat, sd))
    if n > 1 and pitch - sd_m < w - 1e-9:
        p.append("seats %.2f mm wide at %g mm pitch leave %.2f mm fingers; need >= min_wall_mm %g"
                 % (sd_m, pitch, pitch - sd_m, w))
    if em < w - 1e-9:
        p.append("end_margin_mm %g is less than min_wall_mm %g" % (em, w))
    if bh < w - 1e-9:
        p.append("base_height_mm %g is less than min_wall_mm %g" % (bh, w))
    xs = [(k - (n - 1) / 2) * pitch for k in range(n)]
    L = 2 * (xs[-1] + sd_m / 2 + em)
    seat_z = bh + sd_m / 2
    top = bh + sd_m + tip
    holes = []
    if spacing > 0:
        if head < c["mount_screw_clearance_diameter_mm"]:
            p.append("mount_screw_head_diameter_mm must be at least mount_screw_clearance_diameter_mm")
        if head > bh + 1e-9:
            p.append("unsafe screw clearance: a %g mm screw head does not bear fully on the %g mm base bar"
                     % (head, bh))
        if bh / 2 - hd / 2 < w - 1e-9:
            p.append("mount screw hole (%.2f mm) leaves less than min_wall_mm in the %g mm base bar" % (hd, bh))
        holes = [-spacing / 2, spacing / 2]
        if spacing / 2 + max(head / 2, hd / 2 + w) > L / 2 + 1e-9:
            p.append("mount holes %g mm apart do not fit on the %.2f mm comb with min_wall_mm to its ends; add "
                     "end_margin_mm" % (spacing, L))
        for hx in holes:
            for i, x in enumerate(xs):
                if math.hypot(hx - x, bh / 2 - seat_z) < hd / 2 + sd_m / 2 + w - 1e-9:
                    p.append("mount hole at x=%g and cable seat %d are closer than min_wall_mm" % (hx, i))
    if L > c["max_overall_width_mm"] + 1e-9:
        p.append("comb length %.2f exceeds max_overall_width_mm %g" % (L, c["max_overall_width_mm"]))
    if top > c["max_overall_height_mm"] + 1e-9:
        p.append("comb height %.2f exceeds max_overall_height_mm %g" % (top, c["max_overall_height_mm"]))
    if not _fits_plate(L, top, c):
        p.append("comb %.2f x %.2f mm does not fit the %g x %g mm build plate back-down"
                 % (L, top, c["build_plate_x_mm"], c["build_plate_y_mm"]))
    if p:
        raise ConfigError(p)
    squeeze = max(0.0, cd - throat)
    return {
        "module": "cable_comb",
        "args": {"length": L, "t": t, "base_h": bh, "height": top, "slots_x": xs, "slot_d": sd_m, "slot_z": seat_z,
                 "throat": throat, "holes_x": holes, "hole_d": hd, "fn": fn},
        "derived": {
            "comb_mm": [L, t, top],
            "seat_x_mm": xs,
            "modelled_seat_diameter_mm": sd_m,
            "seat_centre_z_mm": seat_z,
            "finger_width_mm": pitch - sd_m if n > 1 else None,
            "snap_in_squeeze_mm": squeeze,
            "mount_hole_x_mm": holes,
        },
        "expected_bbox": {"min": [-L / 2, 0.0, 0.0], "max": [L / 2, t, top]},
        "preview": {"azimuth_deg": 145.0},   # y = 0 is the mounting face
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print back face down (y = 0 on the bed); every seat, throat and hole then runs "
            "vertically and there are no overhangs. %sNot tested on any physical printer."
            % ("Cables are pressed %.2f mm past the throat; the jacket deforms and the fingers flex. " % squeeze
               if squeeze > 0 else "")),
    }


def derive_cartridge(c):
    """Grid holder for upright items. Frame: x centred, y = 0 at the front edge, z = 0 at the bottom."""
    p = []
    fn = c["facets"]
    w = c["min_wall_mm"]
    clr = c["item_clearance_mm"]
    pw = c["item_width_mm"] + 2 * clr
    pd = c["item_thickness_mm"] + 2 * clr
    ih = c["item_height_mm"]
    cols, rows = c["columns"], c["rows"]
    depth = c["pocket_depth_mm"]
    tf, tw, tdv = c["floor_thickness_mm"], c["wall_thickness_mm"], c["divider_thickness_mm"]
    fh_d = c["floor_hole_diameter_mm"]
    nw, nd = c["finger_notch_width_mm"], c["finger_notch_depth_mm"]

    if depth > ih - 1e-9:
        p.append("buried item: pocket_depth_mm %g is not less than item_height_mm %g, nothing would stand proud to "
                 "grip" % (depth, ih))
    if tf < w - 1e-9 or tw < w - 1e-9 or tdv < w - 1e-9:
        p.append("floor_thickness_mm, wall_thickness_mm and divider_thickness_mm must be at least min_wall_mm %g" % w)
    fh_m = hole_d(fh_d, fn) if fh_d > 0 else 0.0
    if fh_d > 0 and fh_m + 2 * w > min(pw, pd) + 1e-9:
        p.append("floor_hole_diameter_mm %g leaves less than min_wall_mm of floor ledge in a %.2f x %.2f mm pocket"
                 % (fh_d, pw, pd))
    if nw > 0:
        if nw > pw - 2 * w + 1e-9:
            p.append("finger_notch_width_mm %g leaves less than min_wall_mm of the dividers beside the %.2f mm pocket"
                     % (nw, pw))
        if nd <= 0 or nd > depth + 1e-9:
            p.append("finger_notch_depth_mm %g must be > 0 and at most pocket_depth_mm %g" % (nd, depth))
    ow = cols * pw + (cols - 1) * tdv + 2 * tw
    od = rows * pd + (rows - 1) * tdv + 2 * tw
    H = tf + depth
    pockets = [[-ow / 2 + tw + i * (pw + tdv) + pw / 2, tw + j * (pd + tdv) + pd / 2]
               for j in range(rows) for i in range(cols)]
    # finger notches [x0, x1, y0, y1, z0]: through the wall in front of each pocket (front wall for row 0,
    # the divider for the others), reaching 0.5 mm into the open pocket(s) on either side
    notches = []
    if nw > 0:
        for j in range(rows):
            for i in range(cols):
                x, y = pockets[j * cols + i]
                notches.append([x - nw / 2, x + nw / 2, -1.0 if j == 0 else y - pd / 2 - tdv - 0.5,
                                y - pd / 2 + 0.5, H - nd])
    if ow > c["max_overall_width_mm"] + 1e-9:
        p.append("holder width %.2f exceeds max_overall_width_mm %g" % (ow, c["max_overall_width_mm"]))
    if od > c["max_overall_depth_mm"] + 1e-9:
        p.append("holder depth %.2f exceeds max_overall_depth_mm %g" % (od, c["max_overall_depth_mm"]))
    loaded = tf + ih
    if loaded > c["max_overall_height_mm"] + 1e-9:
        p.append("holder with items standing in it is %.2f mm tall; max_overall_height_mm is %g"
                 % (loaded, c["max_overall_height_mm"]))
    if not (_fits_plate(ow, od, c) and H <= c["build_plate_z_mm"] + 1e-9):
        p.append("holder %.2f x %.2f x %.2f mm does not fit the %g x %g x %g mm build volume base-down"
                 % (ow, od, H, c["build_plate_x_mm"], c["build_plate_y_mm"], c["build_plate_z_mm"]))
    if p:
        raise ConfigError(p)
    return {
        "module": "cartridge_holder",
        "args": {"outer_w": ow, "outer_d": od, "height": H, "t_floor": tf, "pocket_w": pw, "pocket_d": pd,
                 "pockets": pockets, "floor_hole_d": fh_m, "notches": notches, "fn": fn},
        "derived": {
            "outer_mm": [ow, od, H],
            "pockets": cols * rows,
            "pocket_mm": [pw, pd, depth],
            "item_exposed_mm": ih - depth,
            "height_loaded_mm": loaded,
            "modelled_floor_hole_diameter_mm": fh_m if fh_d > 0 else None,
        },
        "expected_bbox": {"min": [-ow / 2, 0.0, 0.0], "max": [ow / 2, od, H]},
        "print_orientation_suggestion": (
            "UNVALIDATED suggestion: print base down as modelled; pockets, notches and push-out holes are all "
            "vertical, so there are no overhangs. Not tested on any physical printer."),
    }


DERIVE = {"rack_bracket": derive_bracket, "sff_cradle": derive_cradle, "switch_cradle": derive_switch,
          "blanking_panel": derive_panel, "cable_panel": derive_panel,
          "rack_shelf": derive_shelf, "pdu_mount": derive_pdu,
          "keystone_panel": derive_keystone, "rack_drawer": derive_drawer, "label_holder": derive_label,
          "cable_comb": derive_comb, "cartridge_holder": derive_cartridge}


def validate(data):
    c = check_fields(data)
    return c, DERIVE[c["part"]](c)
