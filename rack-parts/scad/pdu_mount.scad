// rack-parts: one external, strap-retained bracket for a power strip / PDU.
// Frame: x along the strip (bracket centred), y across the strip (centred), z = 0 on the mounting
// surface. mm. Parameters are supplied by the generator after validation; see rackparts/validate.py.
// The strip rests on the base between two side walls. A strap crosses the strip's top face and wraps
// around the bar above the slot in each wall. Screw ears outside the walls fix the bracket to a surface.
// Nothing enters, covers or clamps the strip's case, outlets or cord.
module pdu_mount(length, inner_w, t_base, t_wall, ear, wall_h, slot_z0, slot_len, slot_h, hole_y, hole_d, fn) {
    outer_w = inner_w + 2 * t_wall + 2 * ear;
    difference() {
        union() {
            translate([-length / 2, -outer_w / 2, 0]) cube([length, outer_w, t_base]);
            for (s = [-1, 1])
                translate([-length / 2, s > 0 ? inner_w / 2 : -inner_w / 2 - t_wall, 0])
                    cube([length, t_wall, wall_h]);
        }
        for (s = [-1, 1]) {
            translate([-slot_len / 2, s > 0 ? inner_w / 2 - 1 : -inner_w / 2 - t_wall - 1, slot_z0])
                cube([slot_len, t_wall + 2, slot_h]);
            translate([0, s * hole_y, -1]) cylinder(d = hole_d, h = t_base + 2, $fn = fn);
        }
    }
}
