"""Catalog page for the docs site: one entry per part, one preview per synthetic example.

``markdown()`` needs only the validator, so a unit test can check that the committed page
(docs/rack-parts/index.md) is current. ``build()`` also renders every example with OpenSCAD and writes
its PNG preview next to the page; ``check()`` rebuilds and compares the page text and every preview's
decoded pixels with a committed copy (pixels rather than bytes: zlib builds may compress the same
pixels differently).
"""

import json
import os
import shutil
import struct
import tempfile
import zlib

from . import generate, spec, validate

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLES = os.path.join(ROOT, "examples")
REPO_URL = "https://github.com/crew-exchange-2079/goblin-workshop/blob/main/rack-parts"

# (part, title, one-line description, example stems in display order)
ENTRIES = [
    ("rack_bracket", "Rack bracket", "L-shaped rack ear: front flange on the rails, side flange on the device.",
     ["bracket-19in-1u", "bracket-10in-1u"]),
    ("sff_cradle", "SFF PC cradle", "U-shaped cradle with retention lip for a small-form-factor PC or box.",
     ["sff-cradle"]),
    ("switch_cradle", "Switch cradle", "Drop-in cradle for a small switch: stop tabs, strap slots, vent windows.",
     ["switch-cradle"]),
    ("blanking_panel", "Blanking panel", "Flat rack panel on measured rail holes, optional stiffening ribs.",
     ["blank-10in-1u", "blank-19in-1u"]),
    ("cable_panel", "Cable pass-through panel", "Rack panel with stadium cable openings, edges rounded on both faces.",
     ["cable-10in-1u", "cable-19in-2u"]),
    ("keystone_panel", "Keystone patch panel", "Rack panel with rows of snap-in keystone cutouts, thinned from "
     "the back to the thickness the jack latch grips.", ["keystone-10in-1u", "keystone-19in-1u"]),
    ("rack_shelf", "Rack shelf (10-inch)", "Shallow ventilated cantilever shelf hanging from the front rail screws.",
     ["shelf-10in-1u", "shelf-10in-2u"]),
    ("rack_drawer", "Rack drawer (10-inch)", "Housing with a closed sleeve on the front rails plus a sliding "
     "drawer box with finger pull and dividers; both parts come from one file.",
     ["drawer-10in-2u-housing", "drawer-10in-2u-drawer"]),
    ("pdu_mount", "PDU / power-strip mount", "External strap-retained bracket that keeps clear of outlets, "
     "switch, vents and cord.", ["pdu-strip", "pdu-compact"]),
    ("label_holder", "Label holder", "Slide-in card holder with a front window, optional screw ears.",
     ["label-card-ears", "label-strip-adhesive"]),
    ("cable_comb", "Cable comb", "Bar of round cable seats behind entry throats; snap-in or drop-in.",
     ["comb-8x6mm-snap", "comb-12x3mm-open"]),
    ("cartridge_holder", "SFP / tape cartridge holder", "Grid of upright pockets sized from your items, with "
     "push-out holes and finger notches.", ["cartridge-tape-5", "cartridge-sfp-12"]),
]


def load_example(stem):
    with open(os.path.join(EXAMPLES, stem + "-synthetic.json"), encoding="utf-8") as f:
        return json.load(f)


def _size(geo):
    lo, hi = geo["expected_bbox"]["min"], geo["expected_bbox"]["max"]
    return " x ".join("%g" % round(hi[i] - lo[i], 2) for i in range(3))


def _kinds(part):
    counts = {}
    for f in spec.PARTS[part]:
        counts[f[4]] = counts.get(f[4], 0) + 1
    return counts


def markdown():
    out = ["---", "title: rack-parts catalog", "---", "",
           "# rack-parts catalog", "",
           "Parametric, printable rack accessories from [`rack-parts/`](%s). Every part is generated from a JSON "
           "file of **your own measurements**, validated (refused with exit code 2 if inconsistent), rendered "
           "with OpenSCAD, mesh-checked, spec-checked against the features it must have, and exported as STL "
           "and 3MF." % REPO_URL, "",
           "> **Synthetic examples, no rating.** Every picture below comes from a made-up example file, not a "
           "real rack or device. Nothing here has been printed, load-tested or installed; there is no "
           "strength, load, heat, fire or fit rating. Measure your own rack, devices, screws and printer.", "",
           "Previews are orthographic renders of the checked mesh (front-left, from above); they are not to scale "
           "with each other and are not for measurement. Sizes are the bounding box *x* (across) x *y* (depth) x "
           "*z* (height) in mm from the validator.", "",
           "| part | `part` value | what it is | inputs (measured / chosen / tolerance) |",
           "|---|---|---|---|"]
    for part, title, blurb, _ in ENTRIES:
        k = _kinds(part)
        out.append("| [%s](#%s) | `%s` | %s | %d / %d / %d |" % (
            title, part.replace("_", "-"), part, blurb, k.get("measured", 0), k.get("chosen", 0), k.get("tolerance", 0)))
    out += ["", "Generate any of them:", "", "```sh", "cd rack-parts",
            "python3 -m rackparts generate examples/<example>-synthetic.json --out build/<example> --png",
            "python3 -m rackparts spec-check build/<example>   # re-run the part's feature probes", "```", ""]
    for part, title, blurb, stems in ENTRIES:
        out += ['<a id="%s"></a>' % part.replace("_", "-"), "", "## %s" % title, "", blurb, ""]
        out += ["| example | size mm | notes |", "|---|---|---|"]
        for stem in stems:
            cfg, geo = validate.validate(load_example(stem))
            notes = []
            if part == "rack_drawer":
                notes.append("component `%s`" % cfg["component"])
            if geo["derived"].get("fits_build_plate") is False:
                notes.append("**does not fit** the stated %g x %g mm plate" % tuple(geo["derived"]["build_plate_mm"]))
            for key, fmt in (("jacks", "%d jacks"), ("pockets", "%d pockets"), ("brackets_to_print", "print %d")):
                if key in geo["derived"]:
                    notes.append(fmt % geo["derived"][key])
            if "physical_use" in geo:
                notes.append("fit coupon required before use")
            out.append("| [`%s`](%s/examples/%s-synthetic.json) | %s | %s |"
                       % (stem, REPO_URL, stem, _size(geo), "; ".join(notes) or "-"))
        out.append("")
        for stem in stems:
            out.append("![%s preview](img/%s.png)" % (stem, stem))
        out += ["", "Parameters: [README, `%s`](%s/README.md#parameters)." % (part, REPO_URL), ""]
    out += ["## Not checked", "",
            "Strength, load, stiffness, heat, creep, fire and electrical behaviour, real-world fit, and whether a "
            "printed part is appropriate at all. See the [rack-parts README](%s/README.md) for what the validator "
            "and the spec checks do and do not cover." % REPO_URL, ""]
    return "\n".join(out)


def build(out_dir, openscad=None):
    """Write ``out_dir``/index.md and ``out_dir``/img/<example>.png. ``out_dir`` must not exist."""
    os.mkdir(out_dir)  # raises FileExistsError: never clobber
    try:
        return _build_into(out_dir, openscad)
    except BaseException:
        shutil.rmtree(out_dir, ignore_errors=True)   # created by this call above, so ours to remove
        raise


def _build_into(out_dir, openscad):
    img = os.path.join(out_dir, "img")
    os.mkdir(img)
    written = []
    with tempfile.TemporaryDirectory(prefix="rackparts-catalog-") as tmp:
        for _part, _title, _blurb, stems in ENTRIES:
            for stem in stems:
                cfg, geo = validate.validate(load_example(stem))
                run = os.path.join(tmp, stem)
                man = generate.generate(cfg, geo, run, openscad=openscad, png=True)
                dst = os.path.join(img, stem + ".png")
                with open(os.path.join(run, man["files"]["png"]["path"]), "rb") as src, open(dst, "xb") as f:
                    shutil.copyfileobj(src, f)
                written.append(dst)
    page = os.path.join(out_dir, "index.md")
    with open(page, "x", encoding="utf-8") as f:
        f.write(markdown())
    written.append(page)
    return written


def png_pixels(path):
    """(width, height, raw scanlines) of an 8-bit RGB PNG as written by export.png_bytes."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s is not a PNG" % path)
    i, idat, size = 8, b"", None
    while i + 8 <= len(data):
        n, tag = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if tag == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        i += 12 + n
    return size[0], size[1], zlib.decompress(idat)


def check(committed_dir, openscad=None):
    """Rebuild the catalog and compare it with ``committed_dir``. Returns a list of differences."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="rackparts-catalog-check-") as tmp:
        fresh = os.path.join(tmp, "catalog")
        build(fresh, openscad=openscad)
        for rel in sorted(set(_files(fresh)) | set(_files(committed_dir))):
            a, b = os.path.join(fresh, rel), os.path.join(committed_dir, rel)
            if not os.path.exists(b):
                problems.append("%s: missing from %s" % (rel, committed_dir))
            elif not os.path.exists(a):
                problems.append("%s: not produced by the current code (stale file)" % rel)
            elif rel.endswith(".png"):
                pa, pb = png_pixels(a), png_pixels(b)
                if pa[:2] != pb[:2]:
                    problems.append("%s: size %s, committed %s" % (rel, pa[:2], pb[:2]))
                elif pa[2] != pb[2]:
                    diff = sum(1 for x, y in zip(pa[2], pb[2]) if x != y)
                    problems.append("%s: %d bytes of pixel data differ" % (rel, diff))
            else:
                with open(a, "rb") as fa, open(b, "rb") as fb:
                    if fa.read() != fb.read():
                        problems.append("%s: content differs" % rel)
    return problems


def _files(root):
    out = []
    for d, _dirs, names in os.walk(root):
        for n in names:
            out.append(os.path.relpath(os.path.join(d, n), root))
    return out
