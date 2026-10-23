"""Tests for the 19-inch accessories (lacing_bar, label_panel, 19-inch rack_shelf) and the spec checker.

Render tests skip when OpenSCAD/ADMesh are missing, unless RACKPARTS_REQUIRE_RENDER=1 (set in CI).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rackparts import mesh, spec, speccheck, validate  # noqa: E402
from tests.test_families import _render, why  # noqa: E402
from tests.test_rackparts import EX, FX, cli, load, refused  # noqa: E402

EXAMPLES = ["lacing-19in-1u-synthetic.json", "label-19in-1u-synthetic.json", "shelf-19in-1u-switch-synthetic.json"]
FIXTURES = ["loose-lip-label-panel.json", "thin-wall-label-panel.json", "no-tie-points-lacing-bar.json",
            "shank-lacing-bar.json", "open-frame-19in-shelf.json"]
REQUIRE_RENDER = os.environ.get("RACKPARTS_REQUIRE_RENDER") == "1"
HAVE_TOOLS = bool(shutil.which("openscad") and shutil.which("admesh"))


def box(x0, y0, z0, x1, y1, z1):
    """Outward-wound triangles of an axis-aligned box."""
    v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return [t for a, b, c, d in quads for t in ((v[a], v[b], v[c]), (v[a], v[c], v[d]))]


def plain_manifest(min_wall=1.0, up=(0, 0, 1)):
    return {"input": {"parameters": {"part": "test", "name": "t", "min_wall_mm": min_wall}},
            "print_orientations": {"up": list(up)}}


class ExampleTests(unittest.TestCase):
    def test_examples_valid_and_19in(self):
        parts = set()
        for name in EXAMPLES:
            d = load(name)
            self.assertIsNone(refused(d), name)
            self.assertEqual(d["rack_width_class"], "19in")
            self.assertEqual(d["rail_screw_clearance_diameter_mm"], 6.6)   # M6, ISO 273 medium
            parts.add(d["part"])
            self.assertTrue(validate.validate(d)[1]["print_orientations"], name)
        self.assertEqual(parts, {"lacing_bar", "label_panel", "rack_shelf"})

    def test_oversize_is_flagged_not_hidden(self):
        for name in ("lacing-19in-1u-synthetic.json", "label-19in-1u-synthetic.json"):
            geo = validate.validate(load(name))[1]
            self.assertFalse(geo["derived"]["fits_build_plate"], name)
            self.assertIn("WARNING", geo["print_orientation_suggestion"])
        # the shelf has no oversize option: its example states a large-format build volume and fits it
        geo = validate.validate(load("shelf-19in-1u-switch-synthetic.json"))[1]
        self.assertEqual(geo["derived"]["printable_orientations"], ["front_face_down", "floor_down"])

    def test_lacing_layout(self):
        d = load("lacing-19in-1u-synthetic.json")
        g = validate.validate(d)[1]["derived"]
        arms, ties = g["arm_x_mm"], g["tie_slot_x_mm"]
        self.assertEqual(len(arms), 5)
        self.assertAlmostEqual(arms[0], -arms[-1])
        self.assertEqual(len(ties), 16)                      # 4 per bay
        sw = d["tie_width_mm"] + d["tie_clearance_mm"]
        for x in ties:   # every tunnel keeps min_wall_mm to the nearest arm
            gap = min(abs(x - a) for a in arms) - d["arm_width_mm"] / 2 - sw / 2
            self.assertGreaterEqual(gap, d["min_wall_mm"] - 1e-9)
        self.assertEqual(g["tie_slot_z_mm"][0], 2.0)
        self.assertAlmostEqual(g["tie_slot_z_mm"][1], 2.0 + 1.2 + 0.6)

    def test_label_layout(self):
        g = validate.validate(load("label-19in-1u-synthetic.json"))[1]["derived"]
        self.assertAlmostEqual(g["label_pocket_mm"][0], 100 + 0.8 + 15)
        self.assertAlmostEqual(g["label_window_mm"][1], 18 - 2 * 1.5)
        self.assertAlmostEqual(g["label_rear_wall_mm"], 2.3)


class HostileTests(unittest.TestCase):
    def test_every_key_with_hostile_values(self):
        for ex in EXAMPLES:
            base = load(ex)
            for key in base:
                for bad in ([], {}, None, "x", True, 10 ** 400, 2 ** 64, -1e308, [[[]]], ["1"], [None],
                            [1.0] * 9, "$(touch pwned)", "../../etc/passwd", "a\nb"):
                    if key == "name" and bad == "x":
                        continue
                    if key == "allow_oversize_print" and bad is True:
                        continue
                    with self.subTest(ex=ex, key=key, bad=repr(bad)[:40]):
                        self.assertTrue(refused(dict(base, **{key: bad})))

    def test_cross_family_keys(self):
        lab, lac = load(EXAMPLES[1]), load(EXAMPLES[0])
        self.assertIn("unknown key", why(dict(lab, part="blanking_panel")))
        self.assertIn("missing required key", why(dict(load("blank-19in-1u-synthetic.json"), part="label_panel")))
        self.assertIn("unknown key", why(dict(lac, part="rack_shelf")))
        self.assertTrue(refused(dict(lac, arm_count=2.0)))

    def test_fixtures_refused_without_output(self):
        for name in FIXTURES:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out")
                r = cli("generate", os.path.join(FX, name), "--out", out)
                self.assertEqual(r.returncode, 2, name + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertFalse(os.path.exists(out), name)


class LabelRefusals(unittest.TestCase):
    def setUp(self):
        self.d = load("label-19in-1u-synthetic.json")

    def test_lips_and_window(self):
        self.assertIn("slips out", why(dict(self.d, label_lip_mm=0.4)))
        self.assertIsNone(refused(dict(self.d, label_lip_mm=0.41)))
        self.assertIn("no window", why(dict(self.d, label_lip_mm=9.0)))

    def test_skin_and_rear_wall(self):
        self.assertIn("thinner than min_wall_mm", why(dict(self.d, label_face_thickness_mm=1.5)))
        self.assertIsNone(refused(dict(self.d, panel_thickness_mm=4.7)))   # 2 + 0.3 + 0.4 + 2
        self.assertIn("rear wall", why(dict(self.d, panel_thickness_mm=4.69)))

    def test_position(self):
        # x limit: 465.1/2 - 3/2 - 12/2 - 2 = 223.05; holder is 115.8 long
        self.assertIsNone(refused(dict(self.d, label_center_x_mm=223.05 - 57.9)))
        self.assertIn("rail screw heads", why(dict(self.d, label_center_x_mm=223.06 - 57.9)))
        self.assertIn("panel edges", why(dict(self.d, label_center_z_mm=10.0)))
        self.assertIn("panel edges", why(dict(self.d, label_height_mm=40.0)))


class LacingRefusals(unittest.TestCase):
    def setUp(self):
        self.d = load("lacing-19in-1u-synthetic.json")

    def test_rails_and_depth(self):
        self.assertIn("rail screw shank", why(dict(self.d, rail_opening_width_mm=460.0, rail_side_clearance_mm=0.0)))
        self.assertIn("rack_clear_depth_mm", why(dict(self.d, rack_clear_depth_mm=41.9)))
        self.assertIsNone(refused(dict(self.d, rack_clear_depth_mm=42.0)))
        self.assertIn("sanity window", why(dict(self.d, rail_hole_center_spacing_mm=236.5)))

    def test_bar(self):
        self.assertIn("rises above the flange", why(dict(self.d, bar_height_mm=44.0)))
        self.assertIsNone(refused(dict(self.d, bar_height_mm=5.8)))        # 1.8 tunnel + 2 x 2 wall
        self.assertIn("tie tunnel", why(dict(self.d, bar_height_mm=5.79)))
        self.assertIn("at least min_wall_mm", why(dict(self.d, bar_depth_mm=1.5)))
        self.assertIn("at least min_wall_mm", why(dict(self.d, arm_width_mm=1.5)))

    def test_tie_points(self):
        self.assertIn("no tie points", why(dict(self.d, arm_count=12, arm_width_mm=30.0)))
        self.assertIn("between", why(dict(self.d, tie_slot_pitch_mm=7.3)))
        self.assertIsNone(refused(dict(self.d, tie_slot_pitch_mm=7.4)))
        self.assertIn("bays", why(dict(self.d, arm_count=12, arm_width_mm=37.0)))

    def test_build_plate(self):
        self.assertIn("allow_oversize_print", why(dict(self.d, allow_oversize_print=False)))
        self.assertIsNone(refused(dict(self.d, allow_oversize_print=False, build_plate_x_mm=500.0)))
        self.assertIn("build volume", why(dict(self.d, allow_oversize_print=False, build_plate_x_mm=500.0,
                                                build_plate_z_mm=40.0)))


def boundary_cases():
    """Small/large/boundary variants that must still render to one clean shell."""
    lac, lab = load("lacing-19in-1u-synthetic.json"), load("label-19in-1u-synthetic.json")
    sh = load("shelf-19in-1u-switch-synthetic.json")
    lacing_min = dict(lac, name="lacing-min", arm_count=2, arm_width_mm=2.0, bar_depth_mm=2.0, bar_height_mm=5.8,
                      bar_standoff_mm=5.0, tie_slot_pitch_mm=7.4, rail_slot_extra_length_mm=0.0)
    # every bay exactly one tunnel + min_wall_mm to both arms: bay = 5.4 + 2 x 2 = 9.4
    lacing_tight = dict(lac, name="lacing-tight-bays", arm_count=12, arm_width_mm=(447.0 - 11 * 9.4) / 12)
    label_edge = dict(lab, name="label-edge-flat", panel_thickness_mm=4.7, stiffener_depth_mm=0.0,
                      label_center_x_mm=223.05 - 57.9)
    shelf_2u = dict(sh, name="shelf-19in-2u-deep", panel_height_mm=88.11, rail_hole_z_offsets_mm=[5.955, 82.155],
                    shelf_depth_mm=250.0, rack_clear_depth_mm=254.0, side_wall_height_mm=60.0,
                    rear_lip_height_mm=60.0, vent_slot_length_mm=230.0, device_width_mm=300.0,
                    device_depth_mm=200.0, device_height_mm=70.0)
    return [lacing_min, lacing_tight, label_edge, shelf_2u]


class BoundaryValidityTests(unittest.TestCase):
    def test_boundary_cases_validate(self):
        for d in boundary_cases():
            self.assertIsNone(refused(d), d["name"])
        self.assertEqual(len(validate.validate(boundary_cases()[1])[1]["derived"]["tie_slot_x_mm"]), 11)


class SpecCheckUnitTests(unittest.TestCase):
    def test_wall_thickness_of_a_plate(self):
        m = speccheck.Mesh(box(0, 0, 0, 30, 20, 1.5))
        self.assertAlmostEqual(speccheck.wall_thickness(m, 1.5)["thinnest_mm"], 1.5)
        self.assertTrue(speccheck.wall_thickness(m, 1.5)["ok"])
        rep = speccheck.wall_thickness(m, 2.0)
        self.assertFalse(rep["ok"])
        self.assertGreater(rep["facets_below_min_wall"], 0)

    def test_floating_block_is_an_island(self):
        m = speccheck.Mesh(box(0, 0, 0, 10, 10, 1) + box(0, 0, 5, 10, 10, 6))
        rep = speccheck.overhangs(m, (0, 0, 1))
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["failing_regions"][0]["island"])
        self.assertIsNone(rep["worst_unsupported_ledge_mm"])
        # flipped, the upper block's top is the bed and the lower one floats instead
        self.assertFalse(speccheck.overhangs(m, (0, 0, -1))["ok"])
        self.assertTrue(speccheck.overhangs(speccheck.Mesh(box(0, 0, 0, 10, 10, 1)), (0, 0, 1))["ok"])

    def test_check_combines_orientations(self):
        m = box(0, 0, 0, 10, 10, 1) + box(0, 0, 5, 10, 10, 6)
        man = plain_manifest()
        man["print_orientations"] = {"up": [0, 0, 1], "side": [1, 0, 0]}
        rep = speccheck.check(m, man, fastener="none")
        self.assertEqual(rep["overhangs"]["passing"], ["side"])  # on its side nothing overhangs
        self.assertTrue(rep["ok"])
        del man["print_orientations"]
        rep = speccheck.check(m, man, fastener="none")
        self.assertEqual(rep["overhangs"]["orientations_from"], "all six axes")
        self.assertEqual(rep["overhangs"]["passing"], ["+x", "+y", "-x", "-y"])


def _scad_mesh(test, d, src):
    path = os.path.join(d, "shape.scad")
    with open(path, "w") as f:
        f.write(src)
    stl = os.path.join(d, "shape.stl")
    r = subprocess.run(["openscad", "--export-format", "binstl", "-o", stl, path], capture_output=True, text=True)
    test.assertEqual(r.returncode, 0, r.stderr)
    return mesh.read_stl(stl)


@unittest.skipUnless(HAVE_TOOLS or REQUIRE_RENDER, "openscad/admesh not installed (set RACKPARTS_REQUIRE_RENDER=1 "
                                                   "to make this a failure)")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HAVE_TOOLS, "openscad and admesh are required for render checks")

    def test_examples_render_and_pass_spec_check(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in EXAMPLES:
                m, _ = _render(self, d, load(ex), ex[:-5])
                self.assertIn("print_orientations", m)
                r = cli("spec-check", os.path.join(d, ex[:-5]))
                self.assertEqual(r.returncode, 0, ex + r.stdout[-2000:] + r.stderr)
                rep = json.loads(r.stdout)
                self.assertTrue(rep["wall_thickness"]["ok"])
                self.assertTrue(rep["m6_cage_nut"]["applies"])
                self.assertEqual(rep["m6_cage_nut"]["eia310_nominal"]["vertical_offsets_mm"], [0.0, 0.0])
            for case in boundary_cases():
                _render(self, d, case, case["name"])

    def test_shelf_front_face_down_lip_is_reported(self):
        # floor down passes; front face down leaves the full-height rear lip as a ledge off the floor
        with tempfile.TemporaryDirectory() as d:
            _render(self, d, load(EXAMPLES[2]), "shelf")
            rep = speccheck.check_dir(os.path.join(d, "shelf"))
        o = rep["overhangs"]
        self.assertEqual(o["passing"], ["floor_down"])
        ffd = o["by_orientation"]["front_face_down"]
        self.assertAlmostEqual(ffd["worst_unsupported_ledge_mm"], 12.0, delta=0.6)  # lip height, sampled

    def test_known_overhangs_in_existing_parts_fail(self):
        # Both are described in the README as needing supports or good bridging.
        with tempfile.TemporaryDirectory() as d:
            for ex in ("switch-cradle-synthetic.json", "sff-cradle-synthetic.json"):
                _render(self, d, load(ex), ex[:-5])
                r = cli("spec-check", os.path.join(d, ex[:-5]))
                self.assertEqual(r.returncode, 5, ex)
                rep = json.loads(r.stdout)
                self.assertFalse(rep["overhangs"]["ok"])
                self.assertTrue(rep["wall_thickness"]["ok"])
                self.assertFalse(rep["m6_cage_nut"]["applies"])

    def test_wall_and_m6_failures_are_detected(self):
        blank = load("blank-19in-1u-synthetic.json")
        with tempfile.TemporaryDirectory() as d:
            _render(self, d, blank, "blank")
            out = os.path.join(d, "blank")
            with open(os.path.join(out, "manifest.json")) as f:
                man = json.load(f)
            tris = mesh.read_stl(os.path.join(out, man["files"]["stl"]["path"]))
            self.assertTrue(speccheck.check(tris, man)["ok"])
            # material above a 6.6 mm hole centred 5.955 mm below the top of a 43.66 mm panel: 2.648 mm
            rep = speccheck.check(tris, dict(man, input={"parameters": dict(blank, min_wall_mm=3.0)}))
            self.assertAlmostEqual(rep["wall_thickness"]["thinnest_mm"], 43.66 - 37.705 - 6.6141 / 2, delta=0.01)
            self.assertFalse(rep["wall_thickness"]["ok"])
            # manifest claims a bigger hole than the mesh has
            rep = speccheck.check(tris, dict(man, input={"parameters": dict(blank, rail_screw_clearance_diameter_mm=7.0)}))
            self.assertTrue(any("measures 6.6" in p for p in rep["m6_cage_nut"]["problems"]), rep["m6_cage_nut"])
            self.assertTrue(speccheck.check(tris, man, fastener="none")["ok"])
            # tampered STL: refused before checking
            with open(os.path.join(out, man["files"]["stl"]["path"]), "r+b") as f:
                f.seek(100)
                f.write(b"\x01")
            r = cli("spec-check", out)
            self.assertEqual(r.returncode, 2)
            self.assertIn("sha256", r.stderr)
            # valid inputs that a plain panel accepts but an M6 cage nut does not
            _render(self, d, dict(blank, rail_screw_clearance_diameter_mm=6.2), "tight")
            _render(self, d, dict(blank, rail_hole_z_offsets_mm=[6.6, 37.705]), "offset")
            _render(self, d, dict(blank, rail_hole_center_spacing_mm=470.0, rail_slot_extra_length_mm=0.0), "wide")
            for label, needle in (("tight", "outside the M6 clearance"), ("offset", "EIA-310 nominal hole"),
                                  ("wide", "per side off")):
                r = cli("spec-check", os.path.join(d, label))
                self.assertEqual(r.returncode, 5, label)
                self.assertIn(needle, " ".join(json.loads(r.stdout)["m6_cage_nut"]["problems"]), label)

    def test_bridge_and_ledge_limits(self):
        with tempfile.TemporaryDirectory() as d:
            bridge = _scad_mesh(self, d, "cube([5,5,10]); translate([35,0,0]) cube([5,5,10]); "
                                         "translate([0,0,10]) cube([40,5,3]);")
            rep = speccheck.overhangs(speccheck.Mesh(bridge), (0, 0, 1))
            self.assertFalse(rep["ok"])                      # 30 mm bridge > 20
            rep = speccheck.overhangs(speccheck.Mesh(bridge), (0, 0, 1), max_bridge_mm=30.5)
            self.assertTrue(rep["ok"])
            self.assertAlmostEqual(rep["longest_bridge_mm"], 30.0, delta=0.01)
            ledge = _scad_mesh(self, d, "cube([5,20,20]); translate([5,0,17]) cube([3,20,3]);")
            self.assertFalse(speccheck.overhangs(speccheck.Mesh(ledge), (0, 0, 1))["ok"])
            rep = speccheck.overhangs(speccheck.Mesh(ledge), (0, 0, 1), max_ledge_mm=3.0)
            self.assertTrue(rep["ok"])
            # steep but not flat: 10 mm out over 9.5 mm rise exceeds 45 degrees by only 0.5 mm of reach
            slope = _scad_mesh(self, d, "rotate([90,0,0]) linear_extrude(20) "
                                        "polygon([[0,0],[5,0],[15,9.5],[15,12],[0,12]]);")
            rep = speccheck.overhangs(speccheck.Mesh(slope), (0, 0, 1))
            self.assertTrue(rep["ok"], rep)
            self.assertEqual(rep["overhang_regions"], 1)
            self.assertLessEqual(rep["worst_unsupported_ledge_mm"], 0.5 + 1e-3)
            flat = _scad_mesh(self, d, "rotate([90,0,0]) linear_extrude(20) "
                                       "polygon([[0,0],[5,0],[15,2],[15,4],[0,4]]);")
            self.assertFalse(speccheck.overhangs(speccheck.Mesh(flat), (0, 0, 1))["ok"])

    def test_label_window_pocket_and_lacing_tunnels_are_open(self):
        lab, lac = load("label-19in-1u-synthetic.json"), load("lacing-19in-1u-synthetic.json")
        gl, gb = validate.validate(lab)[1], validate.validate(lac)[1]
        with tempfile.TemporaryDirectory() as d:
            ml = speccheck.Mesh(mesh.read_stl(_render(self, d, lab, "label")[1]))
            mb = speccheck.Mesh(mesh.read_stl(_render(self, d, lac, "lacing")[1]))
        x0, x1, z0, z1, wx0, wz0, wz1, ex0, ft, depth = gl["args"]["label"]
        back = ft + depth
        hit = lambda m, x, z, y=-1.0: m.first_hit((x, y, z), (0.0, 1.0, 0.0), 50.0)  # noqa: E731
        # through the window and the entry the first surface is the pocket back; over a lip it is the front face
        self.assertAlmostEqual(hit(ml, (wx0 + x1) / 2, (wz0 + wz1) / 2), 1 + back, places=3)
        self.assertAlmostEqual(hit(ml, (ex0 + x1) / 2, (z0 + wz0) / 2), 1 + back, places=3)
        self.assertAlmostEqual(hit(ml, (wx0 + ex0) / 2, (z0 + wz0) / 2), 1.0, places=3)
        self.assertAlmostEqual(hit(ml, (x0 + wx0) / 2, (wz0 + wz1) / 2), 1.0, places=3)
        # behind the lip the pocket is open: from inside the pocket, a ray up the lip band hits the pocket top
        up = ml.first_hit(((wx0 + ex0) / 2, ft + depth / 2, z0 + 0.01), (0.0, 0.0, 1.0), 50.0)
        self.assertAlmostEqual(up, z1 - z0 - 0.01, places=3)
        a = gb["args"]
        zc = a["tie_z"] + a["tie_h"] / 2
        y0 = a["t"] + a["gap"] - 0.5
        for x in a["ties"]:           # a tie passes straight through the bar
            self.assertIsNone(hit(mb, x, zc, y0), x)
        for x in a["arms"][1:]:       # beside the tunnels the bar is solid
            self.assertAlmostEqual(hit(mb, x - a["arm_w"] / 2 - 0.3, zc, y0), 0.5, places=3)


class DocTests(unittest.TestCase):
    def test_readme(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for ex in EXAMPLES:
            self.assertIn("examples/" + ex, readme)
        for part in ("lacing_bar", "label_panel"):
            self.assertIn(part, spec.PARTS)
            self.assertIn("`%s`" % part, readme)
        self.assertIn("spec-check", readme)
        self.assertNotIn("deferred", readme)


if __name__ == "__main__":
    unittest.main()
