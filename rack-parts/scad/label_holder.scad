// rack-parts: slide-in label holder.
// Frame: x centred, y = 0 at the back (mounting) face, +y toward the reader, z = 0 at the bottom edge. mm.
// Parameters are supplied by the generator after validation; see rackparts/validate.py.
// A back plate (with optional screw ears), a card pocket open at the top, and a front frame whose lips
// cover the card's sides and bottom edge around a window that is open at the top.
module label_holder(outer_w, body_w, outer_h, t_back, t_front, pocket_w, pocket_h, pocket_t, bottom_wall,
                    win_w, lip, holes_x, hole_d, fn) {
    depth = t_back + pocket_t + t_front;
    difference() {
        union() {
            translate([-outer_w / 2, 0, 0]) cube([outer_w, t_back, outer_h]);
            translate([-body_w / 2, 0, 0]) cube([body_w, depth, outer_h]);
        }
        translate([-pocket_w / 2, t_back, bottom_wall]) cube([pocket_w, pocket_t, outer_h]);
        translate([-win_w / 2, t_back, bottom_wall + lip]) cube([win_w, pocket_t + t_front + 1, outer_h]);
        for (x = holes_x)
            translate([x, -1, outer_h / 2]) rotate([-90, 0, 0]) cylinder(d = hole_d, h = t_back + 2, $fn = fn);
    }
}
