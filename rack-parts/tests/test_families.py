"""Tests for switch_cradle, blanking_panel and cable_panel (added after the bracket/SFF cradle)."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rackparts import generate, mesh, spec, validate  # noqa: E402
from tests.test_rackparts import EX, cli, load, refused  # noqa: E402

NEW_EXAMPLES = ["switch-cradle-synthetic.json", "blank-10in-1u-synthetic.json", "blank-19in-1u-synthetic.json",
                "cable-10in-1u-synthetic.json", "cable-19in-2u-synthetic.json"]
HAVE_OPENSCAD = shutil.which("openscad") is not None


def why(data):
    """Joined refusal reasons ('' if accepted)."""
    return "; ".join(refused(data) or [])


class ExampleTests(unittest.TestCase):
    def test_examples_are_valid(self):
        for name in NEW_EXAMPLES:
            self.assertIsNone(refused(load(name)), name)

    def test_both_rack_labels_have_panel_examples(self):
        for part in ("blanking_panel", "cable_panel"):
            classes = {load(n)["rack_width_class"] for n in NEW_EXAMPLES if load(n)["part"] == part}
            self.assertEqual(classes, {"10in", "19in"}, part)

    def test_oversize_flag_is_reported(self):
        _, geo = validate.validate(load("blank-19in-1u-synthetic.json"))
        self.assertFalse(geo["derived"]["fits_build_plate"])
        self.assertIn("does NOT fit", geo["print_orientation_suggestion"])
        _, geo = validate.validate(load("blank-10in-1u-synthetic.json"))
        self.assertTrue(geo["derived"]["fits_build_plate"])


class HostileNewFamilyTests(unittest.TestCase):
    def test_every_key_with_hostile_values(self):
        for ex in NEW_EXAMPLES:
            base = load(ex)
            for key in base:
                for bad in ([], {}, None, "x", True, False, 10 ** 400, -10 ** 400, 2 ** 64, 1e308, -1e308,
                            [[[]]], ["1"], [None], [[1, 2, 3]], [[1, 2, 3, 4, 5]], "$(touch pwned)",
                            "__import__('os').system('id')", "../../etc/passwd", "a\nb"):
                    if key == "name" and bad == "x":
                        continue  # a valid name
                    if key == "allow_oversize_print" and isinstance(bad, bool):
                        continue  # valid values
                    if key == "front_port_zone_mm" or key == "rear_port_zone_mm" or key == "side_vent_zone_mm":
                        if bad == []:
                            continue  # an explicitly empty zone list is valid
                    d = dict(base, **{key: bad})
                    try:
                        validate.validate(d)
                    except validate.ConfigError:
                        continue
                    self.fail("%s: %s=%r accepted" % (ex, key, bad))

    def test_malformed_part_types(self):
        b = load("blank-10in-1u-synthetic.json")
        for part in ("Blanking_panel", "blanking_panel ", "blanking-panel", "", "cable_panel\n", 1, None,
                     "__class__", "rack_panel"):
            self.assertTrue(refused(dict(b, part=part)), repr(part))
        # a valid part name with the other family's keys is refused (unknown/missing keys)
        self.assertIn("missing required key", why(dict(b, part="cable_panel")))
        self.assertIn("unknown key", why(dict(load("cable-10in-1u-synthetic.json"), part="blanking_panel")))

    def test_bool_is_strict(self):
        b = load("blank-10in-1u-synthetic.json")
        for v in (0, 1, "true", "false", None, [True]):
            self.assertIn("true or false", why(dict(b, allow_oversize_print=v)), repr(v))

    def test_integer_count_is_strict(self):
        s = load("switch-cradle-synthetic.json")
        for v in (3.0, 41, -1, 10 ** 30, True):
            self.assertTrue(refused(dict(s, base_vent_slot_count=v)), repr(v))

    def test_cli_regressions_new_families(self):
        for ex in NEW_EXAMPLES:
            text = open(os.path.join(EX, ex)).read()
            cases = [
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": 1' + "0" * 400),
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": 1e400'),
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": NaN'),
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": -Infinity'),
                text.replace('"facets": 48', '"facets": 48, "facets": 48'),
                text.replace('"name": "', '"name": "../'),
                text.replace('"name": "', '"name": "x\\n'),
                text[: len(text) // 2],
            ]
            for i, bad in enumerate(cases):
                with self.subTest(ex=ex, case=i), tempfile.TemporaryDirectory() as d:
                    src = os.path.join(d, "in.json")
                    with open(src, "w") as f:
                        f.write(bad)
                    out = os.path.join(d, "out")
                    r = cli("generate", src, "--out", out, "--scad-only")
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertFalse(os.path.exists(out))

    def test_out_with_control_characters_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("out\nx", "out\rx", "out\tx", "out\x7fx"):
                out = os.path.join(d, bad)
                r = cli("generate", os.path.join(EX, NEW_EXAMPLES[1]), "--out", out, "--scad-only")
                self.assertEqual(r.returncode, 2, repr(bad))
                self.assertIn("control character", r.stderr)
            self.assertEqual(os.listdir(d), [])

    def test_out_overwrite_and_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "sub")
            os.mkdir(sub)
            # traversal in --out is just a path the user chose; it must still never clobber
            out = os.path.join(sub, "..", "sub")
            r = cli("generate", os.path.join(EX, NEW_EXAMPLES[1]), "--out", out, "--scad-only")
            self.assertEqual(r.returncode, 3)
            self.assertEqual(os.listdir(sub), [])
            # files are named only from the validated name
            out = os.path.join(d, "ok")
            self.assertEqual(cli("generate", os.path.join(EX, NEW_EXAMPLES[1]), "--out", out,
                                 "--scad-only").returncode, 0)
            self.assertEqual(sorted(os.listdir(out)), ["manifest.json", "synthetic-blank-10in-1u.scad"])

    def test_scad_contains_only_validated_numbers(self):
        for ex in NEW_EXAMPLES:
            cfg, geo = validate.validate(load(ex))
            src = generate.scad_source(cfg, geo)
            call = src[src.rindex("\n%s(" % geo["module"]):]
            self.assertNotIn('"', call)
            self.assertNotIn("import", call)


class PanelGeometryRefusals(unittest.TestCase):
    def setUp(self):
        self.b19 = load("blank-19in-1u-synthetic.json")
        self.b10 = load("blank-10in-1u-synthetic.json")
        self.c10 = load("cable-10in-1u-synthetic.json")
        self.c19 = load("cable-19in-2u-synthetic.json")

    def test_rack_dimension_relations(self):
        self.assertIn("sanity window", why(dict(self.b19, rail_hole_center_spacing_mm=183.12)))   # 18.312 in * 10
        self.assertIn("sanity window", why(dict(self.b10, rail_hole_center_spacing_mm=465.1)))    # 19in on 10in
        self.assertIn("must exceed rail_opening", why(dict(self.b10, rail_opening_width_mm=240.0)))
        self.assertIn("covers the rails", why(dict(self.b10, panel_width_mm=215.0, rail_opening_width_mm=220.0)))
        self.assertIn("panel half-width", why(dict(self.b10, panel_width_mm=240.0)))
        self.assertIn("at least rail_screw_clearance", why(dict(self.b10, rail_screw_head_diameter_mm=5.0)))

    def test_holes_walls_and_spacing(self):
        self.assertIn("panel edge", why(dict(self.b10, rail_hole_z_offsets_mm=[3.0])))
        self.assertIn("panel edge", why(dict(self.b10, rail_hole_z_offsets_mm=[42.0])))
        self.assertIn("apart", why(dict(self.b10, rail_hole_z_offsets_mm=[10.0, 14.0])))
        self.assertIn("apart", why(dict(self.b10, rail_hole_z_offsets_mm=[20.0, 20.0])))  # duplicate hole

    def test_stiffeners(self):
        self.assertIn("obstruct the rail screw", why(dict(self.b19, rail_opening_width_mm=462.0,
                                                          rail_side_clearance_mm=0.0)))
        self.assertIn("thinner than min_wall", why(dict(self.b10, stiffener_thickness_mm=1.5)))
        self.assertIn("open height", why(dict(self.b10, stiffener_thickness_mm=10.0, panel_height_mm=21.0,
                                               rail_hole_z_offsets_mm=[10.5])))
        # stiffener thickness is irrelevant (and unchecked) for a flat plate
        self.assertIsNone(refused(dict(self.b10, stiffener_depth_mm=0.0, stiffener_thickness_mm=1.5)))

    def test_build_plate(self):
        self.assertIn("build plate", why(dict(self.b19, allow_oversize_print=False)))
        self.assertIn("build plate", why(dict(self.b10, build_plate_x_mm=250.0, build_plate_y_mm=250.0)))
        # rotation is considered
        self.assertIsNone(refused(dict(self.b10, build_plate_x_mm=60.0, build_plate_y_mm=254.0)))

    def test_cable_pass_throughs(self):
        c = self.c10
        self.assertIn("material between them", why(dict(c, pass_through_centers_mm=[[-30.0, 21.83], [30.0, 21.83]])))
        self.assertIn("material between them", why(dict(c, pass_through_centers_mm=[[-50.0, 21.83], [-50.0, 21.83]])))
        self.assertIn("inside the rail opening", why(dict(c, pass_through_centers_mm=[[80.0, 21.83]])))
        self.assertIn("stiffeners", why(dict(c, pass_through_centers_mm=[[0.0, 14.0]])))
        self.assertIn("more than half the panel thickness", why(dict(c, pass_through_edge_radius_mm=2.1)))
        self.assertIn("obstructed cable path", why(dict(c, cable_bundle_diameter_mm=16.5)))
        self.assertIn("must not exceed", why(dict(c, pass_through_width_mm=10.0, pass_through_height_mm=12.0,
                                                  cable_bundle_diameter_mm=5.0)))
        for r in (0, 0.4, -1):
            self.assertIn("pass_through_edge_radius_mm must be >", why(dict(c, pass_through_edge_radius_mm=r)))
        # an opening near the screw head is refused even if inside the rail opening
        self.assertIn("screw heads", why(dict(self.c19, rail_opening_width_mm=462.0, rail_side_clearance_mm=0.0,
                                              stiffener_depth_mm=0.0,
                                              pass_through_centers_mm=[[175.0, 44.05]])))


class SwitchGeometryRefusals(unittest.TestCase):
    def setUp(self):
        self.s = load("switch-cradle-synthetic.json")

    def test_retention_required(self):
        self.assertIn("missing retention", why(dict(self.s, stop_tab_width_mm=2.5)))       # 2.5 - 0.6 < 2
        self.assertIn("missing retention", why(dict(self.s, strap_slot_y_mm=[95.0])))      # strap off the device
        self.assertIn("close the front/rear opening", why(dict(self.s, stop_tab_width_mm=50.0, device_width_mm=100.0, front_port_zone_mm=[],
                                                              rear_port_zone_mm=[])))
        self.assertIn("strap_bar_height_mm", why(dict(self.s, strap_bar_height_mm=1.5)))
        self.assertIn("taller than the device", why(dict(self.s, stop_tab_height_mm=29.0, front_port_zone_mm=[],
                                                         rear_port_zone_mm=[])))
        self.assertIn("strap_slot_y_mm", why(dict(self.s, strap_slot_y_mm=[])))          # at least one strap

    def test_ports_and_vents_unobstructed(self):
        self.assertIn("obstructed port", why(dict(self.s, front_port_zone_mm=[[70.0, 79.0, 1.0, 10.0]])))
        self.assertIn("obstructed port", why(dict(self.s, rear_port_zone_mm=[[-80.0, -75.0, 0.0, 4.0]])))
        # same x but above the tabs is fine
        self.assertIsNone(refused(dict(self.s, front_port_zone_mm=[[70.0, 79.0, 6.5, 10.0]])))
        self.assertIn("outside the", why(dict(self.s, front_port_zone_mm=[[-90.0, 0.0, 1.0, 10.0]])))
        self.assertIn("outside the", why(dict(self.s, side_vent_zone_mm=[[20.0, 80.0, 6.0, 40.0]])))
        self.assertIn("lo < hi", why(dict(self.s, front_port_zone_mm=[[10.0, 10.0, 1.0, 5.0]])))
        self.assertIn("lo < hi", why(dict(self.s, side_vent_zone_mm=[[80.0, 20.0, 6.0, 22.0]])))
        self.assertIn("strap slot 0 and vent window 0", why(dict(self.s, side_vent_zone_mm=[[20.0, 80.0, 4.0, 28.0]])))
        self.assertIn("vent windows 0 and 1", why(dict(self.s, side_vent_zone_mm=[[20.0, 40.0, 4.0, 20.0],
                                                                                  [41.0, 60.0, 4.0, 20.0]])))
        self.assertIn("obstructed vent", why(dict(self.s, min_wall_mm=4.0, shelf_screw_head_height_mm=0.0,
                                                  side_vent_zone_mm=[[0.0, 50.0, 4.0, 20.0]])))

    def test_vents_holes_material(self):
        self.assertIn("less than min_wall_mm between them", why(dict(self.s, base_vent_slot_pitch_mm=6.0)))
        self.assertIn("fits inside the walls", why(dict(self.s, base_vent_slot_count=14)))
        self.assertIn("stop tabs / cradle ends", why(dict(self.s, base_vent_slot_length_mm=100.0)))
        self.assertIn("at least base_vent_slot_width", why(dict(self.s, base_vent_slot_length_mm=4.0)))
        self.assertIn("base vent slot", why(dict(self.s, shelf_hole_positions_mm=[[0.0, 53.6]])))
        self.assertIn("under a stop tab", why(dict(self.s, shelf_hole_positions_mm=[[-75.0, 5.0]])))
        self.assertIn("counterbore depth", why(dict(self.s, shelf_screw_head_height_mm=3.0)))
        self.assertIn("cut into a side wall", why(dict(self.s, shelf_hole_positions_mm=[[78.0, 50.0]])))
        self.assertIn("apart", why(dict(self.s, shelf_hole_positions_mm=[[-65.0, 15.0], [-60.0, 15.0]])))

    def test_envelope_and_plate(self):
        self.assertIn("max_overall_width_mm", why(dict(self.s, max_overall_width_mm=160.0)))
        self.assertIn("max_overall_depth_mm", why(dict(self.s, max_overall_depth_mm=100.0)))
        self.assertIn("max_overall_height_mm", why(dict(self.s, max_overall_height_mm=39.0)))
        self.assertIn("build plate", why(dict(self.s, build_plate_x_mm=150.0, build_plate_y_mm=150.0)))


def _render(test, d, data, label):
    src = os.path.join(d, label + ".json")
    with open(src, "w") as f:
        json.dump(data, f)
    out = os.path.join(d, label)
    r = cli("generate", src, "--out", out)
    test.assertEqual(r.returncode, 0, label + r.stderr)
    with open(os.path.join(out, "manifest.json")) as f:
        m = json.load(f)
    rep = m["mesh"]
    test.assertTrue(rep["ok"], (label, rep["problems"]))
    test.assertEqual(rep["open_edges"], 0, label)
    test.assertEqual(rep["edges_with_bad_winding_or_multiplicity"], 0, label)
    test.assertEqual(rep["degenerate_facets"], 0, label)
    test.assertEqual(rep["shells"], 1, label)
    test.assertTrue(rep["bbox_matches_expected"], label)
    test.assertGreater(rep["volume_mm3"], 0, label)
    if shutil.which("admesh"):
        test.assertTrue(rep["admesh"]["ok"], (label, rep["admesh"]))
        test.assertEqual(rep["admesh"]["parts"], 1, label)
    stl = os.path.join(out, m["files"]["stl"]["path"])
    test.assertEqual(generate.sha256_file(stl), m["files"]["stl"]["sha256"])
    return m, stl


def boundary_cases():
    """Small/large/boundary variants that must still render to one clean shell."""
    s = load("switch-cradle-synthetic.json")
    c = load("cable-10in-1u-synthetic.json")
    b = load("blank-10in-1u-synthetic.json")
    small_switch = dict(s, name="small-switch", device_width_mm=60.0, device_height_mm=12.0, device_depth_mm=40.0,
                        stop_tab_width_mm=4.0, stop_tab_height_mm=3.0, front_port_zone_mm=[[-20.0, 20.0, 3.5, 10.0]],
                        rear_port_zone_mm=[], side_vent_zone_mm=[], strap_slot_y_mm=[20.0],
                        strap_slot_length_mm=12.0, strap_slot_height_mm=2.0, strap_bar_height_mm=2.0,
                        base_vent_slot_count=0, shelf_hole_positions_mm=[[0.0, 23.6]],
                        max_overall_width_mm=70.0, max_overall_depth_mm=50.0, max_overall_height_mm=30.0)
    large_switch = dict(s, name="large-switch", device_width_mm=280.0, device_height_mm=44.0, device_depth_mm=200.0,
                        front_port_zone_mm=[[-120.0, 120.0, 4.0, 40.0], [125.0, 130.0, 10.0, 20.0]],
                        rear_port_zone_mm=[[-50.0, 50.0, 8.0, 30.0]],
                        side_vent_zone_mm=[[20.0, 80.0, 6.0, 38.0], [120.0, 180.0, 6.0, 38.0]],
                        strap_slot_y_mm=[100.0, 160.0], base_vent_slot_count=20, base_vent_slot_length_mm=120.0,
                        shelf_hole_positions_mm=[[-125.0, 20.0], [125.0, 20.0], [-125.0, 190.0], [125.0, 190.0]],
                        max_overall_width_mm=300.0, max_overall_depth_mm=220.0, max_overall_height_mm=80.0,
                        build_plate_x_mm=300.0, build_plate_y_mm=300.0)
    round_full_fillet = dict(c, name="cable-round-full-fillet", pass_through_width_mm=16.0,
                             pass_through_edge_radius_mm=2.0, pass_through_centers_mm=[[-60.0, 21.83],
                                                                                       [0.0, 21.83], [60.0, 21.83]])
    # flat plate, opening face edge exactly min_wall_mm above the bottom edge
    R = validate.hole_d(16.0, 48) / 2 + 1.5
    flat_min_gap = dict(c, name="cable-flat-min-gap", stiffener_depth_mm=0.0,
                        pass_through_centers_mm=[[0.0, 2.0 + R]])
    # two openings exactly min_wall_mm apart on the face (face width = width + 2 * edge radius)
    fw = 70.0 + 2 * 1.5
    gap_pair = dict(c, name="cable-min-gap-pair", pass_through_centers_mm=[[-(fw + 2.0) / 2, 21.83],
                                                                          [(fw + 2.0) / 2, 21.83]])
    blank_plate_exact = dict(b, name="blank-exact-plate", build_plate_x_mm=254.0, build_plate_y_mm=50.0,
                             stiffener_depth_mm=0.0, rail_slot_extra_length_mm=0.0)
    return [small_switch, large_switch, round_full_fillet, flat_min_gap, gap_pair, blank_plate_exact]


class BoundaryValidityTests(unittest.TestCase):
    def test_boundary_cases_validate(self):
        for d in boundary_cases():
            self.assertIsNone(refused(d), d["name"])

    def test_min_gap_is_a_boundary(self):
        d = boundary_cases()[4]
        (x0, z), (x1, _) = d["pass_through_centers_mm"]
        self.assertIn("material between them",
                      why(dict(d, pass_through_centers_mm=[[x0 + 0.01, z], [x1, z]])))


@unittest.skipUnless(HAVE_OPENSCAD, "openscad not installed")
class NewFamilyRenderTests(unittest.TestCase):
    def test_examples_and_boundaries_render_to_clean_meshes(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in NEW_EXAMPLES:
                _render(self, d, load(ex), ex[:-5])
            for case in boundary_cases():
                _render(self, d, case, case["name"])

    def test_pass_through_edges_are_rounded(self):
        """The rendered mesh has the flared face opening and the narrower neck where the validator says."""
        cfg = load("cable-10in-1u-synthetic.json")
        _, geo = validate.validate(cfg)
        with tempfile.TemporaryDirectory() as d:
            _, stl = _render(self, d, cfg, "cable")
            tris = mesh.read_stl(stl)
        verts = {v for t in tris for v in t}
        t = cfg["panel_thickness_mm"]
        f = cfg["pass_through_edge_radius_mm"]
        x_c, z_c = cfg["pass_through_centers_mm"][1]
        neck_r = geo["args"]["cut_h"] / 2
        a = (geo["args"]["cut_len"] - geo["args"]["cut_h"]) / 2

        def top_edge_at(y):
            # the straight top edge of the stadium only has vertices at its two ends, x_c +- a
            zs = [v[2] for v in verts if abs(v[1] - y) < 1e-3 and abs(v[0] - x_c) <= a + 1e-3 and v[2] > z_c]
            return min(zs) - z_c

        self.assertAlmostEqual(top_edge_at(0.0), neck_r + f, delta=0.01)       # front face: flared
        self.assertAlmostEqual(top_edge_at(t), neck_r + f, delta=0.01)         # back face: flared
        self.assertAlmostEqual(top_edge_at(f), neck_r, delta=0.01)             # neck
        self.assertAlmostEqual(top_edge_at(t - f), neck_r, delta=0.01)
        # an intermediate step lies on the quarter circle (chord end points are exact)
        y = f / 2
        expect = neck_r + f - (f * f - (f - y) ** 2) ** 0.5
        self.assertAlmostEqual(top_edge_at(y), expect, delta=0.01)

    def test_render_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            ms = []
            for out in ("a", "b"):
                p = os.path.join(d, out)
                self.assertEqual(cli("generate", os.path.join(EX, "cable-10in-1u-synthetic.json"),
                                     "--out", p).returncode, 0)
                with open(os.path.join(p, "manifest.json")) as f:
                    m = json.load(f)
                del m["files"]["stl"]["sha256"]
                ms.append(m)
            self.assertEqual(ms[0], ms[1])


class DocTests(unittest.TestCase):
    def test_readme_has_commands_for_every_example(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for ex in NEW_EXAMPLES:
            self.assertTrue("examples/" + ex in readme, ex)
        for part in ("switch_cradle", "blanking_panel", "cable_panel"):
            self.assertIn(part, spec.PARTS)
            self.assertTrue("`%s`" % part in readme, part)


if __name__ == "__main__":
    unittest.main()
