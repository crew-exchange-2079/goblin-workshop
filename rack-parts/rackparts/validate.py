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
                if ftype == "z_list":
                    _check_num("%s[%d]" % (name, i), item, lo, hi, problems)
                elif not isinstance(item, list) or len(item) != 2:
                    problems.append("%s[%d] must be a 2-element list" % (name, i))
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


def validate(data):
    c = check_fields(data)
    if c["part"] == "rack_bracket":
        return c, derive_bracket(c)
    return c, derive_cradle(c)
