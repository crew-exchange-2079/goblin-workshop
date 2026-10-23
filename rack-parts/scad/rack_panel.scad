// rack-parts: flat rack panel (blanking, cable pass-through or label holder) with optional stiffening ribs.
// Frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack),
// z = 0 at the bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// Cable pass-throughs are horizontal stadiums (semicircular ends). Both face edges are rounded
// with a quarter-round of radius `fillet`, approximated by `steps` straight-sided frustums.
// `label` (empty for no holder) is [x0, x1, z0, z1, wx0, wz0, wz1, entry_x0, face_t, depth]: a pocket
// x0..x1 / z0..z1 behind a front skin face_t thick, a window from wx0 to x1 between wz0 and wz1, and a
// full-height entry opening from entry_x0 to x1.
module rack_panel(w, h, t, hole_x, hole_zs, rail_d, slot_len, rib_half, rib_depth, rib_t,
                  cuts, cut_len, cut_h, fillet, steps, fn, label) {
    r = cut_h / 2;
    a = max(cut_len - cut_h, 0) / 2;
    // one frustum of the stadium cutter between y0 and y1, radius r0 -> r1 around both end centres
    module stadium_frustum(y0, y1, r0, r1)
        hull() for (dx = [-a, a])
            translate([dx, y0, 0]) rotate([-90, 0, 0]) cylinder(r1 = r0, r2 = r1, h = y1 - y0, $fn = fn);
    // flare offset at depth d from a face: f - sqrt(f^2 - (f - d)^2)
    function flare(d) = fillet - sqrt(max(fillet * fillet - (fillet - d) * (fillet - d), 0));
    module cutter() union() {
        stadium_frustum(-1, 0, r + fillet, r + fillet);
        stadium_frustum(t, t + 1, r + fillet, r + fillet);
        for (i = [0 : steps - 1]) {
            d0 = fillet * i / steps;
            d1 = fillet * (i + 1) / steps;
            stadium_frustum(d0, d1, r + flare(d0), r + flare(d1));           // front face edge
            stadium_frustum(t - d1, t - d0, r + flare(d1), r + flare(d0));   // back face edge
        }
        if (t > 2 * fillet) stadium_frustum(fillet, t - fillet, r, r);
    }
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
        for (c = cuts)
            translate([c[0], 0, c[1]]) cutter();
        if (len(label) == 10) {
            x0 = label[0]; x1 = label[1]; z0 = label[2]; z1 = label[3];
            ft = label[8]; d = label[9];
            translate([x0, ft, z0]) cube([x1 - x0, d, z1 - z0]);                                 // pocket
            translate([label[4], -1, label[5]]) cube([x1 - label[4], 1 + ft + d / 2, label[6] - label[5]]);  // window
            translate([label[7], -1, z0]) cube([x1 - label[7], 1 + ft + d / 2, z1 - z0]);       // entry
        }
    }
}
