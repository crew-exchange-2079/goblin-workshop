// rack-parts: rack drawer box - an open-top box with a front that stops against the housing.
// Frame: x centred, y = 0 at the drawer front face (+y into the rack), z = 0 at the drawer bottom. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Floor, side and back walls; a front plate (bottom flush with the box) with an optional stadium finger
// pull; optional dividers across the width.
module drawer_box(box_w, box_l, box_h, t_floor, t_wall, t_front, front_w, front_h,
                  pull_w, pull_h, pull_z, dividers, fn) {
    union() {
        difference() {
            union() {
                translate([-front_w / 2, 0, 0]) cube([front_w, t_front, front_h]);
                translate([-box_w / 2, 0, 0]) cube([box_w, t_front + box_l, box_h]);
            }
            translate([-(box_w / 2 - t_wall), t_front, t_floor])
                cube([box_w - 2 * t_wall, box_l - t_wall, box_h]);
            if (pull_w > 0)
                translate([0, -1, pull_z]) rotate([-90, 0, 0])
                    hull() for (dx = [-(pull_w - pull_h) / 2, (pull_w - pull_h) / 2])
                        translate([dx, 0, 0]) cylinder(d = pull_h, h = t_front + 2, $fn = fn);
        }
        for (x = dividers)
            translate([x - t_wall / 2, t_front, t_floor]) cube([t_wall, box_l - t_wall, box_h - t_floor]);
    }
}
