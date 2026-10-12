"""Tests for rack_shelf and pdu_mount.

Render tests skip when OpenSCAD/ADMesh are missing, unless RACKPARTS_REQUIRE_RENDER=1 (set in CI), in
which case missing tools are a failure: a skipped render check never counts as a pass there.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rackparts import mesh, spec, validate  # noqa: E402
from tests.test_families import _render, why  # noqa: E402
from tests.test_rackparts import EX, FX, cli, load, refused  # noqa: E402

SHELF_EXAMPLES = ["shelf-10in-1u-synthetic.json", "shelf-10in-2u-synthetic.json"]
PDU_EXAMPLES = ["pdu-strip-synthetic.json", "pdu-compact-synthetic.json"]
EXAMPLES = SHELF_EXAMPLES + PDU_EXAMPLES
REQUIRE_RENDER = os.environ.get("RACKPARTS_REQUIRE_RENDER") == "1"
HAVE_TOOLS = bool(shutil.which("openscad") and shutil.which("admesh"))
LIST_KEYS_MAY_BE_EMPTY = ("top_face_keepouts_mm", "side_face_keepouts_mm", "base_face_keepouts_mm")


class ExampleTests(unittest.TestCase):
    def test_examples_are_valid_and_distinct(self):
        for name in EXAMPLES:
            self.assertIsNone(refused(load(name)), name)
        # the two examples of each family differ in shape, not only in name
        for a, b in (SHELF_EXAMPLES, PDU_EXAMPLES):
            ga = validate.validate(load(a))[1]["expected_bbox"]
            gb = validate.validate(load(b))[1]["expected_bbox"]
            self.assertNotEqual(ga, gb)

    def test_examples_say_unvalidated(self):
        for name in EXAMPLES:
            geo = validate.validate(load(name))[1]
            self.assertIn("UNVALIDATED", geo["physical_use"]["status"])
            self.assertTrue(any(s.startswith("Fit coupon") for s in geo["physical_use"]["required_before_use"]))
            self.assertIn("UNVALIDATED", geo["print_orientation_suggestion"])

    def test_pdu_derived_brackets(self):
        geo = validate.validate(load("pdu-strip-synthetic.json"))[1]
        self.assertEqual(geo["derived"]["brackets_to_print"], 2)
        self.assertEqual(geo["derived"]["bracket_spans_along_strip_mm"], [[22.0, 46.0], [148.0, 172.0]])


class HostileTests(unittest.TestCase):
    def test_every_key_with_hostile_values(self):
        for ex in EXAMPLES:
            base = load(ex)
            for key in base:
                for bad in ([], {}, None, "x", True, False, 10 ** 400, -10 ** 400, 2 ** 64, 1e308, -1e308,
                            [[[]]], ["1"], [None], [[1, 2, 3]], [[1, 2, 3, 4, 5]], [1.0], [1.0] * 5, [[1.0] * 4] * 13,
                            "$(touch pwned)", "../../etc/passwd", "a\nb"):
                    if key == "name" and bad == "x":
                        continue  # a valid name
                    if key in LIST_KEYS_MAY_BE_EMPTY and bad == []:
                        continue  # an explicitly empty keepout list is valid
                    with self.subTest(ex=ex, key=key, bad=repr(bad)[:40]):
                        self.assertTrue(refused(dict(base, **{key: bad})))

    def test_malformed_part_and_cross_family_keys(self):
        s, p = load(SHELF_EXAMPLES[0]), load(PDU_EXAMPLES[0])
        for part in ("Rack_shelf", "rack_shelf ", "rack-shelf", "pdu", "pdu_mount\n", 0):
            self.assertTrue(refused(dict(s, part=part)), repr(part))
        self.assertIn("missing required key", why(dict(s, part="pdu_mount")))
        self.assertIn("unknown key", why(dict(p, part="rack_shelf")))
        self.assertIn("unknown key", why(dict(s, part="blanking_panel")))  # shelf keys are not panel keys

    def test_integer_count_is_strict(self):
        s = load(SHELF_EXAMPLES[0])
        for v in (3.0, 41, -1, True, "3"):
            self.assertTrue(refused(dict(s, vent_slot_count=v)), repr(v))

    def test_cli_malformed_json_writes_nothing(self):
        for ex in EXAMPLES:
            with open(os.path.join(EX, ex)) as f:
                text = f.read()
            cases = [
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": NaN'),
                text.replace('"min_wall_mm": 2.0', '"min_wall_mm": 1e400'),
                text.replace('"facets": 48', '"facets": 48, "facets": 48'),
                text.replace('"name": "', '"name": "../'),
                text[: len(text) // 2],
                "[" * 10 + "]" * 10,
            ]
            for i, bad in enumerate(cases):
                with self.subTest(ex=ex, case=i), tempfile.TemporaryDirectory() as d:
                    src = os.path.join(d, "in.json")
                    with open(src, "w") as f:
                        f.write(bad)
                    out = os.path.join(d, "out")
                    r = cli("generate", src, "--out", out)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertNotIn("Traceback", r.stderr)
                    self.assertFalse(os.path.exists(out))

    def test_new_fixtures_refused_without_output(self):
        for name in ("deferred-19in-shelf.json", "oversize-bed-shelf.json", "tall-device-shelf.json",
                     "outlet-keepout-pdu.json", "cord-end-pdu.json"):
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out")
                r = cli("generate", os.path.join(FX, name), "--out", out)
                self.assertEqual(r.returncode, 2, name + r.stderr)
                self.assertFalse(os.path.exists(out), name)

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.mkdir(out)
            with open(os.path.join(out, "keep.txt"), "w") as f:
                f.write("mine")
            for ex in EXAMPLES:
                r = cli("generate", os.path.join(EX, ex), "--out", out, "--scad-only")
                self.assertEqual(r.returncode, 3, r.stderr)
            self.assertEqual(os.listdir(out), ["keep.txt"])


class ShelfRefusals(unittest.TestCase):
    def setUp(self):
        self.s = load("shelf-10in-1u-synthetic.json")
        self.s2 = load("shelf-10in-2u-synthetic.json")

    def test_19in_is_deferred_not_generated(self):
        with open(os.path.join(FX, "deferred-19in-shelf.json")) as f:
            d = json.load(f)
        self.assertIn("deferred", why(d))
        # even on a hypothetical huge printer
        self.assertIn("deferred", why(dict(d, build_plate_x_mm=1000.0, build_plate_y_mm=1000.0,
                                           build_plate_z_mm=1000.0)))

    def test_rails(self):
        self.assertIn("sanity window", why(dict(self.s, rail_hole_center_spacing_mm=465.1)))
        self.assertIn("must exceed rail_opening", why(dict(self.s, rail_opening_width_mm=240.0)))
        self.assertIn("panel edge", why(dict(self.s, rail_hole_z_offsets_mm=[3.0])))
        self.assertIn("obstruct the rail screw", why(dict(self.s, rail_opening_width_mm=232.0,
                                                          rail_side_clearance_mm=0.0)))

    def test_structure(self):
        self.assertIn("rise above the front plate", why(dict(self.s, side_wall_height_mm=41.0)))
        self.assertIn("rear_lip_height_mm", why(dict(self.s, rear_lip_height_mm=21.0)))
        self.assertIn("at least min_wall_mm", why(dict(self.s, floor_thickness_mm=1.5)))
        self.assertIn("rack_clear_depth_mm", why(dict(self.s, rack_clear_depth_mm=153.9)))
        self.assertIsNone(refused(dict(self.s, rack_clear_depth_mm=154.0)))  # boundary: exactly fits

    def test_device_must_fit_inside_shelf(self):
        self.assertIn("device does not fit", why(dict(self.s, device_width_mm=209.1)))     # 211 - 2 x 1
        self.assertIsNone(refused(dict(self.s, device_width_mm=209.0)))
        self.assertIn("device does not fit", why(dict(self.s, device_depth_mm=146.5)))     # 147 usable - 1 clr
        self.assertIsNone(refused(dict(self.s, device_depth_mm=146.0)))
        self.assertIn("rack space above", why(dict(self.s, device_height_mm=40.7)))
        # without a rear lip the full floor depth is usable and no rear clearance applies
        self.assertIsNone(refused(dict(self.s2, device_depth_mm=220.0)))
        self.assertIn("device does not fit", why(dict(self.s2, device_depth_mm=220.1)))

    def test_vents(self):
        self.assertIn("between them", why(dict(self.s, vent_slot_pitch_mm=6.5)))
        self.assertIn("fits between the side walls", why(dict(self.s, vent_slot_count=20)))
        self.assertIn("front plate / rear edge", why(dict(self.s, vent_slot_length_mm=144.0)))
        self.assertIn("at least vent_slot_width", why(dict(self.s, vent_slot_length_mm=4.0)))
        self.assertIsNone(refused(dict(self.s, vent_slot_count=0, vent_slot_length_mm=4.0)))  # unused when 0

    def test_orientation_text_lists_overhangs(self):
        # Only bed fit is checked; no orientation may be described as support-free.
        for d in (self.s, self.s2):
            text = validate.validate(d)[1]["print_orientation_suggestion"]
            self.assertNotIn("no support", text.lower())
            self.assertNotIn("support-free", text.lower())
        text = validate.validate(self.s)[1]["print_orientation_suggestion"]
        self.assertIn("rear lip becomes a 211.0 mm long horizontal bridge", text)
        self.assertIn("vent slots become vertical slots with rounded overhanging tops", text)
        self.assertNotIn("horizontal slots", text)
        self.assertNotIn("rear lip", validate.validate(self.s2)[1]["print_orientation_suggestion"])  # no lip
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        self.assertNotIn("support-free", readme)
        self.assertIn("neither orientation is claimed to print without supports", readme)

    def test_build_volume(self):
        self.assertIn("build volume", why(dict(self.s, build_plate_x_mm=250.0, build_plate_y_mm=250.0)))
        # front-face-down needs height t + D: too short a printer, but floor-down still fits
        _, geo = validate.validate(dict(self.s, build_plate_z_mm=100.0))
        self.assertEqual(geo["derived"]["printable_orientations"], ["floor_down"])
        # floor-down needs 254 x 154 footprint; a 254 x 60 plate only allows front-face-down
        _, geo = validate.validate(dict(self.s, build_plate_x_mm=254.0, build_plate_y_mm=60.0))
        self.assertEqual(geo["derived"]["printable_orientations"], ["front_face_down"])
        self.assertIn("build volume", why(dict(self.s, build_plate_x_mm=254.0, build_plate_y_mm=60.0,
                                                build_plate_z_mm=150.0)))


class PduRefusals(unittest.TestCase):
    def setUp(self):
        self.p = load("pdu-strip-synthetic.json")
        self.c = load("pdu-compact-synthetic.json")

    def test_keepouts_on_every_face(self):
        self.assertIn("top_face_keepouts_mm[0]", why(dict(self.p, bracket_x_positions_mm=[34.0, 100.0])))
        self.assertIn("side_face_keepouts_mm[0]", why(dict(self.p, bracket_x_positions_mm=[34.0, 225.0])))
        self.assertIn("base_face_keepouts_mm[0]", why(dict(self.p, bracket_x_positions_mm=[30.0, 160.0])))

    def test_keepout_margin_is_a_boundary(self):
        # compact: outlets start at x=30 with a 4 mm margin; bracket 0 ends at 23 (gap 7)
        self.assertIsNone(refused(dict(self.c, keepout_margin_mm=7.0)))
        self.assertIn("keepout violated", why(dict(self.c, keepout_margin_mm=7.01)))

    def test_ends_and_spacing(self):
        self.assertIsNone(refused(dict(self.c, end_clearance_mm=5.0)))   # exactly at both ends
        self.assertIn("cord/end clearance", why(dict(self.c, end_clearance_mm=5.01)))
        self.assertIn("cord/end clearance", why(dict(self.c, bracket_x_positions_mm=[8.0, 146.0])))
        self.assertIn("overlap along the strip", why(dict(self.p, top_face_keepouts_mm=[],
                                                           bracket_x_positions_mm=[150.0, 160.0])))
        self.assertIn("bracket_x_positions_mm", why(dict(self.p, bracket_x_positions_mm=[34.0])))  # one pivots

    def test_keepout_rectangles(self):
        self.assertIn("outside the", why(dict(self.p, top_face_keepouts_mm=[[55.0, 140.0, 5.0, 56.0]])))
        self.assertIn("outside the", why(dict(self.p, side_face_keepouts_mm=[[200.0, 301.0, 10.0, 30.0]])))
        self.assertIn("lo < hi", why(dict(self.p, base_face_keepouts_mm=[[15.0, 5.0, 5.0, 50.0]])))

    def test_strap_and_screws(self):
        self.assertIn("strap slot", why(dict(self.p, strap_width_mm=20.0)))
        self.assertIn("unsafe screw clearance", why(dict(self.p, mount_screw_head_diameter_mm=12.5)))
        self.assertIn("in the ear", why(dict(self.p, mount_screw_clearance_diameter_mm=8.5,
                                             mount_screw_head_diameter_mm=10.0)))
        self.assertIn("at least mount_screw_clearance", why(dict(self.p, mount_screw_head_diameter_mm=4.0)))
        self.assertIn("min_wall_mm", why(dict(self.p, strap_bar_height_mm=1.5)))

    def test_envelope_and_plate(self):
        self.assertIn("max_overall_width_mm", why(dict(self.p, max_overall_width_mm=86.5)))
        self.assertIsNone(refused(dict(self.p, max_overall_height_mm=52.8)))     # includes strap over bar
        self.assertIn("strap looped", why(dict(self.p, max_overall_height_mm=52.7)))
        self.assertIn("build volume", why(dict(self.p, build_plate_z_mm=50.0)))
        self.assertIn("build volume", why(dict(self.p, build_plate_x_mm=80.0, build_plate_y_mm=80.0)))


def boundary_cases():
    """Small/large/boundary variants that must still render to one clean shell."""
    s, s2 = load("shelf-10in-1u-synthetic.json"), load("shelf-10in-2u-synthetic.json")
    p, c = load("pdu-strip-synthetic.json"), load("pdu-compact-synthetic.json")
    shelf_min = dict(s, name="shelf-min-plain", shelf_depth_mm=40.0, rack_clear_depth_mm=44.0, side_wall_height_mm=3.0,
                     rear_lip_height_mm=0.0, vent_slot_count=0, device_depth_mm=40.0, device_height_mm=20.0,
                     rail_slot_extra_length_mm=0.0, build_plate_x_mm=254.0, build_plate_y_mm=50.0,
                     build_plate_z_mm=44.0)
    # vents exactly min_wall_mm from the front plate and the rear lip; lip as tall as the walls
    shelf_tight_vents = dict(s, name="shelf-tight-vents", vent_slot_length_mm=143.0, rear_lip_height_mm=20.0,
                             vent_slot_count=19, vent_slot_pitch_mm=11.0)
    shelf_deep = dict(s2, name="shelf-deep-2u", shelf_depth_mm=250.0, rack_clear_depth_mm=254.0,
                      vent_slot_length_mm=240.0, device_depth_mm=250.0)
    pdu_four = dict(p, name="pdu-four-brackets", top_face_keepouts_mm=[[60.0, 120.0, 5.0, 50.0]],
                    side_face_keepouts_mm=[], base_face_keepouts_mm=[], keepout_margin_mm=0.0,
                    bracket_x_positions_mm=[34.0, 132.0, 200.0, 266.0], bracket_length_mm=24.0)
    pdu_min = dict(c, name="pdu-min", bracket_length_mm=14.8, strap_width_mm=10.0, strap_clearance_mm=0.8,
                   ear_width_mm=7.6, mount_screw_clearance_diameter_mm=3.4, mount_screw_head_diameter_mm=7.6,
                   base_thickness_mm=2.0, wall_thickness_mm=2.0, strap_bar_height_mm=2.0)
    return [shelf_min, shelf_tight_vents, shelf_deep, pdu_four, pdu_min]


class BoundaryValidityTests(unittest.TestCase):
    def test_boundary_cases_validate(self):
        for d in boundary_cases():
            self.assertIsNone(refused(d), d["name"])

    def test_tight_vents_are_a_boundary(self):
        d = boundary_cases()[1]
        self.assertIn("front plate / rear edge", why(dict(d, vent_slot_length_mm=143.01)))
        self.assertIn("fits between the side walls", why(dict(d, vent_slot_count=20)))

    def test_min_pdu_is_a_boundary(self):
        d = boundary_cases()[4]
        self.assertIn("strap slot", why(dict(d, bracket_length_mm=14.79)))


@unittest.skipUnless(HAVE_TOOLS or REQUIRE_RENDER, "openscad/admesh not installed (set RACKPARTS_REQUIRE_RENDER=1 "
                                                   "to make this a failure)")
class RenderTests(unittest.TestCase):
    def setUp(self):
        # Under RACKPARTS_REQUIRE_RENDER a missing tool fails here instead of skipping.
        self.assertTrue(HAVE_TOOLS, "openscad and admesh are required for render checks")

    def test_examples_and_boundaries_render_to_clean_meshes(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in EXAMPLES:
                m, _ = _render(self, d, load(ex), ex[:-5])
                self.assertIsNotNone(m["mesh"]["admesh"])
                self.assertIn("UNVALIDATED", m["physical_use"]["status"])
            for case in boundary_cases():
                m, _ = _render(self, d, case, case["name"])
                self.assertIsNotNone(m["mesh"]["admesh"])

    def test_shelf_device_space_is_empty_and_vents_cut(self):
        cfg = load("shelf-10in-1u-synthetic.json")
        _, geo = validate.validate(cfg)
        a = geo["args"]
        with tempfile.TemporaryDirectory() as d:
            m, stl = _render(self, d, cfg, "shelf")
            verts = {v for t in mesh.read_stl(stl) for v in t}
        inner = a["half"] - a["t_wall"]
        y0, y1 = a["t"], a["t"] + a["depth"] - a["t_wall"]
        # nothing above the floor between the walls and in front of the lip (the device's space)
        inside = [v for v in verts if abs(v[0]) < inner - 1e-3 and y0 + 1e-3 < v[1] < y1 - 1e-3
                  and v[2] > a["t_floor"] + 1e-3]
        self.assertEqual(inside, [])
        # vents go through the floor: vertices on both floor faces at each vent's x extremes
        for vx, _vy0, _vy1 in a["vents"]:
            for z in (0.0, a["t_floor"]):
                self.assertTrue(any(abs(v[0] - (vx - a["vent_w"] / 2)) < 0.01 and abs(v[2] - z) < 1e-3
                                    for v in verts), (vx, z))
        # less material than the solid envelope of plate + floor + walls + lip
        solid = (a["w"] * a["t"] * a["h"] + 2 * a["half"] * a["depth"] * a["t_floor"]
                 + 2 * a["t_wall"] * a["depth"] * a["wall_h"] + (2 * a["half"] - 2 * a["t_wall"]) * a["t_wall"] * a["lip_h"])
        self.assertLess(m["mesh"]["volume_mm3"], solid)

    def test_pdu_strip_channel_is_empty_and_strap_above_top(self):
        cfg = load("pdu-strip-synthetic.json")
        _, geo = validate.validate(cfg)
        a = geo["args"]
        with tempfile.TemporaryDirectory() as d:
            m, stl = _render(self, d, cfg, "pdu")
            verts = {v for t in mesh.read_stl(stl) for v in t}
        # the strip's channel (between the walls, above the base) has no material at all
        inside = [v for v in verts if abs(v[1]) < a["inner_w"] / 2 - 1e-3 and v[2] > a["t_base"] + 1e-3]
        self.assertEqual(inside, [])
        # slot bottom edge sits at strip top + clearance, i.e. the strap cannot press on a side face
        top = a["t_base"] + cfg["device_height_mm"]
        slot_zs = sorted({round(v[2], 3) for v in verts if abs(abs(v[0]) - a["slot_len"] / 2) < 1e-3})
        self.assertIn(round(a["slot_z0"], 3), slot_zs)
        self.assertGreaterEqual(min(z for z in slot_zs if z > a["t_base"] + 1e-3), top - 1e-3)
        # hand-computed volume: base + walls - slots - holes (polygonal holes)
        import math
        L, ow = a["length"], a["inner_w"] + 2 * a["t_wall"] + 2 * a["ear"]
        n = cfg["facets"]
        hole = 0.5 * n * (a["hole_d"] / 2) ** 2 * math.sin(2 * math.pi / n) * a["t_base"]
        vol = (L * ow * a["t_base"] + 2 * L * a["t_wall"] * (a["wall_h"] - a["t_base"])
               - 2 * a["slot_len"] * a["t_wall"] * a["slot_h"] - 2 * hole)
        self.assertAlmostEqual(m["mesh"]["volume_mm3"], vol, delta=0.05)


class DocTests(unittest.TestCase):
    def test_readme(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for ex in EXAMPLES:
            self.assertIn("examples/" + ex, readme)
        for part in ("rack_shelf", "pdu_mount"):
            self.assertIn(part, spec.PARTS)
            self.assertIn("`%s`" % part, readme)
        self.assertNotIn("gyrid", readme)


if __name__ == "__main__":
    unittest.main()
