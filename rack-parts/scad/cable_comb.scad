// rack-parts: cable comb - a bar with a row of round cable seats reached through entry throats.
// Frame: x along the comb (centred), y = 0 at the back (mounting) face, z = 0 at the bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Each seat is a round hole through the comb's thickness; a straight throat of width `throat` opens it to
// the top between two fingers. Optional screw holes run through the base bar.
module cable_comb(length, t, base_h, height, slots_x, slot_d, slot_z, throat, holes_x, hole_d, fn) {
    difference() {
        translate([-length / 2, 0, 0]) cube([length, t, height]);
        for (x = slots_x) {
            translate([x, -1, slot_z]) rotate([-90, 0, 0]) cylinder(d = slot_d, h = t + 2, $fn = fn);
            translate([x - throat / 2, -1, slot_z]) cube([throat, t + 2, height]);
        }
        for (x = holes_x)
            translate([x, -1, base_h / 2]) rotate([-90, 0, 0]) cylinder(d = hole_d, h = t + 2, $fn = fn);
    }
}
