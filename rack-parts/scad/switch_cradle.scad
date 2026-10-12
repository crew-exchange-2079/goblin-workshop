// rack-parts: drop-in cradle for a small network switch, screwed to a shelf/panel.
// Frame: x centred, y = 0 at the front edge, z = 0 at the base bottom. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Front/rear stop tabs hold the switch fore-aft, the side walls hold it laterally, and a strap
// crossing its top wraps around the bar above each strap slot. Side vent windows, base vent
// slots and the open front/rear leave airflow and cable access.
module switch_cradle(t_base, t_wall, inner_w, length, wall_h, tab_w, tab_h, windows, slots,
                     slot_z0, slot_h, vents, vent_w, holes, hole_d, cb_d, cb_depth, fn) {
    outer_w = inner_w + 2 * t_wall;
    difference() {
        union() {
            translate([-outer_w / 2, 0, 0]) cube([outer_w, length, t_base]);
            for (s = [-1, 1]) {
                translate([s > 0 ? inner_w / 2 : -outer_w / 2, 0, 0]) cube([t_wall, length, wall_h]);
                for (y = [0, length - t_wall])
                    translate([s > 0 ? inner_w / 2 - tab_w : -inner_w / 2, y, 0])
                        cube([tab_w, t_wall, t_base + tab_h]);
            }
        }
        // side vent windows [y0, y1, z0, z1] through both walls
        for (wd = windows, s = [-1, 1])
            translate([s > 0 ? inner_w / 2 - 1 : -outer_w / 2 - 1, wd[0], wd[2]])
                cube([t_wall + 2, wd[1] - wd[0], wd[3] - wd[2]]);
        // strap slots [y0, y1] through both walls
        for (sl = slots, s = [-1, 1])
            translate([s > 0 ? inner_w / 2 - 1 : -outer_w / 2 - 1, sl[0], slot_z0])
                cube([t_wall + 2, sl[1] - sl[0], slot_h]);
        // base vent slots [x, y0, y1], rounded ends
        for (v = vents)
            translate([v[0], 0, -1]) hull() for (y = [v[1] + vent_w / 2, v[2] - vent_w / 2])
                translate([0, y, 0]) cylinder(d = vent_w, h = t_base + 2, $fn = fn);
        for (p = holes) {
            translate([p[0], p[1], -1]) cylinder(d = hole_d, h = t_base + 2, $fn = fn);
            if (cb_depth > 0)
                translate([p[0], p[1], t_base - cb_depth]) cylinder(d = cb_d, h = cb_depth + 1, $fn = fn);
        }
    }
}
