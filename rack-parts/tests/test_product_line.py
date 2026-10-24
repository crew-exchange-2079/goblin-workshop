"""Tests for the product-line additions: keystone_panel, rack_drawer, label_holder, cable_comb,
cartridge_holder; the per-part spec checks; the 3MF/PNG exports; and the catalog page.

Render tests skip when OpenSCAD/ADMesh are missing, unless RACKPARTS_REQUIRE_RENDER=1 (set in CI), in
which case missing tools are a failure.
"""

import glob
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rackparts import catalog, export, generate, mesh, spec, speccheck, validate  # noqa: E402
from tests.test_families import _render, why  # noqa: E402
from tests.test_rackparts import EX, FX, _box_stl, cli, load, refused  # noqa: E402

NEW_PARTS = ("keystone_panel", "rack_drawer", "label_holder", "cable_comb", "cartridge_holder")
EXAMPLES = ["keystone-10in-1u-synthetic.json", "keystone-19in-1u-synthetic.json",
            "drawer-10in-2u-housing-synthetic.json", "drawer-10in-2u-drawer-synthetic.json",
            "label-card-ears-synthetic.json", "label-strip-adhesive-synthetic.json",
            "comb-8x6mm-snap-synthetic.json", "comb-12x3mm-open-synthetic.json",
            "cartridge-tape-5-synthetic.json", "cartridge-sfp-12-synthetic.json"]
FIXTURES = ["overlap-keystone-panel.json", "thick-latch-keystone-panel.json", "deferred-19in-drawer.json",
            "screw-cover-drawer.json", "missing-retention-label.json", "thin-finger-comb.json",
            "buried-item-cartridge.json"]
REQUIRE_RENDER = os.environ.get("RACKPARTS_REQUIRE_RENDER") == "1"
HAVE_TOOLS = bool(shutil.which("openscad") and shutil.which("admesh"))
DOCS = os.path.join(os.path.dirname(ROOT), "docs", "rack-parts")


class ExampleTests(unittest.TestCase):
    def test_examples_valid_and_two_per_new_part(self):
        parts = {}
        for name in EXAMPLES:
            d = load(name)
            self.assertIsNone(refused(d), name)
            parts.setdefault(d["part"], []).append(name)
        self.assertEqual(sorted(parts), sorted(NEW_PARTS))
        for part, names in parts.items():
            self.assertEqual(len(names), 2, part)

    def test_drawer_files_differ_only_in_component_and_name(self):
        h, d = load(EXAMPLES[2]), load(EXAMPLES[3])
        self.assertEqual((h["component"], d["component"]), ("housing", "drawer"))
        strip = lambda x: {k: v for k, v in x.items() if k not in ("component", "name")}  # noqa: E731
        self.assertEqual(strip(h), strip(d))
        # both components are derived from the same numbers
        self.assertEqual(validate.validate(h)[1]["derived"], validate.validate(d)[1]["derived"])

    def test_twelve_parts(self):
        self.assertEqual(len(spec.PARTS), 12)
        for part in NEW_PARTS:
            self.assertIn(part, spec.PARTS)


class HostileTests(unittest.TestCase):
    def test_every_key_with_hostile_values(self):
        for ex in EXAMPLES:
            base = load(ex)
            for key in base:
                for bad in ([], {}, None, "x", True, False, 10 ** 400, -10 ** 400, 2 ** 64, 1e308, -1e308,
                            [[[]]], ["1"], [None], [[1, 2, 3]], [1.0] * 9, "$(touch pwned)", "../../etc/passwd",
                            "a\nb"):
                    if key == "name" and bad == "x":
                        continue  # a valid name
                    if key == "allow_oversize_print" and isinstance(bad, bool):
                        continue
                    with self.subTest(ex=ex, key=key, bad=repr(bad)[:40]):
                        self.assertTrue(refused(dict(base, **{key: bad})))

    def test_integers_and_enums_are_strict(self):
        k, d = load(EXAMPLES[0]), load(EXAMPLES[3])
        c, h = load(EXAMPLES[6]), load(EXAMPLES[8])
        for v in (3.0, True, "3", -1):
            self.assertTrue(refused(dict(k, keystone_count=v)), repr(v))
            self.assertTrue(refused(dict(d, divider_count=v)), repr(v))
            self.assertTrue(refused(dict(c, slot_count=v)), repr(v))
            self.assertTrue(refused(dict(h, rows=v)), repr(v))
        for v in ("Housing", "box", "", "drawer ", None, 1):
            self.assertTrue(refused(dict(d, component=v)), repr(v))
        self.assertIn("unknown key", why(dict(d, part="rack_shelf")))
        self.assertIn("missing required key", why(dict(load("blank-10in-1u-synthetic.json"), part="keystone_panel")))

    def test_fixtures_refused_by_cli_without_output(self):
        for name in FIXTURES:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out")
                r = cli("generate", os.path.join(FX, name), "--out", out)
                self.assertEqual(r.returncode, 2, name + r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.assertFalse(os.path.exists(out))

    def test_scad_contains_only_validated_numbers(self):
        for ex in EXAMPLES:
            cfg, geo = validate.validate(load(ex))
            src = generate.scad_source(cfg, geo)
            call = src[src.rindex("\n%s(" % geo["module"]):]
            self.assertNotIn('"', call)


class KeystoneRefusals(unittest.TestCase):
    def setUp(self):
        self.k = load("keystone-10in-1u-synthetic.json")

    def test_pitch_is_a_boundary(self):
        # pocket = 16 + 2 x 0.3 = 16.6 wide; min wall 2
        self.assertIsNone(refused(dict(self.k, keystone_pitch_mm=18.6)))
        self.assertIn("between back pockets", why(dict(self.k, keystone_pitch_mm=18.59)))
        # without a pocket the cutouts set the spacing
        flat = dict(self.k, jack_latch_panel_thickness_mm=4.0)
        self.assertIsNone(refused(dict(flat, keystone_pitch_mm=16.9)))
        self.assertIn("between cutouts", why(dict(flat, keystone_pitch_mm=16.89)))

    def test_row_stays_in_the_rail_opening(self):
        self.assertIn("inside the rail opening", why(dict(self.k, keystone_count=12)))
        self.assertIn("panel edges/stiffeners", why(dict(self.k, keystone_row_z_mm=[14.0])))
        self.assertIn("between them", why(dict(self.k, panel_height_mm=88.1, keystone_row_z_mm=[21.83, 42.0])))
        self.assertIsNone(refused(dict(self.k, panel_height_mm=88.1, keystone_row_z_mm=[21.83, 45.43])))

    def test_jack_measurements(self):
        self.assertIn("thicker than panel_thickness_mm", why(dict(self.k, jack_latch_panel_thickness_mm=4.01)))
        self.assertIn("smaller than its cutout", why(dict(self.k, jack_body_height_mm=19.0)))
        # ledge past the cutout's top/bottom edge: (21 + 0.6 - 19.4) / 2 = 1.1; needs >= 0.5
        self.assertIn("latch would hook", why(dict(self.k, jack_body_height_mm=19.4, jack_clearance_mm=0.49)))
        self.assertIsNone(refused(dict(self.k, jack_body_height_mm=19.4, jack_clearance_mm=0.5)))

    def test_build_plate(self):
        k19 = load("keystone-19in-1u-synthetic.json")
        self.assertIn("build plate", why(dict(k19, allow_oversize_print=False)))
        self.assertFalse(validate.validate(k19)[1]["derived"]["fits_build_plate"])


class DrawerRefusals(unittest.TestCase):
    def setUp(self):
        self.h = load("drawer-10in-2u-housing-synthetic.json")
        self.d = load("drawer-10in-2u-drawer-synthetic.json")

    def test_19in_deferred(self):
        with open(os.path.join(FX, "deferred-19in-drawer.json")) as f:
            self.assertIn("deferred", why(json.load(f)))

    def test_front_may_not_cover_screw_heads_or_rise_above_panel(self):
        # screw-head limit: 236.5/2 - 1 - 6 - 2 = 109.25; front half-width = 106.1 + overlap
        self.assertIsNone(refused(dict(self.d, front_overlap_mm=3.15)))
        self.assertIn("rail screw heads", why(dict(self.d, front_overlap_mm=3.16)))
        self.assertIn("rack space above", why(dict(self.h, sleeve_height_mm=88.2)))
        self.assertIn("into the rack space above", why(dict(self.h, sleeve_height_mm=88.1, front_overlap_mm=3.0)))

    def test_structure_and_fit(self):
        self.assertIn("at least min_wall_mm", why(dict(self.d, drawer_wall_thickness_mm=1.5)))
        self.assertIn("rack_clear_depth_mm", why(dict(self.h, rack_clear_depth_mm=163.9)))
        self.assertIsNone(refused(dict(self.h, rack_clear_depth_mm=164.0)))
        self.assertIn("obstruct the rail screw", why(dict(self.h, rail_opening_width_mm=232.0, rail_side_clearance_mm=0.0)))
        self.assertIn("no usable drawer space", why(dict(self.h, sleeve_height_mm=15.0, drawer_wall_thickness_mm=10.0,
                                                         pull_width_mm=0.0, divider_count=0)))

    def test_pull_and_dividers(self):
        self.assertIn("block it", why(dict(self.d, divider_count=1)))      # a centre divider behind the pull
        self.assertIsNone(refused(dict(self.d, divider_count=1, pull_width_mm=0.0)))
        self.assertIn("must not exceed", why(dict(self.d, pull_width_mm=10.0, pull_height_mm=14.0)))
        self.assertIn("compartments", why(dict(self.d, divider_count=8, pull_width_mm=0.0, drawer_wall_thickness_mm=10.0,
                                               rail_opening_width_mm=100.0)))

    def test_drawer_matches_housing_opening(self):
        g = validate.validate(self.d)[1]
        dv = g["derived"]
        clr = self.d["slide_clearance_mm"]
        self.assertAlmostEqual(dv["drawer_box_mm"][0], dv["opening_mm"][0] - 2 * clr)
        self.assertAlmostEqual(dv["drawer_box_mm"][2], dv["opening_mm"][1] - clr)
        self.assertGreater(dv["drawer_front_mm"][0], dv["opening_mm"][0])     # it stops on the housing


class LabelCombCartridgeRefusals(unittest.TestCase):
    def test_label(self):
        lab = load("label-card-ears-synthetic.json")
        self.assertIsNone(refused(dict(lab, lip_overlap_mm=1.5)))          # 1.5 - 0.3 = 1.2 = min wall
        self.assertIn("missing retention", why(dict(lab, lip_overlap_mm=1.49)))
        self.assertIn("no readable window", why(dict(lab, lip_overlap_mm=19.5)))
        self.assertIn("unsafe screw clearance", why(dict(lab, mount_screw_head_diameter_mm=10.5)))
        self.assertIn("in the ear", why(dict(lab, mount_screw_clearance_diameter_mm=8.0, mount_screw_head_diameter_mm=9.0)))
        self.assertIn("max_overall_width_mm", why(dict(lab, max_overall_width_mm=85.5)))
        strip = load("label-strip-adhesive-synthetic.json")
        # no ears: screw values are not checked
        self.assertIsNone(refused(dict(strip, mount_screw_head_diameter_mm=20.0)))
        self.assertEqual(validate.validate(strip)[1]["args"]["holes_x"], [])

    def test_comb(self):
        c = load("comb-8x6mm-snap-synthetic.json")
        self.assertIn("wider than the seat", why(dict(c, entry_throat_mm=6.7)))
        self.assertIsNone(refused(dict(c, entry_throat_mm=6.6)))
        self.assertIn("fingers", why(dict(c, slot_pitch_mm=8.6)))
        self.assertIn("end_margin_mm", why(dict(c, end_margin_mm=1.9)))
        self.assertIn("do not fit on the", why(dict(c, mount_hole_spacing_mm=82.0)))
        self.assertIn("unsafe screw clearance", why(dict(c, mount_screw_head_diameter_mm=10.5)))
        self.assertIn("max_overall_height_mm", why(dict(c, max_overall_height_mm=20.0)))
        self.assertAlmostEqual(validate.validate(c)[1]["derived"]["snap_in_squeeze_mm"], 0.6)
        self.assertEqual(validate.validate(load("comb-12x3mm-open-synthetic.json"))[1]["derived"]["snap_in_squeeze_mm"], 0.0)

    def test_cartridge(self):
        s = load("cartridge-sfp-12-synthetic.json")
        self.assertIn("buried item", why(dict(s, pocket_depth_mm=57.0)))
        self.assertIsNone(refused(dict(s, pocket_depth_mm=56.9)))
        self.assertIn("floor ledge", why(dict(s, floor_hole_diameter_mm=7.0)))
        self.assertIn("dividers beside", why(dict(s, finger_notch_width_mm=11.7)))
        self.assertIn("at most pocket_depth_mm", why(dict(s, finger_notch_depth_mm=23.0)))
        self.assertIsNone(refused(dict(s, finger_notch_width_mm=0.0, finger_notch_depth_mm=0.0)))
        self.assertIn("max_overall_height_mm", why(dict(s, max_overall_height_mm=58.9)))   # items stand proud
        self.assertIn("build volume", why(dict(s, build_plate_x_mm=60.0, build_plate_y_mm=60.0)))


def boundary_cases():
    """Small/large/boundary variants of the new parts that must still render and pass their spec check."""
    k = load("keystone-10in-1u-synthetic.json")
    d = load("drawer-10in-2u-drawer-synthetic.json")
    lab = load("label-card-ears-synthetic.json")
    c = load("comb-8x6mm-snap-synthetic.json")
    s = load("cartridge-sfp-12-synthetic.json")
    return [
        # no back pocket, no ribs, minimum web between cutouts, single jack
        dict(k, name="keystone-flat-min-web", jack_latch_panel_thickness_mm=4.0, stiffener_depth_mm=0.0,
             keystone_pitch_mm=16.9),
        dict(k, name="keystone-single", keystone_count=1),
        # pocket only 0.1 mm deep (latch seat just under the panel thickness)
        dict(k, name="keystone-shallow-pocket", jack_latch_panel_thickness_mm=3.9),
        # two rows on a 2U panel, rows exactly min_wall_mm apart
        dict(k, name="keystone-2u-two-rows", panel_height_mm=88.1,
             rail_hole_z_offsets_mm=[5.955, 37.705, 50.405, 82.155], keystone_row_z_mm=[33.0, 56.6]),
        dict(d, name="drawer-flush-no-pull", front_overlap_mm=0.0, pull_width_mm=0.0, divider_count=0),
        # a single divider on the centre line (allowed without a pull)
        dict(d, name="drawer-centre-divider", pull_width_mm=0.0, divider_count=1),
        dict(d, name="drawer-housing-max-overlap", component="housing", front_overlap_mm=3.15),
        dict(d, name="drawer-max-overlap", front_overlap_mm=3.15, divider_count=4, pull_width_mm=0.0),
        dict(lab, name="label-min-lip", lip_overlap_mm=1.5),
        dict(c, name="comb-single-seat", slot_count=1, mount_hole_spacing_mm=0.0, finger_length_mm=0.0),
        dict(c, name="comb-u-slots", entry_throat_mm=6.6, slot_pitch_mm=8.62, end_margin_mm=10.0),
        dict(s, name="cartridge-single-plain", columns=1, rows=1, floor_hole_diameter_mm=0.0,
             finger_notch_width_mm=0.0),
        dict(s, name="cartridge-notch-full-depth", finger_notch_depth_mm=22.0, finger_notch_width_mm=11.6),
    ]


class BoundaryValidityTests(unittest.TestCase):
    def test_boundary_cases_validate(self):
        for d in boundary_cases():
            self.assertIsNone(refused(d), d["name"])


class SolidAndExportUnitTests(unittest.TestCase):
    """No OpenSCAD needed: the probe and the writers on a hand-made unit cube."""

    def _box(self, d):
        p = os.path.join(d, "box.stl")
        _box_stl(p)
        return mesh.read_stl(p)

    def test_point_in_mesh(self):
        with tempfile.TemporaryDirectory() as d:
            s = speccheck.Solid(self._box(d))
        self.assertTrue(s.inside((0.5, 0.5, 0.5)))
        self.assertTrue(s.inside((0.01, 0.99, 0.5)))
        for p in ((1.5, 0.5, 0.5), (-0.5, 0.5, 0.5), (0.5, 0.5, 1.01), (0.5, -2, 0.5), (5, 5, 5)):
            self.assertFalse(s.inside(p), p)

    def test_3mf_round_trip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            tris = self._box(d)
            p = os.path.join(d, "box.3mf")
            export.write_3mf(tris, p, "box")
            back = export.read_3mf(p)
            self.assertEqual(mesh.geometry_hash(back), mesh.geometry_hash(tris))
            self.assertTrue(mesh.analyse(back)["ok"])
            with open(p, "rb") as f:
                first = f.read()
            with self.assertRaises(FileExistsError):
                export.write_3mf(tris, p, "box")
            p2 = os.path.join(d, "box2.3mf")
            export.write_3mf(list(reversed(tris)), p2, "box")          # facet order does not matter
            with open(p2, "rb") as f:
                self.assertEqual(f.read(), first)

    def test_png_is_valid_and_order_independent(self):
        with tempfile.TemporaryDirectory() as d:
            tris = self._box(d)
            a, b = os.path.join(d, "a.png"), os.path.join(d, "b.png")
            export.write_png(tris, a, width=64, height=48)
            export.write_png(list(reversed(tris)), b, width=64, height=48)
            with open(a, "rb") as f:
                data = f.read()
            with open(b, "rb") as f:
                self.assertEqual(f.read(), data)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (64, 48))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), h * (1 + 3 * w))
        # the cube is drawn: not every pixel is background
        px = {raw[y * (1 + 3 * w) + 1 + 3 * x: y * (1 + 3 * w) + 4 + 3 * x] for y in range(h) for x in range(w)}
        self.assertGreater(len(px), 2)
        self.assertIn(bytes(export.BACKGROUND), px)


@unittest.skipUnless(HAVE_TOOLS or REQUIRE_RENDER, "openscad/admesh not installed (set RACKPARTS_REQUIRE_RENDER=1 "
                                                   "to make this a failure)")
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(HAVE_TOOLS, "openscad and admesh are required for render checks")

    def _check_outputs(self, m, out):
        self.assertTrue(m["spec_check"]["ok"], m["spec_check"]["problems"])
        self.assertGreater(m["spec_check"]["probes"], 5)
        p3 = os.path.join(out, m["files"]["3mf"]["path"])
        self.assertEqual(mesh.geometry_hash(export.read_3mf(p3)), m["mesh"]["geometry_sha256"])
        self.assertEqual(generate.sha256_file(p3), m["files"]["3mf"]["sha256"])

    def test_examples_and_boundaries_render_and_pass_spec_check(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in EXAMPLES:
                m, _ = _render(self, d, load(ex), ex[:-5])
                self._check_outputs(m, os.path.join(d, ex[:-5]))
            for case in boundary_cases():
                m, _ = _render(self, d, case, case["name"])
                self._check_outputs(m, os.path.join(d, case["name"]))

    def test_every_existing_part_passes_its_spec_check(self):
        with tempfile.TemporaryDirectory() as d:
            for ex in sorted(os.path.basename(p) for p in glob.glob(os.path.join(EX, "*.json"))):
                if ex in EXAMPLES:
                    continue
                m, _ = _render(self, d, load(ex), ex[:-5])
                self._check_outputs(m, os.path.join(d, ex[:-5]))

    def test_spec_check_catches_a_missing_feature(self):
        """Mesh rendered without the back pockets, checked against the pocketed spec: the probes must fail."""
        k = load("keystone-10in-1u-synthetic.json")
        with tempfile.TemporaryDirectory() as d:
            _, stl = _render(self, d, dict(k, jack_latch_panel_thickness_mm=4.0), "flat")
            cfg, geo = validate.validate(k)
            rep = speccheck.run(cfg, geo, mesh.read_stl(stl))
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["failed"], 2 * k["keystone_count"])   # pocket behind ledge + pocket corner, per jack
        self.assertTrue(all("pocket" in p for p in rep["problems"]))
        # and a drawer without its finger pull
        dr = load("drawer-10in-2u-drawer-synthetic.json")
        with tempfile.TemporaryDirectory() as d:
            _, stl = _render(self, d, dict(dr, pull_width_mm=0.0), "nopull")
            cfg, geo = validate.validate(dr)
            rep = speccheck.run(cfg, geo, mesh.read_stl(stl))
        self.assertEqual([r["label"] for r in rep["results"] if not r["ok"]], ["finger pull"])

    def test_drawer_mesh_fits_housing_mesh(self):
        h, dr = load(EXAMPLES[2]), load(EXAMPLES[3])
        with tempfile.TemporaryDirectory() as d:
            mh, stl_h = _render(self, d, h, "housing")
            md, stl_d = _render(self, d, dr, "drawer")
            housing = speccheck.Solid(mesh.read_stl(stl_h))
        a = validate.validate(dr)[1]["args"]
        clr = dr["slide_clearance_mm"]
        th = dr["housing_wall_thickness_mm"]
        # inserted position: drawer front against the housing face, box from y = 0 to box_l, on the sleeve floor.
        # The slide-clearance gap around the box is empty housing space; beyond it is sleeve material.
        bw, bl, bh = a["box_w"], a["box_l"], a["box_h"]
        zt = th + bh
        for y in (1.0, bl / 2, bl - 1.0):
            for sx in (-1, 1):
                self.assertFalse(housing.inside((sx * (bw / 2 + clr / 2), y, th + bh / 2)), (sx, y))
                self.assertTrue(housing.inside((sx * (bw / 2 + clr + th / 2), y, th + bh / 2)), (sx, y))
            self.assertFalse(housing.inside((0.0, y, zt + clr / 2)), y)                 # above the box
            self.assertTrue(housing.inside((0.0, y, zt + clr + th / 2)), y)             # sleeve roof
        self.assertFalse(housing.inside((0.0, bl + clr / 2, th + bh / 2)))              # behind the box
        self.assertTrue(housing.inside((0.0, bl + clr + th / 2, th + bh / 2)))          # back wall
        self.assertEqual(md["mesh"]["shells"], 1)
        self.assertEqual(mh["mesh"]["shells"], 1)

    def test_png_and_3mf_are_deterministic_and_spec_check_cli(self):
        with tempfile.TemporaryDirectory() as d:
            hashes = []
            for out in ("a", "b"):
                p = os.path.join(d, out)
                r = cli("generate", os.path.join(EX, EXAMPLES[6]), "--out", p, "--png")
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(os.path.join(p, "manifest.json")) as f:
                    m = json.load(f)
                hashes.append((m["files"]["3mf"]["sha256"], m["files"]["png"]["sha256"]))
                self.assertEqual(sorted(os.listdir(p)), sorted(["manifest.json"] + [
                    "synthetic-comb-8x6mm-snap." + e for e in ("scad", "stl", "3mf", "png")]))
            self.assertEqual(hashes[0], hashes[1])
            r = cli("spec-check", os.path.join(d, "a"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(json.loads(r.stdout)["ok"])
            so = os.path.join(d, "scad-only")
            self.assertEqual(cli("generate", os.path.join(EX, EXAMPLES[6]), "--out", so, "--scad-only").returncode, 0)
            r = cli("spec-check", so)
            self.assertEqual(r.returncode, 4)
            self.assertIn("no STL", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


class CatalogTests(unittest.TestCase):
    def test_committed_catalog_page_is_current(self):
        with open(os.path.join(DOCS, "index.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), catalog.markdown(),
                             "regenerate with: python3 -m rackparts catalog --out build/catalog and copy to docs/rack-parts")

    def test_catalog_covers_every_part_and_example(self):
        parts = [e[0] for e in catalog.ENTRIES]
        self.assertEqual(sorted(parts), sorted(spec.PARTS))
        stems = sorted(s for e in catalog.ENTRIES for s in e[3])
        files = sorted(os.path.basename(p)[:-len("-synthetic.json")] for p in glob.glob(os.path.join(EX, "*.json")))
        self.assertEqual(stems, files)
        for stem in stems:
            self.assertTrue(os.path.exists(os.path.join(DOCS, "img", stem + ".png")), stem)

    def test_docs_site_links_the_catalog(self):
        with open(os.path.join(os.path.dirname(DOCS), "index.md"), encoding="utf-8") as f:
            self.assertIn("rack-parts/", f.read())


class DocTests(unittest.TestCase):
    def test_readme(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for ex in EXAMPLES:
            self.assertIn("examples/" + ex, readme)
        for name in FIXTURES:
            self.assertTrue(os.path.exists(os.path.join(FX, name)), name)
        for part in NEW_PARTS:
            self.assertIn("`%s`" % part, readme)
        for cmd in ("spec-check", "--png", "catalog"):
            self.assertIn(cmd, readme)


if __name__ == "__main__":
    unittest.main()
