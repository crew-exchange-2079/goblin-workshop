import copy
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rackparts import generate, mesh, spec, validate  # noqa: E402

EX = os.path.join(ROOT, "examples")
FX = os.path.join(ROOT, "tests", "fixtures")
EXAMPLES = ["bracket-10in-1u-synthetic.json", "bracket-19in-1u-synthetic.json", "sff-cradle-synthetic.json"]
HAVE_OPENSCAD = shutil.which("openscad") is not None


def load(name):
    with open(os.path.join(EX, name)) as f:
        return json.load(f)


def refused(data):
    try:
        validate.validate(data)
    except validate.ConfigError as e:
        return e.problems
    return None


def cli(*args):
    return subprocess.run([sys.executable, "-m", "rackparts", *args], cwd=ROOT, capture_output=True, text=True)


class SchemaTests(unittest.TestCase):
    def test_committed_schema_is_current(self):
        with open(os.path.join(ROOT, "schema", "rack-part.schema.json")) as f:
            self.assertEqual(json.load(f), spec.json_schema())

    def test_readme_documents_every_parameter(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for fields in spec.PARTS.values():
            for name, *_ in fields:
                self.assertIn("`%s`" % name, readme)

    def test_examples_are_valid(self):
        for name in EXAMPLES:
            self.assertIsNone(refused(load(name)), name)


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self.b = load("bracket-19in-1u-synthetic.json")
        self.c = load("sff-cradle-synthetic.json")

    def test_strict_json(self):
        bad = ['{"a": NaN}', '{"a": Infinity}', '{"a": 1, "a": 2}', "[1,2]", "{"]
        for text in bad:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                f.write(text)
            try:
                with self.assertRaises(validate.ConfigError):
                    validate.validate(validate.load_config(f.name))
            finally:
                os.unlink(f.name)

    def test_types_and_keys(self):
        for k, v in [("thickness_mm", "4"), ("thickness_mm", True), ("thickness_mm", None),
                     ("facets", 48.0), ("hand", "both"), ("units", "in"), ("name", "../x"),
                     ("name", "A"), ("rail_hole_z_offsets_mm", []), ("device_screw_positions_mm", [[1.0]])]:
            d = dict(self.b, **{k: v})
            self.assertTrue(refused(d), (k, v))
        d = dict(self.b, extra=1)
        self.assertTrue(refused(d))
        d = dict(self.b)
        del d["gusset_mm"]
        self.assertTrue(refused(d))

    def test_size_bounds(self):
        for k, v in [("thickness_mm", 0), ("thickness_mm", -3), ("flange_height_mm", 1e6), ("facets", 4)]:
            self.assertTrue(refused(dict(self.b, **{k: v})), (k, v))
        self.assertTrue(refused(dict(self.b, rail_hole_center_spacing_mm=18.312)))  # inches, not mm

    def test_bracket_clearances(self):
        self.assertTrue(refused(dict(self.b, device_width_mm=440.0)))           # hits rails
        self.assertTrue(refused(dict(self.b, rail_opening_width_mm=470.0)))     # spacing < opening
        self.assertTrue(refused(dict(self.b, max_overall_width_mm=470.0)))      # ears too wide
        self.assertTrue(refused(dict(self.b, rail_hole_z_offsets_mm=[1.0])))    # hole at edge
        self.assertTrue(refused(dict(self.b, rail_hole_z_offsets_mm=[20.0, 22.0])))  # overlapping
        self.assertTrue(refused(dict(self.b, device_screw_positions_mm=[[0.0, 15.0]])))  # head under flange
        self.assertTrue(refused(dict(self.b, device_screw_positions_mm=[[20.0, 15.0], [23.0, 15.0]])))

    def test_cradle_clearances(self):
        self.assertTrue(refused(dict(self.c, shelf_screw_head_height_mm=4.5)))
        self.assertTrue(refused(dict(self.c, device_height_mm=65.0)))
        self.assertTrue(refused(dict(self.c, shelf_hole_positions_mm=[[88.0, 30.0]])))  # into wall
        self.assertTrue(refused(dict(self.c, max_overall_width_mm=150.0)))
        self.assertTrue(refused(dict(self.c, cradle_length_mm=195.0)))

    def test_orientation_swaps_footprint(self):
        d = dict(self.c, device_orientation="on_side", wall_clear_height_mm=181.0, max_overall_height_mm=200.0,
                 shelf_hole_positions_mm=[[0.0, 30.0], [0.0, 90.0]], retention_lip_mm=6.0,
                 max_overall_width_mm=80.0)
        _, geo = validate.validate(d)
        self.assertEqual(geo["derived"]["footprint_width_mm"], 60.0)
        self.assertEqual(geo["derived"]["footprint_height_mm"], 180.0)

    def test_fixtures_refused_by_cli(self):
        for name in sorted(os.listdir(FX)):
            r = cli("validate", os.path.join(FX, name))
            self.assertEqual(r.returncode, 2, name + r.stderr)
            self.assertIn("REFUSED", r.stderr)

    def test_scad_writer_only_takes_numbers(self):
        with self.assertRaises(TypeError):
            generate._lit("x); import(\"/etc/passwd")
        with self.assertRaises(ValueError):
            generate._lit(float("nan"))


def _box_stl(path, drop_last=False):
    v = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    if drop_last:
        f = f[:-1]
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(f)))
        for a, b, c in f:
            fh.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0))


class MeshTests(unittest.TestCase):
    def test_closed_box(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "box.stl")
            _box_stl(p)
            rep = mesh.analyse(mesh.read_stl(p))
            self.assertTrue(rep["ok"], rep)
            self.assertAlmostEqual(rep["volume_mm3"], 1.0)
            self.assertEqual(rep["shells"], 1)

    def test_open_box_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "box.stl")
            _box_stl(p, drop_last=True)
            rep = mesh.analyse(mesh.read_stl(p))
            self.assertFalse(rep["ok"])
            self.assertGreater(rep["open_edges"], 0)

    def test_empty_rejected(self):
        with self.assertRaises(mesh.MeshError):
            mesh.analyse([])


class OutputSafetyTests(unittest.TestCase):
    def test_no_clobber(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.mkdir(out)
            marker = os.path.join(out, "keep.txt")
            with open(marker, "w") as f:
                f.write("mine")
            r = cli("generate", os.path.join(EX, EXAMPLES[0]), "--out", out, "--scad-only")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertEqual(os.listdir(out), ["keep.txt"])
            # an existing empty directory or a symlink is refused too
            empty = os.path.join(d, "empty")
            os.mkdir(empty)
            self.assertEqual(cli("generate", os.path.join(EX, EXAMPLES[0]), "--out", empty, "--scad-only").returncode, 3)
            link = os.path.join(d, "link")
            os.symlink(out, link)
            self.assertEqual(cli("generate", os.path.join(EX, EXAMPLES[0]), "--out", link, "--scad-only").returncode, 3)

    def test_refused_input_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            r = cli("generate", os.path.join(FX, "unsafe-clearance-bracket.json"), "--out", out)
            self.assertEqual(r.returncode, 2)
            self.assertFalse(os.path.exists(out))

    def test_render_failure_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            r = cli("generate", os.path.join(EX, EXAMPLES[0]), "--out", out, "--openscad", "/bin/false")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertFalse(os.path.exists(out))


@unittest.skipUnless(HAVE_OPENSCAD, "openscad not installed")
class RenderTests(unittest.TestCase):
    def test_examples_render_to_clean_meshes(self):
        with tempfile.TemporaryDirectory() as d:
            for name in EXAMPLES:
                out = os.path.join(d, name[:-5])
                r = cli("generate", os.path.join(EX, name), "--out", out)
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(os.path.join(out, "manifest.json")) as f:
                    m = json.load(f)
                rep = m["mesh"]
                self.assertTrue(rep["ok"], rep["problems"])
                self.assertEqual(rep["open_edges"], 0)
                self.assertEqual(rep["shells"], 1)
                self.assertTrue(rep["bbox_matches_expected"])
                self.assertGreater(rep["volume_mm3"], 0)
                if shutil.which("admesh"):
                    self.assertTrue(rep["admesh"]["ok"], rep["admesh"])
                stl = os.path.join(out, m["files"]["stl"]["path"])
                self.assertEqual(generate.sha256_file(stl), m["files"]["stl"]["sha256"])
                self.assertIn("openscad", m["tools"])

    def test_render_is_deterministic(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            for out in (a, b):
                self.assertEqual(cli("generate", os.path.join(EX, EXAMPLES[2]), "--out", out).returncode, 0)
            ms = []
            for out in (a, b):
                with open(os.path.join(out, "manifest.json")) as f:
                    m = json.load(f)
                # OpenSCAD 2021.01 may emit the same facets in a different order, so
                # raw STL bytes can differ; the order-independent geometry hash must not.
                del m["files"]["stl"]["sha256"]
                ms.append(m)
            self.assertEqual(ms[0], ms[1])


if __name__ == "__main__":
    unittest.main()
