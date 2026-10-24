// rack-parts: rack drawer housing - a front plate on the rail screws with a closed sleeve behind it.
// Frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack),
// z = 0 at the bottom edge of the front plate. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// The sleeve (floor, side walls, roof, back wall) spans the rail opening minus side clearance and is open
// through the front plate; the drawer (drawer_box.scad) slides into it and stops on the back wall.
module drawer_housing(w, h, t, hole_x, hole_zs, rail_d, slot_len, half, depth, sleeve_h,
                      t_floor, t_wall, t_roof, t_back, fn) {
    difference() {
        union() {
            translate([-w / 2, 0, 0]) cube([w, t, h]);
            translate([-half, 0, 0]) cube([2 * half, t + depth, sleeve_h]);
        }
        translate([-(half - t_wall), -1, t_floor])
            cube([2 * (half - t_wall), t + depth - t_back + 1, sleeve_h - t_floor - t_roof]);
        for (sx = [-hole_x, hole_x], z = hole_zs)
            translate([sx, -1, z]) rotate([-90, 0, 0])
                hull() for (dx = [-slot_len / 2, slot_len / 2])
                    translate([dx, 0, 0]) cylinder(d = rail_d, h = t + 2, $fn = fn);
    }
}
