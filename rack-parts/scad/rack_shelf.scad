// rack-parts: shallow ventilated shelf hanging from the front rail screws.
// Frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack),
// z = 0 at the bottom edge of the front plate. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Front plate with (slotted) rail holes; a floor behind it, flush with the plate's bottom edge and
// spanning the rail opening minus side clearance; two side walls; optional rear lip; rounded vent
// slots through the floor running along the depth.
module rack_shelf(w, h, t, hole_x, hole_zs, rail_d, slot_len, half, depth, t_floor, wall_h, t_wall,
                  lip_h, vents, vent_w, fn) {
    difference() {
        union() {
            translate([-w / 2, 0, 0]) cube([w, t, h]);
            translate([-half, t, 0]) cube([2 * half, depth, t_floor]);
            for (s = [-1, 1])
                translate([s > 0 ? half - t_wall : -half, t, 0]) cube([t_wall, depth, t_floor + wall_h]);
            if (lip_h > 0)
                translate([-half, t + depth - t_wall, 0]) cube([2 * half, t_wall, t_floor + lip_h]);
        }
        for (sx = [-hole_x, hole_x], z = hole_zs)
            translate([sx, -1, z]) rotate([-90, 0, 0])
                hull() for (dx = [-slot_len / 2, slot_len / 2])
                    translate([dx, 0, 0]) cylinder(d = rail_d, h = t + 2, $fn = fn);
        // vent slots [x, y0, y1], rounded ends
        for (v = vents)
            translate([v[0], 0, -1]) hull() for (y = [v[1] + vent_w / 2, v[2] - vent_w / 2])
                translate([0, y, 0]) cylinder(d = vent_w, h = t_floor + 2, $fn = fn);
    }
}
