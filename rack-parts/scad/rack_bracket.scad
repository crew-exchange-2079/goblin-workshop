// rack-parts: L-shaped rack ear bracket (right hand; left = X mirror).
// Frame: x = 0 at the device-side face of the side flange, +x toward the rail;
// y = 0 at the front face, +y into the rack; z = 0 at the bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
module rack_bracket(t, h, ear_w, side_depth, gusset, rail_hole_x, rail_hole_zs,
                    rail_d, slot_len, dev_holes, dev_d, left, fn) {
    mirror([left ? 1 : 0, 0, 0]) difference() {
        union() {
            cube([ear_w, t, h]);                       // front flange (bolts to rails)
            cube([t, side_depth, h]);                  // side flange (bolts to device)
            if (gusset > 0)
                translate([t, t, 0]) linear_extrude(height = h)
                    polygon([[0, 0], [gusset, 0], [0, gusset]]);
        }
        // rail holes / horizontal slots through the front flange
        for (z = rail_hole_zs)
            translate([rail_hole_x, -1, z]) rotate([-90, 0, 0])
                hull() for (dx = [-slot_len / 2, slot_len / 2])
                    translate([dx, 0, 0]) cylinder(d = rail_d, h = t + 2, $fn = fn);
        // device screw holes through the side flange
        for (p = dev_holes)
            translate([-1, p[0], p[1]]) rotate([0, 90, 0])
                cylinder(d = dev_d, h = t + 2, $fn = fn);
    }
}
