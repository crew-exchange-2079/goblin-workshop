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
    rr = rd / 2
    slot = c["rail_slot_extra_length_mm"]
    head = c["rail_screw_head_diameter_mm"]
    side_clr = c["rail_side_clearance_mm"]
    rib_depth = c["stiffener_depth_mm"]
    rib_t = c["stiffener_thickness_mm"] if rib_depth > 0 else 0.0

    _rail_front_checks(c, W, H, p)
    rib_half = opening / 2 - side_clr
    shank_inner = S / 2 - slot / 2 - rr
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
        orient += ("front face down (-y to the bed): vent slots become horizontal slots with rounded overhanging "
                   "ends")
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


DERIVE = {"rack_bracket": derive_bracket, "sff_cradle": derive_cradle, "switch_cradle": derive_switch,
          "blanking_panel": derive_panel, "cable_panel": derive_panel,
          "rack_shelf": derive_shelf, "pdu_mount": derive_pdu}


def validate(data):
    c = check_fields(data)
    return c, DERIVE[c["part"]](c)
