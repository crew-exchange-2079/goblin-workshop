// rack-parts: holder with a grid of upright pockets for modules or media (SFP, tape cartridges, ...).
// Frame: x centred, y = 0 at the front edge, z = 0 at the bottom. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Rectangular pockets open at the top, optional round push-out holes through the floor and optional finger
// notches [x0, x1, y0, y1, z0] cut down through the wall in front of each pocket.
module cartridge_holder(outer_w, outer_d, height, t_floor, pocket_w, pocket_d, pockets, floor_hole_d,
                        notches, fn) {
    difference() {
        translate([-outer_w / 2, 0, 0]) cube([outer_w, outer_d, height]);
        for (q = pockets) {
            translate([q[0] - pocket_w / 2, q[1] - pocket_d / 2, t_floor]) cube([pocket_w, pocket_d, height]);
            if (floor_hole_d > 0)
                translate([q[0], q[1], -1]) cylinder(d = floor_hole_d, h = t_floor + 2, $fn = fn);
        }
        for (n = notches)
            translate([n[0], n[2], n[4]]) cube([n[1] - n[0], n[3] - n[2], height]);
    }
}
