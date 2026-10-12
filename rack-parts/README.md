# rack-parts

Parametric OpenSCAD generator for two printable rack parts:

- **`rack_bracket`** – an L-shaped rack ear: a front flange that screws to the rack rails and a side flange that screws to a device's side. One bracket per run (`hand: right|left`).
- **`sff_cradle`** – a U-shaped cradle that holds a small-form-factor PC (or any box) flat or on its side, with a retention lip, and screws down to a shelf/panel through counterbored holes.

You describe the part in a JSON file. The CLI validates it, writes a self-contained `.scad`, renders an STL with headless OpenSCAD, checks the mesh, and writes a `manifest.json`. Geometry and validation are deterministic, stdlib-only Python; no network or model/API service is used. The JSON schema is meant to be fillable by a person or an agent, but **every dimension must come from your own measurements**.

> **What this is not.** Nothing here has been printed, load-tested or installed in a rack. There is no strength, load or fit rating, and none is implied. Example files are **synthetic** fixtures and do not describe any real rack or device.

## Quick start

Requires Python ≥ 3.8 and OpenSCAD on `PATH` (tested with OpenSCAD 2021.01 and ADMesh 0.98.5 from Ubuntu 24.04: `sudo apt-get install openscad admesh`). ADMesh is optional but recommended; when present it is run as a second, independent mesh check. Run from this directory:

```sh
cd rack-parts
python3 -m rackparts generate examples/bracket-19in-1u-synthetic.json --out build/bracket-19in
python3 -m rackparts generate examples/bracket-10in-1u-synthetic.json --out build/bracket-10in
python3 -m rackparts generate examples/sff-cradle-synthetic.json      --out build/sff-cradle
```

Other commands:

```sh
python3 -m rackparts schema > my-schema.json          # JSON Schema (same as schema/rack-part.schema.json)
python3 -m rackparts validate my-part.json            # checks only, prints derived dimensions
python3 -m rackparts generate my-part.json --out build/x --scad-only   # skip rendering
python3 -m rackparts check-stl build/x/x.stl          # mesh checks on any STL
python3 -m unittest discover -s tests -t .            # test suite (render tests skip without openscad)
```

Exit codes: `0` ok, `2` input refused, `3` output directory already exists / tool failure, `4` rendered mesh rejected.

### Outputs

`--out DIR` must not exist; it is created, and nothing is ever overwritten. On any failure the files this run created are removed again. Inside:

| file | content |
|---|---|
| `<name>.scad` | self-contained OpenSCAD source: module copied from `scad/` plus one call with the validated numbers |
| `<name>.stl` | binary STL from `openscad --export-format binstl` |
| `manifest.json` | input parameters and their sha256, derived dimensions, expected bounding box, tool versions (Python, OpenSCAD, ADMesh), file sha256s, mesh report (facets, measured bbox, volume, shells, open edges, admesh counters), and an *unvalidated* print-orientation suggestion |

OpenSCAD 2021.01 can write the same facets in a different order on different runs, so the STL file hash may change between identical runs; `mesh.geometry_sha256` is order-independent and stays the same.

## What gets validated

Refusals happen before any file is written:

- **Data only.** Input is parsed with Python's JSON parser; `NaN`/`Infinity`, duplicate keys, unknown keys, missing keys, booleans or strings where numbers belong, and files over 64 KiB are refused. No value is evaluated or passed to a shell. Only numbers/booleans reach the SCAD file. The only string that reaches a filename is `name`, restricted to `[a-z0-9-]`. Output location comes from `--out` only.
- **Finite, bounded values.** Every number has a range (see table); thicknesses must be strictly positive.
- **Cross-field geometry.**
  - bracket: side flange + gusset + device-screw head + `rail_side_clearance_mm` must fit inside the measured rail opening; the rail screw shank must clear the side flange/gusset; the rack-screw head must bear on the flange; the pair of ears must fit `max_overall_width_mm`; every hole keeps `min_wall_mm` to every edge and to every other hole; device screw heads must not sit under the front flange/gusset; rail hole spacing must exceed the rail opening; a per-class window catches inch/mm mix-ups.
  - cradle: counterbore depth must leave `min_wall_mm` of base; counterbores may not cut into walls; the lip may not press on the device; outer size must fit the stated space; holes keep `min_wall_mm` to edges and each other.
  - Holes are modelled slightly larger than the stated diameter so the `facets`-sided polygon still clears it.
- **Rendered mesh.** OpenSCAD warnings/errors are fatal. The STL must be non-empty, have no zero-area facets, be a closed and consistently oriented manifold (every edge used exactly twice, in opposite directions), have positive volume, be a single connected shell, and its measured bounding box must equal the validator's prediction within 0.01 mm. ADMesh (read-only run) must report one part and zero repairs.
- **Not checked:** general triangle-triangle self-intersection (CSG through CGAL is not expected to produce it, and ADMesh is a second opinion, but no dedicated test exists), wall thickness of the printed result, strength, and real-world fit.

## Rack dimensions: standard vs. measure-it-yourself

**19-inch.** The 19-inch rack is defined by EIA/ECIA-310-E (*Cabinets, Racks, Panels, and Associated Equipment*) and IEC 60297-3-100. The values below are as commonly summarised from those standards; the standards themselves are paywalled and were not consulted directly for this tool — check the text if a value matters to you:

| item | nominal value |
|---|---|
| rack unit (U) | 1.75 in = 44.45 mm |
| front panel width | 19 in = 482.6 mm |
| rail mounting-hole centre spacing (horizontal) | 18.312 in = 465.1 mm |
| minimum clear opening between rails | 17.72 in = 450 mm |
| vertical hole pattern within each U | 3 holes; spacing repeats 0.625 in, 0.625 in, 0.5 in (15.875 / 15.875 / 12.7 mm) |

Even for 19-inch racks you must still **measure**: rail type (square cage-nut, round, tapped M6/10-32/12-24), actual opening (rails, PDUs and cable managers intrude), rail depth, whether your hole rows are where you think they are, and your device's screw positions. The bracket generator never fills these in for you.

**10-inch.** We know of no published standard that fixes 10-inch rack hole spacing, opening width, unit height, or screw type. Many 10-inch racks reuse the 44.45 mm unit and use roughly 254 mm wide panels, but vendors differ. **Treat every 10-inch dimension as measured.** The `rack_width_class` field only selects a coarse sanity window (180–270 mm for 10-inch, 430–500 mm for 19-inch rail-hole spacing) to catch unit mistakes; it is not a standard and supplies no geometry.

**Devices.** Side screw positions, thread size and head size on devices are vendor- and model-specific. There is no universal pattern; measure your unit.

## How to measure and check fit

1. **Rail hole spacing** (`rail_hole_center_spacing_mm`): measure edge-to-edge between the *left* edges of one left-rail hole and the matching right-rail hole (same as centre-to-centre). Measure at the U you will use.
2. **Rail opening** (`rail_opening_width_mm`): narrowest clear width between rails over the depth the bracket's side flange will occupy.
3. **Vertical hole offsets** (`rail_hole_z_offsets_mm`): decide where the bracket's bottom edge sits, then measure each hole centre above it. For an EIA-310 1U bracket of height 43.66 mm sitting 0.395 mm above the U boundary, the three holes are at 5.955, 21.83 and 37.705 mm — this is what the 19-inch example uses.
4. **Screws**: measure the major diameter and head diameter/height of the actual screws (or washers) you will use; add your own clearance to get `*_clearance_diameter_mm`.
5. **Device**: measure width across the side faces and every side screw hole position from the device's front face and bottom.
6. Run `validate`; read the derived dimensions (e.g. `overall_pair_width_mm`, `rail_inner_edge_x_local_mm`) and compare them with your rack.
7. Before trusting a part, print a quick test (e.g. scale a thin slice in your slicer, or set `side_flange_depth_mm` small) and check the holes against the real rails and device. Adjust the `tolerance` parameters for your printer and material.

Tolerances (`*_clearance_*`, `fit_clearance_mm`, `rail_side_clearance_mm`, `device_side_gap_mm`, `min_wall_mm`) are deliberately user-configurable; the example values are rough starting points, not recommendations for any printer.

## Slicer orientation (unvalidated)

The manifest includes a suggestion; none of it has been tested on a printer.

- Bracket: lay the side flange flat on the bed so layer lines run along the flange; the front flange then stands vertically. Alternatively print front-flange-down. Either way the inner corner sees bending loads across layers in some direction — consider the gusset, more perimeters, or a stronger material, and decide for yourself whether a printed part is appropriate at all.
- Cradle: base down as modelled; the retention lip is an overhang that likely needs supports or a chamfer you add.

## Parameters

All lengths are millimetres and **all keys are required** (no hidden defaults). `kind`: *measured* – measure the real rack/device; *chosen* – a design choice; *tolerance* – a clearance you tune; *label* – metadata. Machine-readable form with the same text: [`schema/rack-part.schema.json`](schema/rack-part.schema.json) (JSON Schema 2020-12, `x-kind` annotations). Schema ranges are coarse; the cross-field checks above are only enforced by the CLI.

Bracket frame: x = 0 at the device-side face of the side flange (+x toward the rail), y = 0 at the front face (+y into the rack), z = 0 at the bottom edge. Cradle frame: x centred, y = 0 at the front edge, z = 0 at the base bottom.

**Common (both parts)**

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `schema_version` | label | `1` | Must be 1. |
| `name` | label | slug | Output basename: 1-48 chars of a-z, 0-9 and '-', starting with a letter or digit. |
| `units` | label | `mm` | All lengths are millimetres. Must be "mm". |
| `facets` | chosen | 24 – 128 | Polygon segments per circle ($fn). Holes are enlarged so the polygon still clears the stated diameter. |
| `min_wall_mm` | chosen | >0.4 – 20 | Minimum material left between any hole/counterbore and a part edge or another hole. |

**`rack_bracket`**

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `rack_bracket` | Selects the L-shaped rack ear bracket. |
| `rack_width_class` | label | `10in` \| `19in` | Only selects a sanity window for rail_hole_center_spacing_mm (catches inch/mm mix-ups). It does not supply any dimension. |
| `hand` | chosen | `right` \| `left` | Which side of the device, viewed from the front. 'left' is the X-mirror of 'right'. |
| `rail_hole_center_spacing_mm` | measured | 150 – 520 | Horizontal centre-to-centre distance between the left and right rail mounting holes. |
| `rail_opening_width_mm` | measured | 100 – 500 | Clear horizontal opening between the inner edges of the left and right rails (narrowest point along the depth the bracket occupies). |
| `max_overall_width_mm` | measured | 100 – 520 | Widest the pair of front flanges may be (e.g. rack panel width or nearest obstruction). |
| `rail_hole_z_offsets_mm` | measured | 1–8 values, each 0 – 400 | Rail hole centres used, measured upward from the bottom edge of the bracket as it will sit in the rack. 1-8 values. |
| `rail_screw_clearance_diameter_mm` | tolerance | >1 – 12 | Through-hole diameter for the rack screw (screw major diameter + your clearance). |
| `rail_screw_head_diameter_mm` | measured | >1 – 25 | Rack screw (or washer) head diameter; the head must bear fully on the front flange. |
| `rail_slot_extra_length_mm` | tolerance | 0 – 15 | Horizontal slot elongation of each rail hole for side-to-side adjustment (0 = round hole). |
| `rail_side_clearance_mm` | tolerance | 0 – 10 | Gap required between the bracket/device-screw heads and the inner rail edge. |
| `device_width_mm` | measured | 20 – 500 | Device width across the faces the brackets attach to. |
| `device_side_gap_mm` | tolerance | 0 – 5 | Gap between the device side and the bracket side flange. |
| `device_setback_mm` | chosen | -100 – 400 | Distance from the bracket front face back to the device front face (negative = device protrudes forward). |
| `device_bottom_offset_mm` | chosen | -100 – 400 | Height of the device bottom above the bracket bottom edge. |
| `device_screw_positions_mm` | measured | 1–8 `[y,z]`, each -100 – 600 | Device side screw holes as [y, z]: y back from the device front face, z up from the device bottom. 1-8 pairs. |
| `device_screw_clearance_diameter_mm` | tolerance | >1 – 10 | Through-hole diameter in the side flange for the device screw. |
| `device_screw_head_diameter_mm` | measured | >1 – 20 | Device screw head diameter. |
| `device_screw_head_height_mm` | measured | 0 – 10 | Device screw head height; it sits on the outside of the side flange, next to the rail. |
| `thickness_mm` | chosen | >1 – 12 | Material thickness of both flanges. |
| `flange_height_mm` | chosen | 10 – 400 | Height of the bracket (both flanges). For 19-inch gear this is usually a little under n x 44.45 mm. |
| `side_flange_depth_mm` | chosen | 10 – 400 | How far the side flange extends back from the front face. |
| `gusset_mm` | chosen | 0 – 40 | Leg length of the triangular corner gusset (0 = none). |

**`sff_cradle`**

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `sff_cradle` | Selects the U-shaped small-form-factor PC cradle. |
| `device_orientation` | chosen | `flat` \| `on_side` | 'flat': device sits on its bottom. 'on_side': device stands on a side, so its height becomes the cradle width. |
| `device_width_mm` | measured | 20 – 500 | Device width in its normal (flat) pose. |
| `device_height_mm` | measured | 10 – 500 | Device height in its normal (flat) pose. |
| `device_depth_mm` | measured | 20 – 600 | Device depth front to back. |
| `fit_clearance_mm` | tolerance | 0 – 10 | Gap added on each side of the device inside the cradle (and above it, under the lip). |
| `cradle_length_mm` | chosen | 20 – 600 | Cradle length along the device depth. |
| `base_thickness_mm` | chosen | >1 – 20 | Base plate thickness. |
| `wall_thickness_mm` | chosen | >1 – 20 | Side wall and lip thickness. |
| `wall_clear_height_mm` | chosen | 2 – 500 | Clear height inside the cradle from the base top to the wall top (or to the underside of the lip). |
| `retention_lip_mm` | chosen | 0 – 60 | Inward lip at the top of each wall (0 = none). |
| `shelf_hole_positions_mm` | measured | 1–12 `[x,y]`, each -300 – 600 | Mounting holes as [x, y]: x from the cradle centreline, y back from the cradle front edge. Must match holes you measured on your shelf or panel. 1-12 pairs. |
| `shelf_screw_clearance_diameter_mm` | tolerance | >1 – 12 | Through-hole diameter for the shelf screw. |
| `shelf_screw_head_diameter_mm` | measured | >1 – 25 | Shelf screw head diameter (counterbored from the top). |
| `shelf_screw_head_height_mm` | measured | 0 – 15 | Shelf screw head height; the counterbore is this deep so the head sits flush or below the base top. |
| `max_overall_width_mm` | measured | 20 – 600 | Available width on the shelf. |
| `max_overall_depth_mm` | measured | 20 – 700 | Available depth on the shelf. |
| `max_overall_height_mm` | measured | 5 – 600 | Available height above the shelf. |

## Example fixtures (synthetic)

| file | what it is |
|---|---|
| `examples/bracket-19in-1u-synthetic.json` | right bracket, EIA-310 nominal rail spacing/opening and 1U hole pattern, for a made-up 430 mm wide box with made-up side screws |
| `examples/bracket-10in-1u-synthetic.json` | left bracket for a made-up 10-inch rack (236.5 mm hole spacing, 220 mm opening are invented sample values, not a standard) and a made-up 200 mm wide box |
| `examples/sff-cradle-synthetic.json` | cradle for a made-up 180 × 60 × 190 mm box, flat, on a made-up shelf hole pattern |
| `tests/fixtures/*.json` | inputs that must be refused (zero thickness / out-of-range size, bracket too wide for the rail opening, counterbore too deep, lip pressing on the device, NaN) |

## Limitations

- No strength, load, temperature or fire rating; no claim that a printed part can safely carry any device. Rack-mounted equipment can be heavy; use the device's own rack kit or a proper shelf when in doubt.
- No physical print, rack installation, or hardware test has been done. Fit depends on your measurements, printer, material and slicer.
- Only two part shapes; no cage-nut, threaded-insert, vent, cable or rear-support features.
- Self-intersection is not tested directly (see above).
- Not every bad idea is detectable: the validator checks the relationships listed above, nothing more.

## License and provenance

MIT — see [`LICENSE`](LICENSE). All code, SCAD modules and example data in this directory were written for this repository (with AI assistance via Claude Code) and contain no third-party geometry, vendor drawings, or measurements of any real rack or device. Standard dimensions are cited from public summaries of EIA/ECIA-310-E and IEC 60297-3-100 as noted above.
