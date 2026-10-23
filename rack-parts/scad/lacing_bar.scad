// rack-parts: cable-lacing bar hanging from the rail screws.
// Frame: x centred on the rack centreline, y = 0 at the flange front face (+y into the rack),
// z = 0 at the flange bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Flange with (slotted) rail holes; standoff arms at x = arms[i]; a bar from -half to +half whose
// front face is `gap` behind the flange. Tie tunnels run front to back through the bar at x = ties[i],
// tie_z above the bed; a tie goes through a tunnel and round the cable bundle resting on the bar.
module lacing_bar(w, h, t, hole_x, hole_zs, rail_d, slot_len, half, gap, bar_d, bar_h, arms, arm_w,
                  ties, tie_w, tie_h, tie_z, fn) {
    difference() {
        union() {
            translate([-w / 2, 0, 0]) cube([w, t, h]);
            for (x = arms)
                translate([x - arm_w / 2, t, 0]) cube([arm_w, gap, bar_h]);
            translate([-half, t + gap, 0]) cube([2 * half, bar_d, bar_h]);
        }
        for (sx = [-hole_x, hole_x], z = hole_zs)
            translate([sx, -1, z]) rotate([-90, 0, 0])
                hull() for (dx = [-slot_len / 2, slot_len / 2])
                    translate([dx, 0, 0]) cylinder(d = rail_d, h = t + 2, $fn = fn);
        for (x = ties)
            translate([x - tie_w / 2, t + gap - 1, tie_z]) cube([tie_w, bar_d + 2, tie_h]);
    }
}
