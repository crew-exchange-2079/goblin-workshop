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
# type: "number" | "integer" | "enum:<a>|<b>" | "z_list" | "yz_list" | "xy_list" | "name"
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

PARTS = {"rack_bracket": COMMON + BRACKET, "sff_cradle": COMMON + CRADLE}

# Sanity windows that only catch unit mistakes; NOT standards for 10-inch racks.
RAIL_SPACING_WINDOW = {"10in": (180.0, 270.0), "19in": (430.0, 500.0)}

LIST_LIMITS = {"z_list": (1, 8), "yz_list": (1, 8), "xy_list": (1, 12)}


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
        return {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,47}$"}
    if ftype == "integer":
        return {"type": "integer", "minimum": lo, "maximum": hi}
    if ftype == "number":
        return _number_schema(lo, hi)
    if ftype in LIST_LIMITS:
        n_lo, n_hi = LIST_LIMITS[ftype]
        item = _number_schema(lo, hi)
        if ftype != "z_list":
            item = {"type": "array", "items": item, "minItems": 2, "maxItems": 2}
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
