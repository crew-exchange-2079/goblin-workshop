// rack-parts: U-shaped small-form-factor PC cradle that screws to a shelf/panel.
// Frame: x centred, y = 0 at the front edge, z = 0 at the base bottom. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
module sff_cradle(t_base, t_wall, inner_w, length, clear_h, lip, holes,
                  hole_d, cb_d, cb_depth, fn) {
    outer_w = inner_w + 2 * t_wall;
    wall_h = t_base + clear_h + (lip > 0 ? t_wall : 0);
    difference() {
        union() {
            translate([-outer_w / 2, 0, 0]) cube([outer_w, length, t_base]);
            for (s = [-1, 1]) {
                translate([s > 0 ? inner_w / 2 : -outer_w / 2, 0, 0])
                    cube([t_wall, length, wall_h]);
                if (lip > 0)
                    translate([s > 0 ? inner_w / 2 - lip : -inner_w / 2, 0, t_base + clear_h])
                        cube([lip, length, t_wall]);
            }
        }
        for (p = holes) {
            translate([p[0], p[1], -1]) cylinder(d = hole_d, h = t_base + 2, $fn = fn);
            if (cb_depth > 0)
                translate([p[0], p[1], t_base - cb_depth]) cylinder(d = cb_d, h = cb_depth + 1, $fn = fn);
        }
    }
}
