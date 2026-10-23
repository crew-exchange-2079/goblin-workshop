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
#       | "z_list" | "y_list" | "x_list" | "yz_list" | "xy_list" | "xz_list" | "rect_list" | "keepout_list"
# rect_list / keepout_list entries are [a0, a1, b0, b1] rectangles (a0 < a1, b0 < b1; checked in validate.py).
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
     "Gap kept between anything behind the panel (stiffening ribs, shelf floor and walls) and the inner rail edge."),
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

# Rack shelf frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack),
# z = 0 at the bottom edge of the front plate (the floor's underside is flush with it).
SHELF = [
    ("part", "const:rack_shelf", None, None, "label",
     "Selects a shallow ventilated shelf: a front plate on measured rail holes with a cantilevered floor, "
     "side walls, optional rear lip and vent slots. Only 10-inch is generated; 19-inch is refused (deferred)."),
] + PANEL_COMMON[:11] + [
    ("rack_clear_depth_mm", "number", 20, 1000, "measured",
     "Free depth behind the panel front face (to rear rails, door, wall or cables) at the shelf's height."),
    ("shelf_depth_mm", "number", 40, 250, "chosen",
     "Floor depth behind the front plate. The shelf hangs only from the front rail screws; keep it shallow."),
    ("floor_thickness_mm", "number", ("gt", 1.0), 10, "chosen", "Floor thickness (at least min_wall_mm)."),
    ("side_wall_height_mm", "number", 3, 300, "chosen",
     "Height of the two side walls above the floor top. They are the shelf's main stiffeners."),
    ("side_wall_thickness_mm", "number", ("gt", 1.0), 10, "chosen", "Side wall (and rear lip) thickness."),
    ("rear_lip_height_mm", "number", 0, 300, "chosen",
     "Height of a lip along the rear floor edge above the floor top (0 = none). At most side_wall_height_mm."),
    ("vent_slot_count", "integer", 0, 40, "chosen", "Number of rounded vent slots through the floor (0 = none)."),
    ("vent_slot_width_mm", "number", ("gt", 1.0), 20, "chosen", "Width of each vent slot (across the rack)."),
    ("vent_slot_length_mm", "number", 2, 250, "chosen",
     "Length of each vent slot along the depth, centred on the floor between front plate and rear lip."),
    ("vent_slot_pitch_mm", "number", 2, 60, "chosen", "Centre-to-centre spacing of vent slots across the rack."),
    ("device_width_mm", "number", 10, 500, "measured", "Width of the device that will sit on the shelf."),
    ("device_depth_mm", "number", 10, 600, "measured", "Depth of that device, including anything sticking out at the back."),
    ("device_height_mm", "number", 5, 400, "measured", "Height of that device including feet."),
    ("fit_clearance_mm", "number", 0, 10, "tolerance",
     "Gap required between the device and each side wall, and between the device and the rear lip."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
    ("build_plate_z_mm", "number", 20, 1000, "measured", "Usable build height."),
]

# PDU mount frame (one bracket): x along the strip length, centred on the bracket; y across the strip,
# centred; z = 0 on the mounting surface. Strip-face rectangles use x measured from the strip's left
# end (viewed from the face opposite the base, i.e. the "top" face) and the second axis across that face.
PDU = [
    ("part", "const:pdu_mount", None, None, "label",
     "Selects an external strap-retained power-strip/PDU bracket. One bracket is generated; print one per "
     "entry in bracket_x_positions_mm. Non-electrical accessory: it never touches outlets, switches, vents or "
     "the cord exit, and needs no change to the strip's case."),
    ("device_length_mm", "number", 60, 700, "measured", "Strip body length (end to end, excluding cord and plugs)."),
    ("device_width_mm", "number", 15, 150, "measured", "Strip body width across the face that rests on the bracket base."),
    ("device_height_mm", "number", 10, 100, "measured", "Strip body height from the face on the base to the opposite (top) face."),
    ("fit_clearance_mm", "number", 0, 5, "tolerance",
     "Gap between the strip and each side wall, and between the strip top and the strap slot bottom."),
    ("top_face_keepouts_mm", "keepout_list", 0, 700, "measured",
     "Regions on the face opposite the base (usually outlets, switch, LEDs, including plug bodies) as "
     "[x0, x1, y0, y1]: x from the strip's left end, y across the face from its near (-y) edge. 0-12 rectangles."),
    ("side_face_keepouts_mm", "keepout_list", 0, 700, "measured",
     "Regions on either long side face (vents, switch, side outlets, side cord exit) as [x0, x1, z0, z1]: "
     "z up from the base face. Applied to both sides. 0-12 rectangles."),
    ("base_face_keepouts_mm", "keepout_list", 0, 700, "measured",
     "Regions on the face resting on the base (vents, labels you need to read, feet) as [x0, x1, y0, y1]. "
     "0-12 rectangles."),
    ("keepout_margin_mm", "number", 0, 30, "tolerance",
     "Extra distance kept along the strip between each bracket (and its strap) and every keepout region."),
    ("end_clearance_mm", "number", 0, 150, "measured",
     "Minimum distance from each bracket to both strip ends (cord exit, strain relief, end switches)."),
    ("bracket_x_positions_mm", "x_list", 0, 700, "chosen",
     "Bracket centres along the strip, from its left end. 2-4 values (one bracket would let the strip pivot)."),
    ("bracket_length_mm", "number", 8, 100, "chosen", "Bracket length along the strip."),
    ("base_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Base plate and ear thickness."),
    ("wall_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Side wall thickness."),
    ("strap_width_mm", "number", 3, 60, "measured", "Width of the strap you will use (hook-and-loop, tie)."),
    ("strap_thickness_mm", "number", ("gt", 0.2), 10, "measured", "Strap thickness."),
    ("strap_clearance_mm", "number", 0, 5, "tolerance", "Added to strap width and thickness to size the wall slots."),
    ("strap_bar_height_mm", "number", ("gt", 1.0), 20, "chosen",
     "Wall material above each strap slot that the strap wraps around."),
    ("ear_width_mm", "number", 4, 60, "chosen", "Width of the screw ear on each side of the walls."),
    ("mount_screw_clearance_diameter_mm", "number", ("gt", 1.0), 10, "tolerance",
     "Through-hole diameter for the mounting screw, one per ear."),
    ("mount_screw_head_diameter_mm", "number", ("gt", 1.0), 20, "measured",
     "Mounting screw (or washer) head diameter; it must bear on the ear clear of the wall."),
    ("max_overall_width_mm", "number", 20, 600, "measured", "Available width on the mounting surface across the strip."),
    ("max_overall_height_mm", "number", 10, 300, "measured",
     "Available height off the mounting surface (e.g. gap to a door or side panel), including the strap loop."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
    ("build_plate_z_mm", "number", 20, 1000, "measured", "Usable build height."),
]

# Keystone patch panel: the panel frame. Jacks sit in rows at the heights in keystone_row_z_mm, centred
# across the rack at keystone_pitch_mm. Around each cutout the panel is thinned from the back to the
# thickness the jack's latch grips.
KEYSTONE = [
    ("part", "const:keystone_panel", None, None, "label",
     "Selects a rack patch panel with rows of snap-in keystone jack cutouts, thinned from the back to the "
     "thickness the jack latch grips."),
] + PANEL_COMMON + [
    ("keystone_count", "integer", 1, 24, "chosen", "Jacks per row, centred across the rack."),
    ("keystone_pitch_mm", "number", 10, 60, "chosen", "Centre-to-centre spacing of jacks in a row."),
    ("keystone_row_z_mm", "z_list", 0, 400, "chosen",
     "Centre height of each row of jacks above the panel bottom edge. 1-8 values."),
    ("keystone_cutout_width_mm", "number", 8, 30, "measured",
     "Width of the opening the jack snaps into (from your jack's datasheet or a test print); across the rack."),
    ("keystone_cutout_height_mm", "number", 8, 30, "measured",
     "Height of that opening, in the direction the latch flexes."),
    ("jack_latch_panel_thickness_mm", "number", ("gt", 0.4), 6, "measured",
     "Panel thickness the jack's latch is made to grip. Where it is less than panel_thickness_mm the panel is "
     "pocketed from the back around each cutout to this thickness."),
    ("jack_body_width_mm", "number", 8, 40, "measured",
     "Width of the jack body behind the panel (including the latch), at least the cutout width."),
    ("jack_body_height_mm", "number", 8, 50, "measured",
     "Height of the jack body behind the panel (including the latch), at least the cutout height."),
    ("jack_clearance_mm", "number", 0, 3, "tolerance",
     "Gap added on every side of the jack body to size the back pocket."),
]

# Drawer: housing frame = rack shelf frame (x centred, y = 0 at the front face, z = 0 at the bottom edge of
# the front plate). Drawer frame: x centred, y = 0 at the drawer front face, z = 0 at the drawer bottom.
DRAWER = [
    ("part", "const:rack_drawer", None, None, "label",
     "Selects a rack drawer: a housing (front plate on measured rail holes with a closed sleeve behind it) and "
     "a drawer box that slides in. Only 10-inch is generated; 19-inch is refused (deferred)."),
    ("component", "enum:housing|drawer", None, None, "chosen",
     "Which of the two parts this run writes. Both are validated against each other on every run: keep the "
     "rest of the file identical for the housing and the drawer."),
] + PANEL_COMMON[:11] + [
    ("rack_clear_depth_mm", "number", 20, 1000, "measured",
     "Free depth behind the panel front face (to rear rails, door, wall or cables) at the drawer's height."),
    ("sleeve_depth_mm", "number", 40, 250, "chosen",
     "Depth of the housing sleeve behind the front plate, including its back wall. The housing hangs only "
     "from the front rail screws; keep it shallow."),
    ("sleeve_height_mm", "number", 15, 400, "chosen",
     "Outer height of the sleeve, from the front plate's bottom edge. At most panel_height_mm."),
    ("housing_wall_thickness_mm", "number", ("gt", 1.0), 10, "chosen",
     "Thickness of the sleeve floor, side walls, roof and back wall."),
    ("drawer_wall_thickness_mm", "number", ("gt", 1.0), 10, "chosen",
     "Thickness of the drawer floor, side and back walls and dividers."),
    ("drawer_front_thickness_mm", "number", ("gt", 1.0), 10, "chosen", "Thickness of the drawer front."),
    ("slide_clearance_mm", "number", ("gt", 0.1), 3, "tolerance",
     "Gap between drawer and sleeve on each side, above the drawer, and between the drawer back and the "
     "sleeve back wall."),
    ("front_overlap_mm", "number", 0, 30, "chosen",
     "How far the drawer front reaches past the opening to the sides and above it; it stops against the "
     "housing front plate (0 = flush front, the sleeve back wall is the only stop)."),
    ("pull_width_mm", "number", 0, 150, "chosen", "Width of the finger-pull slot through the drawer front (0 = none)."),
    ("pull_height_mm", "number", 4, 40, "chosen",
     "Height of the finger-pull slot (its round ends' diameter); ignored when pull_width_mm is 0, but still required."),
    ("divider_count", "integer", 0, 8, "chosen", "Dividers splitting the drawer into equal compartments across its width."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
    ("build_plate_z_mm", "number", 20, 1000, "measured", "Usable build height."),
]

# Label holder frame: x centred, y = 0 at the back (mounting) face, +y toward the reader, z = 0 at the
# bottom edge. The card slides in from the open top.
LABEL = [
    ("part", "const:label_holder", None, None, "label",
     "Selects a slide-in label holder: a back plate, a card pocket open at the top and a front frame whose lips "
     "hold the card's sides and bottom edge, with optional screw ears."),
    ("card_width_mm", "number", 10, 300, "measured", "Width of the label card or strip you will insert."),
    ("card_height_mm", "number", 5, 150, "measured", "Height of the card."),
    ("card_thickness_mm", "number", ("gt", 0.05), 5, "measured", "Thickness of the card (several sheets if laminated)."),
    ("card_clearance_mm", "number", 0, 2, "tolerance",
     "Gap added on each side of the card, above its thickness and below its bottom edge."),
    ("lip_overlap_mm", "number", 0.5, 20, "chosen",
     "How far the front frame covers the card's side and bottom edges. Must exceed card_clearance_mm by at least "
     "min_wall_mm so the card cannot fall out of the window."),
    ("frame_border_mm", "number", ("gt", 0.4), 20, "chosen",
     "Frame material outside the card pocket on the left, right and bottom (at least min_wall_mm)."),
    ("back_thickness_mm", "number", ("gt", 0.4), 10, "chosen", "Back plate (and ear) thickness."),
    ("front_thickness_mm", "number", ("gt", 0.4), 5, "chosen", "Front frame (lip) thickness."),
    ("ear_width_mm", "number", 0, 40, "chosen", "Width of a screw ear on each side (0 = no ears, e.g. for adhesive)."),
    ("mount_screw_clearance_diameter_mm", "number", ("gt", 1.0), 10, "tolerance",
     "Through-hole diameter for the mounting screw, one per ear (ignored without ears, but still required)."),
    ("mount_screw_head_diameter_mm", "number", ("gt", 1.0), 20, "measured",
     "Mounting screw head diameter; it must bear on the ear."),
    ("max_overall_width_mm", "number", 10, 600, "measured", "Available width where the holder goes."),
    ("max_overall_height_mm", "number", 5, 300, "measured", "Available height where the holder goes."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
]

# Cable comb frame: x along the comb (centred), y = 0 at the back (mounting) face, z = 0 at the base bar's
# bottom edge; cable seats open upward.
COMB = [
    ("part", "const:cable_comb", None, None, "label",
     "Selects a cable comb: a base bar with a row of round cable seats, each reached through an entry throat "
     "between two fingers, and optional screw holes through the bar."),
    ("cable_diameter_mm", "number", 1, 30, "measured", "Outer diameter of the cables (largest one, over the jacket)."),
    ("cable_clearance_mm", "number", 0, 3, "tolerance", "Added to cable_diameter_mm to size each round seat."),
    ("slot_count", "integer", 1, 48, "chosen", "Number of cable seats."),
    ("slot_pitch_mm", "number", 3, 60, "chosen", "Centre-to-centre spacing of the seats."),
    ("entry_throat_mm", "number", ("gt", 0.4), 30, "chosen",
     "Width of the entry above each seat. At least cable_diameter_mm: cables drop in freely. Narrower: cables "
     "are pressed in and the jacket deforms (avoid for fibre and thin coax)."),
    ("finger_length_mm", "number", 0, 40, "chosen", "Height of the fingers above the top of the seats."),
    ("base_height_mm", "number", 2, 60, "chosen", "Height of the solid bar below the seats."),
    ("comb_thickness_mm", "number", ("gt", 1.0), 30, "chosen", "Thickness of the comb (along the cables)."),
    ("end_margin_mm", "number", 0, 100, "chosen", "Material beyond the outermost seats at each end (at least min_wall_mm)."),
    ("mount_hole_spacing_mm", "number", 0, 600, "measured",
     "Centre distance of the two screw holes through the base bar, matching holes you measured (0 = no holes)."),
    ("mount_screw_clearance_diameter_mm", "number", ("gt", 1.0), 10, "tolerance",
     "Through-hole diameter for the mounting screws (ignored without holes, but still required)."),
    ("mount_screw_head_diameter_mm", "number", ("gt", 1.0), 20, "measured",
     "Mounting screw head diameter; it must bear on the base bar."),
    ("max_overall_width_mm", "number", 10, 600, "measured", "Available length for the comb."),
    ("max_overall_height_mm", "number", 5, 300, "measured", "Available height for the comb."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
]

# Cartridge holder frame: x centred, y = 0 at the front edge, z = 0 at the bottom. Items stand upright in a
# grid of pockets, item_width_mm across x and item_thickness_mm along y.
CARTRIDGE = [
    ("part", "const:cartridge_holder", None, None, "label",
     "Selects a holder with a grid of upright pockets for small modules or media (SFP transceivers, tape "
     "cartridges, ...), sized from your measurements, with optional push-out holes and finger notches."),
    ("item_width_mm", "number", 3, 200, "measured", "Item width across the holder (x)."),
    ("item_thickness_mm", "number", 3, 200, "measured", "Item thickness front to back (y)."),
    ("item_height_mm", "number", 10, 300, "measured", "Item height as it stands in the pocket."),
    ("item_clearance_mm", "number", 0, 3, "tolerance", "Gap added on each side of the item in its pocket."),
    ("columns", "integer", 1, 20, "chosen", "Pockets across (x)."),
    ("rows", "integer", 1, 10, "chosen", "Pockets front to back (y)."),
    ("pocket_depth_mm", "number", 3, 300, "chosen",
     "How deep each item sits. Must be less than item_height_mm so part of each item stands proud to grip."),
    ("floor_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Floor thickness under the pockets."),
    ("wall_thickness_mm", "number", ("gt", 1.0), 15, "chosen", "Outer wall thickness."),
    ("divider_thickness_mm", "number", ("gt", 0.4), 15, "chosen", "Thickness of the walls between pockets."),
    ("floor_hole_diameter_mm", "number", 0, 100, "chosen",
     "Push-out hole through the floor under each pocket (0 = none)."),
    ("finger_notch_width_mm", "number", 0, 100, "chosen",
     "Width of a notch cut down into the wall in front of each pocket (0 = none)."),
    ("finger_notch_depth_mm", "number", 0, 300, "chosen",
     "Depth of that notch from the top; at most pocket_depth_mm (ignored when the width is 0, but still required)."),
    ("max_overall_width_mm", "number", 10, 700, "measured", "Available width."),
    ("max_overall_depth_mm", "number", 10, 700, "measured", "Available depth."),
    ("max_overall_height_mm", "number", 5, 600, "measured", "Available height, including the items standing in it."),
    ("build_plate_x_mm", "number", 50, 1000, "measured", "Usable build plate size, first axis (measure your printer)."),
    ("build_plate_y_mm", "number", 50, 1000, "measured", "Usable build plate size, second axis."),
    ("build_plate_z_mm", "number", 20, 1000, "measured", "Usable build height."),
]

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}(?![\s\S])"

PARTS = {"rack_bracket": COMMON + BRACKET, "sff_cradle": COMMON + CRADLE,
         "switch_cradle": COMMON + SWITCH, "blanking_panel": COMMON + BLANKING, "cable_panel": COMMON + CABLE,
         "rack_shelf": COMMON + SHELF, "pdu_mount": COMMON + PDU,
         "keystone_panel": COMMON + KEYSTONE, "rack_drawer": COMMON + DRAWER, "label_holder": COMMON + LABEL,
         "cable_comb": COMMON + COMB, "cartridge_holder": COMMON + CARTRIDGE}

# Sanity windows that only catch unit mistakes; NOT standards for 10-inch racks.
RAIL_SPACING_WINDOW = {"10in": (180.0, 270.0), "19in": (430.0, 500.0)}

LIST_LIMITS = {"z_list": (1, 8), "y_list": (1, 4), "x_list": (2, 4), "yz_list": (1, 8), "xy_list": (1, 12),
               "xz_list": (1, 8), "rect_list": (0, 4), "keepout_list": (0, 12)}
SCALAR_LISTS = ("z_list", "y_list", "x_list")   # lists of numbers; the others are lists of fixed-size lists
TUPLE_SIZE = {"yz_list": 2, "xy_list": 2, "xz_list": 2, "rect_list": 4, "keepout_list": 4}


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
