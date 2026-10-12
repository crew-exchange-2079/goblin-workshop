"""Parameter specification: the single source for validation and the JSON schema.

Every field is required; there are no silent defaults. ``kind`` tells a human
or an agent filling the file where the value must come from:

  measured   -- measure it on the actual rack / device with calipers
  chosen     -- a design choice (thickness, flange size, facet count)
  tolerance  -- a clearance you choose and tune for your printer/material
  label      -- metadata; does not change geometry
"""

SCHEMA_VERSION = 1

# (name, type, min, max, kind, description)
# type: "number" | "integer" | "bool" | "enum:<a>|<b>" | "name"
#       | "z_list" | "y_list" | "yz_list" | "xy_list" | "xz_list" | "rect_list"
# rect_list entries are [a0, a1, b0, b1] rectangles (a0 < a1, b0 < b1; checked in validate.py).
# For "number", min is exclusive when written as ("gt", value).

COMMON = [
    ("schema_version", "const:1", None, None, "label", "Must be 1."),
    ("name", "name", None, None, "label",
     "Output basename: 1-48 chars of a-z, 0-9 and '-', starting with a letter or digit."),
    ("units", "const:mm", None, None, "label", "All lengths are millimetres. Must be \"mm\"."),
    ("facets", "integer", 24, 128, "chosen",
     "Polygon segments per circle ($fn). Holes are enlarged so the polygon still clears the stated diameter."),
    ("min_wall_mm", "number", ("gt", 0.4), 20, "chosen",
     "Minimum material left between any hole/counterbore and a part edge or another hole."),
]

BRACKET = [
    ("part", "const:rack_bracket", None, None, "label", "Selects the L-shaped rack ear bracket."),
    ("rack_width_class", "enum:10in|19in", None, None, "label",
     "Only selects a sanity window for rail_hole_center_spacing_mm (catches inch/mm mix-ups). "
     "It does not supply any dimension."),
    ("hand", "enum:right|left", None, None, "chosen",
     "Which side of the device, viewed from the front. 'left' is the X-mirror of 'right'."),
    # rack measurements
    ("rail_hole_center_spacing_mm", "number", 150, 520, "measured",
     "Horizontal centre-to-centre distance between the left and right rail mounting holes."),
    ("rail_opening_width_mm", "number", 100, 500, "measured",
     "Clear horizontal opening between the inner edges of the left and right rails (narrowest point along the depth the bracket occupies)."),
    ("max_overall_width_mm", "number", 100, 520, "measured",
     "Widest the pair of front flanges may be (e.g. rack panel width or nearest obstruction)."),
    ("rail_hole_z_offsets_mm", "z_list", 0, 400, "measured",
     "Rail hole centres used, measured upward from the bottom edge of the bracket as it will sit in the rack. 1-8 values."),
    ("rail_screw_clearance_diameter_mm", "number", ("gt", 1.0), 12, "tolerance",
     "Through-hole diameter for the rack screw (screw major diameter + your clearance)."),
    ("rail_screw_head_diameter_mm", "number", ("gt", 1.0), 25, "measured",
     "Rack screw (or washer) head diameter; the head must bear fully on the front flange."),
    ("rail_slot_extra_length_mm", "number", 0, 15, "tolerance",
     "Horizontal slot elongation of each rail hole for side-to-side adjustment (0 = round hole)."),
    ("rail_side_clearance_mm", "number", 0, 10, "tolerance",
     "Gap required between the bracket/device-screw heads and the inner rail edge."),
    # device measurements
    ("device_width_mm", "number", 20, 500, "measured", "Device width across the faces the brackets attach to."),
    ("device_side_gap_mm", "number", 0, 5, "tolerance", "Gap between the device side and the bracket side flange."),
    ("device_setback_mm", "number", -100, 400, "chosen",
     "Distance from the bracket front face back to the device front face (negative = device protrudes forward)."),
    ("device_bottom_offset_mm", "number", -100, 400, "chosen",
     "Height of the device bottom above the bracket bottom edge."),
    ("device_screw_positions_mm", "yz_list", -100, 600, "measured",
     "Device side screw holes as [y, z]: y back from the device front face, z up from the device bottom. 1-8 pairs."),
    ("device_screw_clearance_diameter_mm", "number", ("gt", 1.0), 10, "tolerance",
     "Through-hole diameter in the side flange for the device screw."),
    ("device_screw_head_diameter_mm", "number", ("gt", 1.0), 20, "measured", "Device screw head diameter."),
    ("device_screw_head_height_mm", "number", 0, 10, "measured",
     "Device screw head height; it sits on the outside of the side flange, next to the rail."),
    # chosen geometry
    ("thickness_mm", "number", ("gt", 1.0), 12, "chosen", "Material thickness of both flanges."),
    ("flange_height_mm", "number", 10, 400, "chosen",
     "Height of the bracket (both flanges). For 19-inch gear this is usually a little under n x 44.45 mm."),
    ("side_flange_depth_mm", "number", 10, 400, "chosen", "How far the side flange extends back from the front face."),
    ("gusset_mm", "number", 0, 40, "chosen", "Leg length of the triangular corner gusset (0 = none)."),
]

CRADLE = [
    ("part", "const:sff_cradle", None, None, "label", "Selects the U-shaped small-form-factor PC cradle."),
    ("device_orientation", "enum:flat|on_side", None, None, "chosen",
     "'flat': device sits on its bottom. 'on_side': device stands on a side, so its height becomes the cradle width."),
    ("device_width_mm", "number", 20, 500, "measured", "Device width in its normal (flat) pose."),
    ("device_height_mm", "number", 10, 500, "measured", "Device height in its normal (flat) pose."),
    ("device_depth_mm", "number", 20, 600, "measured", "Device depth front to back."),
    ("fit_clearance_mm", "number", 0, 10, "tolerance",
     "Gap added on each side of the device inside the cradle (and above it, under the lip)."),
    ("cradle_length_mm", "number", 20, 600, "chosen", "Cradle length along the device depth."),
    ("base_thickness_mm", "number", ("gt", 1.0), 20, "chosen", "Base plate thickness."),
    ("wall_thickness_mm", "number", ("gt", 1.0), 20, "chosen", "Side wall and lip thickness."),
    ("wall_clear_height_mm", "number", 2, 500, "chosen",
     "Clear height inside the cradle from the base top to the wall top (or to the underside of the lip)."),
    ("retention_lip_mm", "number", 0, 60, "chosen", "Inward lip at the top of each wall (0 = none)."),
    ("shelf_hole_positions_mm", "xy_list", -300, 600, "measured",
     "Mounting holes as [x, y]: x from the cradle centreline, y back from the cradle front edge. "
     "Must match holes you measured on your shelf or panel. 1-12 pairs."),
    ("shelf_screw_clearance_diameter_mm", "number", ("gt", 1.0), 12, "tolerance", "Through-hole diameter for the shelf screw."),
    ("shelf_screw_head_diameter_mm", "number", ("gt", 1.0), 25, "measured", "Shelf screw head diameter (counterbored from the top)."),
    ("shelf_screw_head_height_mm", "number", 0, 15, "measured",
     "Shelf screw head height; the counterbore is this deep so the head sits flush or below the base top."),
    ("max_overall_width_mm", "number", 20, 600, "measured", "Available width on the shelf."),
    ("max_overall_depth_mm", "number", 20, 700, "measured", "Available depth on the shelf."),
    ("max_overall_height_mm", "number", 5, 600, "measured", "Available height above the shelf."),
]

# Shared by blanking_panel and cable_panel. Panel frame: x centred on the rack
# centreline, y = 0 at the panel front face (+y into the rack), z = 0 at the
# panel bottom edge.
PANEL_COMMON = [
    ("rack_width_class", "enum:10in|19in", None, None, "label",
     "Only selects a sanity window for rail_hole_center_spacing_mm (catches inch/mm mix-ups). "
     "It does not supply any dimension; there is no universal 10-inch hole pattern."),
    ("rail_hole_center_spacing_mm", "number", 150, 520, "measured",
     "Horizontal centre-to-centre distance between the left and right rail mounting holes."),
    ("rail_opening_width_mm", "number", 100, 500, "measured",
     "Clear horizontal opening between the inner edges of the rails over the depth the panel's stiffeners occupy."),
    ("panel_width_mm", "number", 120, 520, "measured",
     "Overall panel width: the space available in front of the rails (19-inch nominal 482.6; 10-inch: measure)."),
    ("panel_height_mm", "number", 10, 400, "chosen",
     "Panel height. Usually a little under n x 44.45 mm so neighbouring panels do not touch."),
    ("panel_thickness_mm", "number", ("gt", 1.0), 10, "chosen", "Front plate thickness."),
    ("rail_hole_z_offsets_mm", "z_list", 0, 400, "measured",
     "Rail hole centres used, measured upward from the panel bottom edge as it will sit in the rack. "
     "The same heights are used on the left and right rail. 1-8 values."),
    ("rail_screw_clearance_diameter_mm", "number", ("gt", 1.0), 12, "tolerance",
     "Through-hole diameter for the rack screw (screw major diameter + your clearance)."),
    ("rail_screw_head_diameter_mm", "number", ("gt", 1.0), 25, "measured",
     "Rack screw (or washer) head diameter; the head must bear fully on the panel."),
    ("rail_slot_extra_length_mm", "number", 0, 15, "tolerance",
     "Horizontal slot elongation of each rail hole for side-to-side adjustment (0 = round hole)."),
    ("rail_side_clearance_mm", "number", 0, 10, "tolerance",
     "Gap kept between the stiffening ribs behind the panel and the inner rail edge."),
    ("stiffener_depth_mm", "number", 0, 40, "chosen",
     "Depth of the two horizontal stiffening ribs along the top and bottom edges of the back face (0 = flat plate). "
     "Ribs stop inside the rail opening."),
    ("stiffener_thickness_mm", "number", ("gt", 1.0), 10, "chosen",
     "Vertical thickness of each stiffening rib (ignored when stiffener_depth_mm is 0, but still required)."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
    ("allow_oversize_print", "bool", None, None, "chosen",
     "false: refuse a panel whose face does not fit the build plate in either rotation. true: generate it anyway "
     "and flag it in the manifest; no split/joint is produced, so you need a larger printer or another process."),
]

BLANKING = [
    ("part", "const:blanking_panel", None, None, "label",
     "Selects a flat blanking panel that screws to measured rail holes."),
] + PANEL_COMMON

CABLE = [
    ("part", "const:cable_panel", None, None, "label",
     "Selects a rack panel with rounded (stadium-shaped, edge-filleted) cable pass-throughs."),
] + PANEL_COMMON + [
    ("pass_through_centers_mm", "xz_list", -260, 400, "chosen",
     "Pass-through centres as [x, z]: x from the rack centreline (+x to the right, viewed from the front), "
     "z up from the panel bottom edge. 1-8 pairs."),
    ("pass_through_width_mm", "number", 5, 400, "chosen",
     "Narrowest (mid-thickness) width of each horizontal stadium opening, end to end."),
    ("pass_through_height_mm", "number", 5, 200, "chosen",
     "Narrowest (mid-thickness) height of each opening; also the diameter of its round ends. Must not exceed the width."),
    ("pass_through_edge_radius_mm", "number", ("gt", 0.4), 10, "chosen",
     "Radius of the rounded edge on both faces around each opening; at most half the panel thickness. "
     "The opening on each face is wider and taller than the neck by twice this radius."),
    ("cable_bundle_diameter_mm", "number", 1, 150, "measured",
     "Largest connector or cable bundle diameter that must pass through; the opening neck must be at least this tall."),
]

# Switch cradle frame: x centred, y = 0 at the cradle front edge, z = 0 at the base bottom.
# Device-face rectangles are [x0, x1, z0, z1]: x from the device centreline (viewed from the
# front, +x to the right, same for the rear face), z up from the device bottom.
SWITCH = [
    ("part", "const:switch_cradle", None, None, "label",
     "Selects a drop-in cradle for a small network switch: front/rear stop tabs, strap slots, vent windows, "
     "base vent slots and counterbored shelf holes."),
    ("device_width_mm", "number", 30, 300, "measured", "Switch width (left to right, viewed from the front)."),
    ("device_height_mm", "number", 10, 100, "measured", "Switch height, including feet if they stay on."),
    ("device_depth_mm", "number", 30, 300, "measured", "Switch depth, front face to rear face (excluding plugs)."),
    ("fit_clearance_mm", "number", 0, 5, "tolerance",
     "Gap added on every side of the switch inside the cradle (left/right, front/back, and above it under the strap slot)."),
    ("base_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Base plate thickness."),
    ("wall_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Side wall thickness; also the front/rear stop tab thickness."),
    ("stop_tab_width_mm", "number", 1, 60, "chosen",
     "How far each front/rear stop tab reaches inward from its side wall. Must overlap the device face by at least "
     "min_wall_mm beyond fit_clearance_mm."),
    ("stop_tab_height_mm", "number", 1, 60, "chosen", "Height of the stop tabs above the base top."),
    ("front_port_zone_mm", "rect_list", -200, 200, "measured",
     "Front-face regions that must stay unobstructed (ports, LEDs, buttons) as [x0, x1, z0, z1]. 0-4 rectangles."),
    ("rear_port_zone_mm", "rect_list", -200, 200, "measured",
     "Rear-face regions that must stay unobstructed (power jack, ports) as [x0, x1, z0, z1]. 0-4 rectangles."),
    ("side_vent_zone_mm", "rect_list", -10, 310, "measured",
     "Side vent regions as [y0, y1, z0, z1]: y back from the device front face, z up from the device bottom. "
     "A window of exactly this size is cut through both side walls. 0-4 rectangles."),
    ("strap_slot_y_mm", "y_list", 0, 300, "chosen",
     "Centre of each strap slot pair, measured back from the device front face. The strap crosses the top of "
     "the switch and wraps around the bar above each slot. 1-4 values."),
    ("strap_slot_length_mm", "number", 3, 60, "tolerance", "Slot length along the depth: strap width + your clearance."),
    ("strap_slot_height_mm", "number", 1, 10, "tolerance", "Slot height: strap thickness + your clearance."),
    ("strap_bar_height_mm", "number", ("gt", 1.0), 20, "chosen",
     "Height of the wall material above each strap slot that the strap wraps around."),
    ("base_vent_slot_count", "integer", 0, 40, "chosen", "Number of rounded vent slots in the base under the switch (0 = none)."),
    ("base_vent_slot_width_mm", "number", ("gt", 1.0), 20, "chosen", "Width of each base vent slot."),
    ("base_vent_slot_length_mm", "number", 2, 300, "chosen",
     "Length of each base vent slot (along the depth, centred on the cradle)."),
    ("base_vent_slot_pitch_mm", "number", 2, 60, "chosen", "Centre-to-centre spacing of base vent slots."),
    ("shelf_hole_positions_mm", "xy_list", -300, 600, "measured",
     "Mounting holes as [x, y]: x from the cradle centreline, y back from the cradle front edge. "
     "Must match holes you measured on your shelf or panel. 1-12 pairs."),
    ("shelf_screw_clearance_diameter_mm", "number", ("gt", 1.0), 12, "tolerance", "Through-hole diameter for the shelf screw."),
    ("shelf_screw_head_diameter_mm", "number", ("gt", 1.0), 25, "measured", "Shelf screw head diameter (counterbored from the top)."),
    ("shelf_screw_head_height_mm", "number", 0, 15, "measured",
     "Shelf screw head height; the counterbore is this deep so the head sits flush or below the base top."),
    ("max_overall_width_mm", "number", 20, 600, "measured", "Available width on the shelf."),
    ("max_overall_depth_mm", "number", 20, 700, "measured", "Available depth on the shelf."),
    ("max_overall_height_mm", "number", 5, 600, "measured", "Available height above the shelf."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
]

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}(?![\s\S])"

PARTS = {"rack_bracket": COMMON + BRACKET, "sff_cradle": COMMON + CRADLE,
         "switch_cradle": COMMON + SWITCH, "blanking_panel": COMMON + BLANKING, "cable_panel": COMMON + CABLE}

# Sanity windows that only catch unit mistakes; NOT standards for 10-inch racks.
RAIL_SPACING_WINDOW = {"10in": (180.0, 270.0), "19in": (430.0, 500.0)}

LIST_LIMITS = {"z_list": (1, 8), "y_list": (1, 4), "yz_list": (1, 8), "xy_list": (1, 12), "xz_list": (1, 8),
               "rect_list": (0, 4)}
SCALAR_LISTS = ("z_list", "y_list")   # lists of numbers; the others are lists of fixed-size lists
TUPLE_SIZE = {"yz_list": 2, "xy_list": 2, "xz_list": 2, "rect_list": 4}


def _number_schema(lo, hi):
    s = {"type": "number"}
    if isinstance(lo, tuple):
        s["exclusiveMinimum"] = lo[1]
    elif lo is not None:
        s["minimum"] = lo
    if hi is not None:
        s["maximum"] = hi
    return s


def field_schema(ftype, lo, hi):
    if ftype.startswith("const:"):
        v = ftype[6:]
        return {"const": int(v) if v.isdigit() else v}
    if ftype.startswith("enum:"):
        return {"type": "string", "enum": ftype[5:].split("|")}
    if ftype == "name":
        # "(?![\s\S])" = true end of string in both ECMA-262 and Python re.search
        # ("$" would also match before a trailing newline in Python).
        return {"type": "string", "minLength": 1, "maxLength": 48, "pattern": NAME_PATTERN}
    if ftype == "bool":
        return {"type": "boolean"}
    if ftype == "integer":
        return {"type": "integer", "minimum": lo, "maximum": hi}
    if ftype == "number":
        return _number_schema(lo, hi)
    if ftype in LIST_LIMITS:
        n_lo, n_hi = LIST_LIMITS[ftype]
        item = _number_schema(lo, hi)
        if ftype not in SCALAR_LISTS:
            k = TUPLE_SIZE[ftype]
            item = {"type": "array", "items": item, "minItems": k, "maxItems": k}
        return {"type": "array", "items": item, "minItems": n_lo, "maxItems": n_hi}
    raise ValueError(ftype)


def json_schema():
    variants = []
    for part, fields in PARTS.items():
        props = {}
        for name, ftype, lo, hi, kind, desc in fields:
            p = field_schema(ftype, lo, hi)
            p["description"] = desc
            p["x-kind"] = kind
            props[name] = p
        variants.append({
            "title": part,
            "type": "object",
            "additionalProperties": False,
            "required": [f[0] for f in fields],
            "properties": props,
        })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/crew-exchange-2079/goblin-workshop/rack-parts/schema/rack-part.schema.json",
        "title": "rack-parts input",
        "description": (
            "Input for the rack-parts generator. All lengths in mm. x-kind says where a value must come from: "
            "'measured' (measure the real rack/device), 'chosen' (design choice), 'tolerance' (clearance you tune), "
            "'label'. Schema bounds are coarse; the generator also runs cross-field geometry checks that the schema "
            "cannot express."
        ),
        "oneOf": variants,
    }
