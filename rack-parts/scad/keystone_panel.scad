// rack-parts: keystone patch panel - a flat rack panel with rows of snap-in keystone jack cutouts.
// Frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack),
// z = 0 at the bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Each jack gets a rectangular cutout through the panel; where seat_t < t a pocket the size of the jack body
// is cut from the back so the latch grips seat_t of material. Optional stiffening ribs as on rack_panel.
module keystone_panel(w, h, t, hole_x, hole_zs, rail_d, slot_len, rib_half, rib_depth, rib_t,
                      jack_x, rows, cut_w, cut_h, pocket_w, pocket_h, seat_t, fn) {
    difference() {
        union() {
            translate([-w / 2, 0, 0]) cube([w, t, h]);
            if (rib_depth > 0)
                for (z = [0, h - rib_t])
                    translate([-rib_half, t, z]) cube([2 * rib_half, rib_depth, rib_t]);
        }
        // rail holes / horizontal slots
        for (sx = [-hole_x, hole_x], z = hole_zs)
            translate([sx, -1, z]) rotate([-90, 0, 0])
                hull() for (dx = [-slot_len / 2, slot_len / 2])
                    translate([dx, 0, 0]) cylinder(d = rail_d, h = t + 2, $fn = fn);
        for (z = rows, x = jack_x) {
            translate([x - cut_w / 2, -1, z - cut_h / 2]) cube([cut_w, t + 2, cut_h]);
            if (seat_t < t)
                translate([x - pocket_w / 2, seat_t, z - pocket_h / 2]) cube([pocket_w, t - seat_t + 1, pocket_h]);
        }
    }
}
