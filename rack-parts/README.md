# rack-parts

Parametric OpenSCAD generator for seven printable rack parts:

- **`rack_bracket`** – an L-shaped rack ear: a front flange that screws to the rack rails and a side flange that screws to a device's side. One bracket per run (`hand: right|left`).
- **`sff_cradle`** – a U-shaped cradle that holds a small-form-factor PC (or any box) flat or on its side, with a retention lip, and screws down to a shelf/panel through counterbored holes.
- **`switch_cradle`** – a drop-in cradle for a small network switch: front/rear stop tabs, side walls, a strap over the top, side vent windows cut where you measured the switch's vents, rounded base vent slots, open front/rear for cables, and counterbored shelf holes. Port areas you measure are checked to stay clear.
- **`blanking_panel`** – a flat 10-inch or 19-inch rack panel with slotted holes at the rail positions you measured, and optional stiffening ribs behind it.
- **`cable_panel`** – the same panel with horizontal stadium-shaped cable pass-throughs whose edges are rounded (quarter-round) on both faces.
- **`rack_shelf`** – a shallow ventilated **10-inch** shelf: a front plate on your measured rail holes, a floor cantilevered behind it with side walls, an optional rear lip and rounded vent slots. The measured device must fit on it. 19-inch shelves are **refused (deferred)**, see [Rack shelf](#rack-shelf).
- **`pdu_mount`** – an external, strap-retained bracket for a power strip / PDU body: the strip rests between two side walls and a strap over its top loops around a bar in each wall; screw ears fix the bracket to a surface. You print 2–4 identical brackets. Non-electrical accessory only, see [PDU mount](#pdu-mount).

You describe the part in a JSON file. The CLI validates it, writes a self-contained `.scad`, renders an STL with headless OpenSCAD, checks the mesh, and writes a `manifest.json`. Geometry and validation are deterministic, stdlib-only Python; no network or model/API service is used. The JSON schema is meant to be fillable by a person or an agent, but **every dimension must come from your own measurements**.

> **What this is not.** Nothing here has been printed, load-tested or installed in a rack. There is no strength, load or fit rating, and none is implied. Example files are **synthetic** fixtures and do not describe any real rack or device.

## Quick start

Requires Python ≥ 3.8 and OpenSCAD on `PATH` (tested with OpenSCAD 2021.01 and ADMesh 0.98.5 from Ubuntu 24.04: `sudo apt-get install openscad admesh`). ADMesh is optional but recommended; when present it is run as a second, independent mesh check. Run from this directory:

```sh
cd rack-parts
python3 -m rackparts generate examples/bracket-19in-1u-synthetic.json --out build/bracket-19in
python3 -m rackparts generate examples/bracket-10in-1u-synthetic.json --out build/bracket-10in
python3 -m rackparts generate examples/sff-cradle-synthetic.json      --out build/sff-cradle
python3 -m rackparts generate examples/switch-cradle-synthetic.json   --out build/switch-cradle
python3 -m rackparts generate examples/blank-10in-1u-synthetic.json   --out build/blank-10in-1u
python3 -m rackparts generate examples/blank-19in-1u-synthetic.json   --out build/blank-19in-1u
python3 -m rackparts generate examples/cable-10in-1u-synthetic.json   --out build/cable-10in-1u
python3 -m rackparts generate examples/cable-19in-2u-synthetic.json   --out build/cable-19in-2u
python3 -m rackparts generate examples/shelf-10in-1u-synthetic.json   --out build/shelf-10in-1u
python3 -m rackparts generate examples/shelf-10in-2u-synthetic.json   --out build/shelf-10in-2u
python3 -m rackparts generate examples/pdu-strip-synthetic.json       --out build/pdu-strip
python3 -m rackparts generate examples/pdu-compact-synthetic.json     --out build/pdu-compact
```

The 19-inch panel examples are 482.6 mm wide and **do not fit a typical 220–256 mm build plate**; they set `allow_oversize_print: true` so the geometry can still be inspected. See [Build plate and full-width panels](#build-plate-and-full-width-panels).

Other commands:

```sh
python3 -m rackparts schema > my-schema.json          # JSON Schema (same as schema/rack-part.schema.json)
python3 -m rackparts validate my-part.json            # checks only, prints derived dimensions
python3 -m rackparts generate my-part.json --out build/x --scad-only   # skip rendering
python3 -m rackparts check-stl build/x/x.stl          # mesh checks on any STL
python3 -m unittest discover -s tests -t .            # test suite (render tests skip without openscad)
RACKPARTS_REQUIRE_RENDER=1 python3 -m unittest discover -s tests -t .   # as in CI: shelf/PDU render tests fail, not skip, without openscad+admesh
```

Exit codes: `0` ok, `2` input refused (including an `--out` path containing a newline or other control character), `3` output directory already exists / tool failure, `4` rendered mesh rejected.

### Outputs

`--out DIR` must not exist; it is created, and nothing is ever overwritten. On any failure the files this run created are removed again. Inside:

| file | content |
|---|---|
| `<name>.scad` | self-contained OpenSCAD source: module copied from `scad/` plus one call with the validated numbers |
| `<name>.stl` | binary STL from `openscad --export-format binstl` |
| `manifest.json` | input parameters and their sha256, derived dimensions, expected bounding box, tool versions (Python, OpenSCAD, ADMesh), file sha256s, mesh report (facets, measured bbox, volume, shells, open edges, admesh counters), and an *unvalidated* print-orientation suggestion. `rack_shelf` and `pdu_mount` manifests also carry `physical_use`: an `UNVALIDATED` status and the fit-coupon / owner-inspection steps required before use |

OpenSCAD 2021.01 can write the same facets in a different order on different runs, so the STL file hash may change between identical runs; `mesh.geometry_sha256` is order-independent and stays the same.

## What gets validated

Refusals happen before any file is written:

- **Data only.** Input is parsed with Python's JSON parser; `NaN`/`Infinity`, duplicate keys, unknown keys, missing keys, booleans or strings where numbers belong, numeric literals longer than 32 characters or outside float range, nesting deeper than 4 levels, and files over 64 KiB are refused (exit 2, nothing written). No value is evaluated or passed to a shell. Only numbers/booleans reach the SCAD file. The only string that reaches a filename is `name`, which must fully match `[a-z0-9][a-z0-9-]{0,47}` (no trailing newline or other characters). Output location comes from `--out` only.
- **Finite, bounded values.** Every number has a range (see table); thicknesses must be strictly positive.
- **Cross-field geometry.**
  - bracket: side flange + gusset + device-screw head + `rail_side_clearance_mm` must fit inside the measured rail opening; the rail screw shank must clear the side flange/gusset; the rack-screw head must bear on the flange; the pair of ears must fit `max_overall_width_mm`; every hole keeps `min_wall_mm` to every edge and to every other hole; device screw heads must not sit under the front flange/gusset; rail hole spacing must exceed the rail opening; a per-class window catches inch/mm mix-ups.
  - cradle: counterbore depth must leave `min_wall_mm` of base; counterbores may not cut into walls; the lip may not press on the device; outer size must fit the stated space; holes keep `min_wall_mm` to edges and each other.
  - switch cradle: stop tabs must overlap the switch face by at least `min_wall_mm` beyond `fit_clearance_mm` and at least one strap slot must sit over the switch (refused as *missing retention* otherwise); stop tabs may not cover any measured front/rear port rectangle; port and vent rectangles must lie on the switch face; a side vent window is cut for every vent rectangle and must leave wall at the cradle ends and `min_wall_mm` to strap slots and other windows; strap bar ≥ `min_wall_mm`; base vent slots keep `min_wall_mm` to each other, the walls and the stop tabs; counterbores may not run under a stop tab, into a wall, or within `min_wall_mm` of a vent slot; outer size must fit the stated space and the build plate.
  - panels: rail hole spacing must exceed the rail opening and fall in the per-class sanity window; the panel must be wider than the opening; screw heads must bear fully on the panel horizontally and holes keep `min_wall_mm` to the top/bottom edge and to each other; stiffening ribs stay inside the rail opening minus `rail_side_clearance_mm`, may not reach the rail screw shanks, are at least `min_wall_mm` thick and leave open height; the face must fit the build plate in either rotation unless `allow_oversize_print` is true.
  - cable panel: the opening neck must be at least `cable_bundle_diameter_mm` tall (*obstructed cable path* otherwise); the edge radius is > 0.4 mm and at most half the panel thickness; height ≤ width; every opening, measured at its flared face size, stays inside the rail opening minus `rail_side_clearance_mm`, `min_wall_mm` clear of rail-screw heads, panel edges and ribs, and `min_wall_mm` from every other opening.
  - rack shelf: `19in` is refused outright (deferred); rail holes are checked as for panels; floor and side walls stay inside the rail opening minus `rail_side_clearance_mm` and clear of the rail screw shanks; floor/wall ≥ `min_wall_mm`; walls (and the device) may not rise above the front plate into the rack space above; the rear lip is no taller than the walls; the shelf must fit `rack_clear_depth_mm`; the measured device plus `fit_clearance_mm` must fit between the walls and in front of the lip (no overhang); vent slots keep `min_wall_mm` to each other, the walls, the front plate and the rear edge; the part must fit the build volume front-face-down or floor-down (refused otherwise, no oversize option).
  - PDU mount: base, walls and strap bar ≥ `min_wall_mm`; the strap slot (strap width/thickness + clearance) leaves `min_wall_mm` at the bracket ends and its bottom sits at the strip top + `fit_clearance_mm`, so the strap only touches the top face; every bracket span (with `keepout_margin_mm`) is clear of every declared top/side/base keepout rectangle along the strip (*keepout violated*), and at least `end_clearance_mm` from both strip ends (*cord/end clearance*); brackets do not overlap; at least two brackets; keepout rectangles must lie on their face; the screw head bears on the ear clear of the wall and the hole keeps `min_wall_mm`; width and height (including the strap looped over the bar) fit the stated space; the bracket fits the build volume base-down.
  - Holes are modelled slightly larger than the stated diameter so the `facets`-sided polygon still clears it.
- **Rendered mesh.** OpenSCAD warnings/errors are fatal. The STL must be non-empty, have no zero-area facets, be a closed and consistently oriented manifold (every edge used exactly twice, in opposite directions), have positive volume, be a single connected shell, and its measured bounding box must equal the validator's prediction within 0.01 mm. ADMesh (read-only run) must report one part and zero repairs.
- **Rounded edges (cable panel).** A test reads the rendered STL and confirms the opening is `cut_h + 2 × radius` tall at both faces, `cut_h` at the neck, and on the quarter circle in between (each of the 6 straight steps per edge ends exactly on the arc; between the ends the chord removes slightly more material than a true round).
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

### Checked by the tool vs. measured by you

| property | checked by the tool | you must measure / decide |
|---|---|---|
| input is finite, bounded, typed, no unknown/duplicate keys, safe name, no overwrite | yes | – |
| holes/slots keep `min_wall_mm` to edges and each other; no zero-thickness features | yes (in the model) | whether `min_wall_mm` is enough for your printer/material |
| retention features present (stop tabs overlap the switch, strap over the top) | yes | strap/cable tie type and that you actually fit it |
| measured port/vent rectangles stay unobstructed by the cradle | yes, against the rectangles you enter | every port, LED, button, vent and cable bend radius on the real switch |
| cable openings pass `cable_bundle_diameter_mm` and stay inside the rail opening | yes | connector/bundle size, cable bend radius behind the panel |
| rail hole positions, spacing, rail opening, panel width | consistency only (and a coarse inch/mm window) | every value, on your rack, at the U you use |
| fits the stated build plate | yes (refused, or flagged with `allow_oversize_print`) | your printer's real usable area |
| mesh is closed, oriented, one shell, bbox as designed | yes (own checker + ADMesh) | – |
| shelf: device footprint fits between walls, in front of the lip, inside the shelf's own panel height | yes, against the device size you enter | the real device, its cables and airflow |
| PDU mount: brackets/straps clear of outlets, switch, vents, cord exit | yes, along the strip, against the keepouts and end clearance you enter | every outlet, plug body, switch, vent and cord on the real strip |
| strength, load, stiffness, heat, fire, creep, electrical safety | **no** – no rating of any kind | whether a printed part is appropriate at all |

## Switch cradle and panels: measuring

**Switch cradle** (top view, then front view). The cradle length is derived: `device_depth_mm + 2 × fit_clearance_mm + 2 × wall_thickness_mm`.

```
 top view (y grows toward the rear)          front view of the switch face (device frame)
 +--+-----------------------------+--+       z ^
 |  |[tab]                   [tab]|  |  y=0    |   +-------------------------------+  device_height_mm
 |  |  .......................... |  |         |   |   [ front_port_zone x0..x1 ] |
 |W |  : switch footprint       : |W |         |   |   [   z0..z1               ] |
 |A |  :  ||  ||  ||  base vents: |A |         |   +--[tab]---------------[tab]---+  stop_tab_height_mm
 |L |  :  ||  ||  ||            : |L |         0---+---------------0---------------+---> x
 |L |  :........................: |L |           -width/2      centreline      +width/2
 |  |[tab]                   [tab]|  |  y=L
 +--+-----------------------------+--+     side view: the side_vent_zone_mm rectangle [y0,y1,z0,z1]
   strap crosses the top and wraps        (y from the switch front face, z from its bottom) is cut
   around the bar above each strap slot    through both walls; the strap slot sits just above the
                                           switch top, with strap_bar_height_mm of wall above it.
```

Measure with calipers: switch width/height/depth (with feet if they stay on); every front and rear region that must stay clear (ports, LEDs, reset button, power jack) as `[x0, x1, z0, z1]` from the switch centreline and bottom; side vent regions as `[y0, y1, z0, z1]` from the front face and bottom; your strap's width and thickness (add clearance); the shelf's hole positions; the free space on the shelf. Leave a port list empty (`[]`) only if that face really has nothing to keep clear. Insert the switch from above, then fit the strap.

**Panels** (front view). Rail holes are mirrored left/right at `±rail_hole_center_spacing_mm / 2`, all at the heights in `rail_hole_z_offsets_mm` measured from the panel's bottom edge.

```
 |<--------------------------- panel_width_mm --------------------------->|
 +------------------------------------------------------------------------+ panel_height_mm
 |  (o=)    .-----------------.        .-----------------.          (=o)  |
 |          (  pass-through   )        (                 )                |
 |  (o=)    '-----------------'        '-----------------'          (=o)  |
 +------------------------------------------------------------------------+ z = 0
     |<----------- rail_hole_center_spacing_mm (hole centres) ------->|
          |<----------- rail_opening_width_mm (between rails) ---->|
 (o=) slotted rail hole: rail_screw_clearance_diameter_mm + rail_slot_extra_length_mm
 cross-section through an opening:  front face  )====(  back face  -> each face edge is a
 quarter round of pass_through_edge_radius_mm; the narrowest part is pass_through_height_mm.
```

The stiffening ribs (if `stiffener_depth_mm` > 0) run along the top and bottom edges of the back face, between `±(rail_opening_width_mm / 2 − rail_side_clearance_mm)`. For a 10-inch rack, **all** of these values are vendor-specific: there is no universal 10-inch hole pattern, and the 10-inch examples use invented values.

### Build plate and full-width panels

A 19-inch panel is 482.6 mm wide. Few hobby printers can print that in one piece; even the diagonal of a 256 × 256 mm plate is only 362 mm. The tool does **not** split panels or add joints: a split panel needs a joint that is validated and assembled, and that is not offered here. Instead:

- `build_plate_x_mm` / `build_plate_y_mm` are required. A panel whose face does not fit in either rotation is refused unless `allow_oversize_print` is `true`.
- With `allow_oversize_print: true` the STL is still generated as one piece, `manifest.json` records `"fits_build_plate": false`, and the orientation note starts with a warning. You then need a large-format printer or another process (e.g. cutting from sheet); the SCAD file is the source to adapt.
- 10-inch panels (~254 mm) fit some 256 mm plates face-down; the example states a 256 × 256 mm plate and fits.

## Rack shelf

```
 side view (section through a side wall)            top view
 z ^                                                 +-------------------------------+ front plate (panel_width_mm)
   |  front plate                                    |  |                         |  |
   |  |                                              |W | ||  ||  ||  ||  ||  ||  |W | vent slots run along the depth,
   |  |   side_wall_height_mm      rear lip          |A | ||  ||  ||  ||  ||  ||  |A | centred between front plate and
   |  |__________________________|                   |L |      device footprint    |L | rear lip
   |  |=========== floor ========|                   |  |__________________________|  |
   0--+--------------------------+--> y               +--+ <- rear lip (optional) -> +--+
     0  t        shelf_depth_mm                           |<-- rail_opening - 2 x rail_side_clearance -->|
```

- **10-inch only.** `rack_width_class: "19in"` is refused with an explicit *deferred* message, whatever build plate is stated. A ~483 mm front plate does not fit common build plates, and a bolt-together split would need a specified joint whose geometry and assembly can be checked; none is implemented. Use a commercial 19-inch shelf.
- The shelf **hangs only from the front rail screws** (a cantilever). There is no rear support and no load rating. `shelf_depth_mm` is capped at 250 mm; keep it shallow and the device light.
- Measure: everything you would measure for a panel, plus `rack_clear_depth_mm` (free depth behind the front face at that height — rear rails, doors, cables) and the device's width, depth and height. The device (with `fit_clearance_mm`) must fit between the walls and in front of the rear lip, and the floor + device must stay within `panel_height_mm`, i.e. inside the rack units the shelf occupies.
- Printing: refused unless it fits your build volume in one of two orientations: **floor down** (needs `panel_width_mm × (panel_thickness_mm + shelf_depth_mm)` on the plate) or **front face down** (needs the front plate on the plate and `panel_thickness_mm + shelf_depth_mm` of height). `derived.printable_orientations` lists what fits. Only bed fit is checked; **neither orientation is claimed to print without supports**. Overhangs in the model: floor down, the rail holes are horizontal round holes (overhanging tops); front face down, the vent slots stay vertical (depth becomes build height) with rounded overhanging tops, and a rear lip (`rear_lip_height_mm` > 0) becomes a horizontal bridge as long as the space between the side walls (211 mm in the 1U example) with nothing under it, which needs supports or reliable long-span bridging. Check the overhang preview in your slicer.
- **Fit coupon:** first print a `blanking_panel` with identical `rail_*`/`panel_*` values and `stiffener_depth_mm: 0` and check it against your rails.

## PDU mount

```
 end view (looking along the strip)                 top view of the strip (length along x)
            strap over the top face                  0                                       device_length_mm
     ___  ____________________________  ___          |end_clr|[B]|  outlets  |[B]|  outlets  | switch |end_clr|
    |bar||                            ||bar|          |       |   |  keepout  |   |  keepout  |keepout |       |
    |slot  .--------------------------.  slot|        [B] = bracket span (bracket_length_mm) + keepout_margin_mm,
    |   | |    power strip body        | |   |        checked against top, side and base keepouts on every face
    |   | |  (outlet face up)          | |   |
 ___|   |_|____________________________|_|   |___
|ear (o)       base                         (o) ear|   z = 0 on the mounting surface
```

- **External, non-electrical accessory.** It does not open, drill, cover or modify the strip; nothing touches the outlets, switch, vents or cord as long as your keepouts are complete. Keepouts on the top face must include plug bodies, not just the sockets.
- One bracket design is generated; print one per entry in `bracket_x_positions_mm` (2–4). The strap (your own hook-and-loop strap or tie) crosses the strip's top face, passes through the slot in each wall and loops around the bar above it. The slot bottom is set at strip top + `fit_clearance_mm`, so the strap never presses on a side face.
- Measure with calipers: strip body length, width and height (without cord and plugs); every outlet, switch, LED, vent, label you must read, feet and side cord exit as rectangles on the face they are on (`x` from the strip's left end viewed from above); how far the cord and its strain relief need from each end (`end_clearance_mm`); your strap's width and thickness; screw head and shank; the space available.
- Mounting surface, screws and orientation (e.g. upside down under a desk) are your choice and responsibility; no holding force is rated, and straps creep.
- **Fit coupon:** print one bracket, seat the unplugged strip and strap it, check nothing is covered, squeezed or bent, then print the rest.

## Slicer starting settings (untested)

> These are **starting points only. None of them has been tried on a printer.** Print a fit coupon first: e.g. in your slicer, cut the model to a thin strip that contains one rail hole and one pass-through (panel) or one stop tab and one shelf hole (cradle), check it against the real rack/switch/screws, then adjust the `tolerance` parameters. There is no load, heat or fire certification.

| setting | starting point | trade-off |
|---|---|---|
| orientation | panels: front face down (stiffeners up, no supports); switch cradle: base down as modelled; bracket/SFF cradle: see above | face-down panels get a flat, smooth face; layer lines then run across the panel's thickness |
| layer height | 0.2 mm (0.4 mm nozzle) | 0.12–0.16 mm renders the rounded pass-through edges more smoothly at the cost of time |
| walls / perimeters | 4 (≥ 1.6 mm) | holes and slots are mostly walls; more walls → stiffer, more accurate holes |
| top / bottom layers | 5 / 5 | – |
| infill | 25–40 % gyroid or grid | panels carry little load; cradles benefit from more around the screw holes |
| supports | none for panels or switch cradle | vent-window and strap-slot tops are bridges; enable supports if your printer bridges poorly over the window length |
| brim | 5–8 mm for long panels | long thin panels tend to lift at the corners, especially in ABS/ASA |
| material | PETG or ASA as a starting point | PLA is stiff and easy but softens at modest temperatures (behind warm equipment it may creep); PETG tolerates more heat and is tougher; ASA/ABS tolerate more heat still but warp – use an enclosure. Check the material against the temperature inside **your** rack. |

## Slicer orientation (unvalidated)

The manifest includes a suggestion; none of it has been tested on a printer.

- Bracket: lay the side flange flat on the bed so layer lines run along the flange; the front flange then stands vertically. Alternatively print front-flange-down. Either way the inner corner sees bending loads across layers in some direction — consider the gusset, more perimeters, or a stronger material, and decide for yourself whether a printed part is appropriate at all.
- Cradle: base down as modelled; the retention lip is an overhang that likely needs supports or a chamfer you add.
- Switch cradle: base down as modelled; vent-window and strap-slot tops are bridges.
- Panels: front face down; stiffeners point up and need no supports.
- Rack shelf: floor down or front face down, whichever fits your stated build volume (the manifest lists both and their overhangs). Front face down turns a rear lip into a long unsupported bridge; see [Rack shelf](#rack-shelf).
- PDU mount: base down as modelled; strap-slot tops are short bridges.

Practical starting slicer settings for all parts are in [Slicer starting settings (untested)](#slicer-starting-settings-untested).

## Parameters

All lengths are millimetres and **all keys are required** (no hidden defaults). `kind`: *measured* – measure the real rack/device; *chosen* – a design choice; *tolerance* – a clearance you tune; *label* – metadata. Machine-readable form with the same text: [`schema/rack-part.schema.json`](schema/rack-part.schema.json) (JSON Schema 2020-12, `x-kind` annotations). Schema ranges are coarse; the cross-field checks above are only enforced by the CLI.

Bracket frame: x = 0 at the device-side face of the side flange (+x toward the rail), y = 0 at the front face (+y into the rack), z = 0 at the bottom edge. Cradle frame (SFF and switch): x centred, y = 0 at the front edge, z = 0 at the base bottom. Panel frame: x centred on the rack centreline, y = 0 at the front face (+y into the rack), z = 0 at the bottom edge. Switch port rectangles use the device face frame: x from the switch centreline (viewed from the front, also for the rear face), z up from the switch bottom.

**Common (all parts)**

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

**`switch_cradle`**

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `switch_cradle` | Selects a drop-in cradle for a small network switch: front/rear stop tabs, strap slots, vent windows, base vent slots and counterbored shelf holes. |
| `device_width_mm` | measured | 30 – 300 | Switch width (left to right, viewed from the front). |
| `device_height_mm` | measured | 10 – 100 | Switch height, including feet if they stay on. |
| `device_depth_mm` | measured | 30 – 300 | Switch depth, front face to rear face (excluding plugs). |
| `fit_clearance_mm` | tolerance | 0 – 5 | Gap added on every side of the switch inside the cradle (left/right, front/back, and above it under the strap slot). |
| `base_thickness_mm` | chosen | >1 – 15 | Base plate thickness. |
| `wall_thickness_mm` | chosen | >1 – 15 | Side wall thickness; also the front/rear stop tab thickness. |
| `stop_tab_width_mm` | chosen | 1 – 60 | How far each front/rear stop tab reaches inward from its side wall. Must overlap the device face by at least min_wall_mm beyond fit_clearance_mm. |
| `stop_tab_height_mm` | chosen | 1 – 60 | Height of the stop tabs above the base top. |
| `front_port_zone_mm` | measured | 0–4 `[a0,a1,b0,b1]`, each -200 – 200 | Front-face regions that must stay unobstructed (ports, LEDs, buttons) as [x0, x1, z0, z1]. 0-4 rectangles. |
| `rear_port_zone_mm` | measured | 0–4 `[a0,a1,b0,b1]`, each -200 – 200 | Rear-face regions that must stay unobstructed (power jack, ports) as [x0, x1, z0, z1]. 0-4 rectangles. |
| `side_vent_zone_mm` | measured | 0–4 `[a0,a1,b0,b1]`, each -10 – 310 | Side vent regions as [y0, y1, z0, z1]: y back from the device front face, z up from the device bottom. A window of exactly this size is cut through both side walls. 0-4 rectangles. |
| `strap_slot_y_mm` | chosen | 1–4 values, each 0 – 300 | Centre of each strap slot pair, measured back from the device front face. The strap crosses the top of the switch and wraps around the bar above each slot. 1-4 values. |
| `strap_slot_length_mm` | tolerance | 3 – 60 | Slot length along the depth: strap width + your clearance. |
| `strap_slot_height_mm` | tolerance | 1 – 10 | Slot height: strap thickness + your clearance. |
| `strap_bar_height_mm` | chosen | >1 – 20 | Height of the wall material above each strap slot that the strap wraps around. |
| `base_vent_slot_count` | chosen | 0 – 40 | Number of rounded vent slots in the base under the switch (0 = none). |
| `base_vent_slot_width_mm` | chosen | >1 – 20 | Width of each base vent slot. |
| `base_vent_slot_length_mm` | chosen | 2 – 300 | Length of each base vent slot (along the depth, centred on the cradle). |
| `base_vent_slot_pitch_mm` | chosen | 2 – 60 | Centre-to-centre spacing of base vent slots. |
| `shelf_hole_positions_mm` | measured | 1–12 `[x,y]`, each -300 – 600 | Mounting holes as [x, y]: x from the cradle centreline, y back from the cradle front edge. Must match holes you measured on your shelf or panel. 1-12 pairs. |
| `shelf_screw_clearance_diameter_mm` | tolerance | >1 – 12 | Through-hole diameter for the shelf screw. |
| `shelf_screw_head_diameter_mm` | measured | >1 – 25 | Shelf screw head diameter (counterbored from the top). |
| `shelf_screw_head_height_mm` | measured | 0 – 15 | Shelf screw head height; the counterbore is this deep so the head sits flush or below the base top. |
| `max_overall_width_mm` | measured | 20 – 600 | Available width on the shelf. |
| `max_overall_depth_mm` | measured | 20 – 700 | Available depth on the shelf. |
| `max_overall_height_mm` | measured | 5 – 600 | Available height above the shelf. |
| `build_plate_x_mm` | measured | 50 – 1000 | Usable build plate size, first axis (measure your printer). |
| `build_plate_y_mm` | measured | 50 – 1000 | Usable build plate size, second axis. |

**`blanking_panel`** and **`cable_panel`** (shared)

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `blanking_panel` | Selects a flat blanking panel that screws to measured rail holes. |
| `part` | label | `cable_panel` | Selects a rack panel with rounded (stadium-shaped, edge-filleted) cable pass-throughs. |
| `rack_width_class` | label | `10in` \| `19in` | Only selects a sanity window for rail_hole_center_spacing_mm (catches inch/mm mix-ups). It does not supply any dimension; there is no universal 10-inch hole pattern. |
| `rail_hole_center_spacing_mm` | measured | 150 – 520 | Horizontal centre-to-centre distance between the left and right rail mounting holes. |
| `rail_opening_width_mm` | measured | 100 – 500 | Clear horizontal opening between the inner edges of the rails over the depth the panel's stiffeners occupy. |
| `panel_width_mm` | measured | 120 – 520 | Overall panel width: the space available in front of the rails (19-inch nominal 482.6; 10-inch: measure). |
| `panel_height_mm` | chosen | 10 – 400 | Panel height. Usually a little under n x 44.45 mm so neighbouring panels do not touch. |
| `panel_thickness_mm` | chosen | >1 – 10 | Front plate thickness. |
| `rail_hole_z_offsets_mm` | measured | 1–8 values, each 0 – 400 | Rail hole centres used, measured upward from the panel bottom edge as it will sit in the rack. The same heights are used on the left and right rail. 1-8 values. |
| `rail_screw_clearance_diameter_mm` | tolerance | >1 – 12 | Through-hole diameter for the rack screw (screw major diameter + your clearance). |
| `rail_screw_head_diameter_mm` | measured | >1 – 25 | Rack screw (or washer) head diameter; the head must bear fully on the panel. |
| `rail_slot_extra_length_mm` | tolerance | 0 – 15 | Horizontal slot elongation of each rail hole for side-to-side adjustment (0 = round hole). |
| `rail_side_clearance_mm` | tolerance | 0 – 10 | Gap kept between anything behind the panel (stiffening ribs, shelf floor and walls) and the inner rail edge. |
| `stiffener_depth_mm` | chosen | 0 – 40 | Depth of the two horizontal stiffening ribs along the top and bottom edges of the back face (0 = flat plate). Ribs stop inside the rail opening. |
| `stiffener_thickness_mm` | chosen | >1 – 10 | Vertical thickness of each stiffening rib (ignored when stiffener_depth_mm is 0, but still required). |
| `build_plate_x_mm` | measured | 50 – 1000 | Usable build plate size, first axis (measure your printer). |
| `build_plate_y_mm` | measured | 50 – 1000 | Usable build plate size, second axis. |
| `allow_oversize_print` | chosen | `true` \| `false` | false: refuse a panel whose face does not fit the build plate in either rotation. true: generate it anyway and flag it in the manifest; no split/joint is produced, so you need a larger printer or another process. |

**`cable_panel`** only

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `pass_through_centers_mm` | chosen | 1–8 `[x,z]`, each -260 – 400 | Pass-through centres as [x, z]: x from the rack centreline (+x to the right, viewed from the front), z up from the panel bottom edge. 1-8 pairs. |
| `pass_through_width_mm` | chosen | 5 – 400 | Narrowest (mid-thickness) width of each horizontal stadium opening, end to end. |
| `pass_through_height_mm` | chosen | 5 – 200 | Narrowest (mid-thickness) height of each opening; also the diameter of its round ends. Must not exceed the width. |
| `pass_through_edge_radius_mm` | chosen | >0.4 – 10 | Radius of the rounded edge on both faces around each opening; at most half the panel thickness. The opening on each face is wider and taller than the neck by twice this radius. |
| `cable_bundle_diameter_mm` | measured | 1 – 150 | Largest connector or cable bundle diameter that must pass through; the opening neck must be at least this tall. |

**`rack_shelf`**: the eleven fields from `rack_width_class` to `rail_side_clearance_mm` in the panel table above, plus:

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `rack_shelf` | Selects a shallow ventilated shelf: a front plate on measured rail holes with a cantilevered floor, side walls, optional rear lip and vent slots. Only 10-inch is generated; 19-inch is refused (deferred). |
| `rack_clear_depth_mm` | measured | 20 – 1000 | Free depth behind the panel front face (to rear rails, door, wall or cables) at the shelf's height. |
| `shelf_depth_mm` | chosen | 40 – 250 | Floor depth behind the front plate. The shelf hangs only from the front rail screws; keep it shallow. |
| `floor_thickness_mm` | chosen | >1 – 10 | Floor thickness (at least min_wall_mm). |
| `side_wall_height_mm` | chosen | 3 – 300 | Height of the two side walls above the floor top. They are the shelf's main stiffeners. |
| `side_wall_thickness_mm` | chosen | >1 – 10 | Side wall (and rear lip) thickness. |
| `rear_lip_height_mm` | chosen | 0 – 300 | Height of a lip along the rear floor edge above the floor top (0 = none). At most side_wall_height_mm. |
| `vent_slot_count` | chosen | 0 – 40 | Number of rounded vent slots through the floor (0 = none). |
| `vent_slot_width_mm` | chosen | >1 – 20 | Width of each vent slot (across the rack). |
| `vent_slot_length_mm` | chosen | 2 – 250 | Length of each vent slot along the depth, centred on the floor between front plate and rear lip. |
| `vent_slot_pitch_mm` | chosen | 2 – 60 | Centre-to-centre spacing of vent slots across the rack. |
| `device_width_mm` | measured | 10 – 500 | Width of the device that will sit on the shelf. |
| `device_depth_mm` | measured | 10 – 600 | Depth of that device, including anything sticking out at the back. |
| `device_height_mm` | measured | 5 – 400 | Height of that device including feet. |
| `fit_clearance_mm` | tolerance | 0 – 10 | Gap required between the device and each side wall, and between the device and the rear lip. |
| `build_plate_x_mm` | measured | 50 – 1000 | Usable build plate size, first axis (measure your printer). |
| `build_plate_y_mm` | measured | 50 – 1000 | Usable build plate size, second axis. |
| `build_plate_z_mm` | measured | 20 – 1000 | Usable build height. |

**`pdu_mount`** (strip-face rectangles: `x` from the strip's left end viewed from above, second axis across that face)

| parameter | kind | allowed | meaning |
|---|---|---|---|
| `part` | label | `pdu_mount` | Selects an external strap-retained power-strip/PDU bracket. One bracket is generated; print one per entry in bracket_x_positions_mm. Non-electrical accessory: it never touches outlets, switches, vents or the cord exit, and needs no change to the strip's case. |
| `device_length_mm` | measured | 60 – 700 | Strip body length (end to end, excluding cord and plugs). |
| `device_width_mm` | measured | 15 – 150 | Strip body width across the face that rests on the bracket base. |
| `device_height_mm` | measured | 10 – 100 | Strip body height from the face on the base to the opposite (top) face. |
| `fit_clearance_mm` | tolerance | 0 – 5 | Gap between the strip and each side wall, and between the strip top and the strap slot bottom. |
| `top_face_keepouts_mm` | measured | 0–12 `[a0,a1,b0,b1]`, each 0 – 700 | Regions on the face opposite the base (usually outlets, switch, LEDs, including plug bodies) as [x0, x1, y0, y1]: x from the strip's left end, y across the face from its near (-y) edge. 0-12 rectangles. |
| `side_face_keepouts_mm` | measured | 0–12 `[a0,a1,b0,b1]`, each 0 – 700 | Regions on either long side face (vents, switch, side outlets, side cord exit) as [x0, x1, z0, z1]: z up from the base face. Applied to both sides. 0-12 rectangles. |
| `base_face_keepouts_mm` | measured | 0–12 `[a0,a1,b0,b1]`, each 0 – 700 | Regions on the face resting on the base (vents, labels you need to read, feet) as [x0, x1, y0, y1]. 0-12 rectangles. |
| `keepout_margin_mm` | tolerance | 0 – 30 | Extra distance kept along the strip between each bracket (and its strap) and every keepout region. |
| `end_clearance_mm` | measured | 0 – 150 | Minimum distance from each bracket to both strip ends (cord exit, strain relief, end switches). |
| `bracket_x_positions_mm` | chosen | 2–4 values, each 0 – 700 | Bracket centres along the strip, from its left end. 2-4 values (one bracket would let the strip pivot). |
| `bracket_length_mm` | chosen | 8 – 100 | Bracket length along the strip. |
| `base_thickness_mm` | chosen | >1 – 15 | Base plate and ear thickness. |
| `wall_thickness_mm` | chosen | >1 – 15 | Side wall thickness. |
| `strap_width_mm` | measured | 3 – 60 | Width of the strap you will use (hook-and-loop, tie). |
| `strap_thickness_mm` | measured | >0.2 – 10 | Strap thickness. |
| `strap_clearance_mm` | tolerance | 0 – 5 | Added to strap width and thickness to size the wall slots. |
| `strap_bar_height_mm` | chosen | >1 – 20 | Wall material above each strap slot that the strap wraps around. |
| `ear_width_mm` | chosen | 4 – 60 | Width of the screw ear on each side of the walls. |
| `mount_screw_clearance_diameter_mm` | tolerance | >1 – 10 | Through-hole diameter for the mounting screw, one per ear. |
| `mount_screw_head_diameter_mm` | measured | >1 – 20 | Mounting screw (or washer) head diameter; it must bear on the ear clear of the wall. |
| `max_overall_width_mm` | measured | 20 – 600 | Available width on the mounting surface across the strip. |
| `max_overall_height_mm` | measured | 10 – 300 | Available height off the mounting surface (e.g. gap to a door or side panel), including the strap loop. |
| `build_plate_x_mm` | measured | 50 – 1000 | Usable build plate size, first axis (measure your printer). |
| `build_plate_y_mm` | measured | 50 – 1000 | Usable build plate size, second axis. |
| `build_plate_z_mm` | measured | 20 – 1000 | Usable build height. |

## Example fixtures (synthetic)

| file | what it is |
|---|---|
| `examples/bracket-19in-1u-synthetic.json` | right bracket, EIA-310 nominal rail spacing/opening and 1U hole pattern, for a made-up 430 mm wide box with made-up side screws |
| `examples/bracket-10in-1u-synthetic.json` | left bracket for a made-up 10-inch rack (236.5 mm hole spacing, 220 mm opening are invented sample values, not a standard) and a made-up 200 mm wide box |
| `examples/sff-cradle-synthetic.json` | cradle for a made-up 180 × 60 × 190 mm box, flat, on a made-up shelf hole pattern |
| `examples/switch-cradle-synthetic.json` | switch cradle for a made-up 160 × 28 × 100 mm switch with made-up port and vent zones |
| `examples/blank-10in-1u-synthetic.json` | 10-inch 1U blanking panel; 236.5 mm hole spacing, 220 mm opening and 254 mm width are invented sample values, not a 10-inch standard; fits a 256 mm plate |
| `examples/blank-19in-1u-synthetic.json` | 19-inch 1U blanking panel from EIA-310 nominal summary values; **does not fit** the stated 256 mm plate (`allow_oversize_print: true`) |
| `examples/cable-10in-1u-synthetic.json` | 10-inch 1U cable panel, two 70 × 16 mm pass-throughs with 1.5 mm rounded edges (same invented rack values as above) |
| `examples/cable-19in-2u-synthetic.json` | 19-inch 2U cable panel, three 100 × 30 mm pass-throughs; **does not fit** the stated plate (`allow_oversize_print: true`) |
| `examples/shelf-10in-1u-synthetic.json` | 10-inch 1U shelf, 150 mm deep, 16 vent slots, rear lip, for a made-up 180 × 120 × 30 mm device; same invented rack values as the 10-inch panels; fits a 256 mm build volume both ways |
| `examples/shelf-10in-2u-synthetic.json` | 10-inch 2U shelf, 220 mm deep, no rear lip, 10 wider vent slots, made-up 150 × 210 × 70 mm device, 300 mm build volume |
| `examples/pdu-strip-synthetic.json` | two brackets for a made-up 300 × 55 × 40 mm strip with two outlet groups and a switch on top, a side vent, feet underneath; brackets sit at the left end and between the outlet groups |
| `examples/pdu-compact-synthetic.json` | two brackets at the ends of a made-up 160 × 40 × 30 mm strip with one outlet block; thinner strap, smaller screws |
| `tests/fixtures/*.json` | inputs that must be refused (19-inch shelf (deferred), shelf larger than the build volume, device taller than the shelf's panel, PDU bracket over an outlet keepout, PDU bracket too close to the cord end, zero thickness / out-of-range size, bracket too wide for the rail opening, counterbore too deep, lip pressing on the device, NaN, stop tab over a port, missing retention, vent window against a strap slot, oversize panel without the flag, overlapping rail holes, overlapping pass-throughs, edge radius over half the thickness, cable bundle larger than the opening) |

"Synthetic" means the numbers were made up (or, for 19-inch rails, taken from standard summaries) to exercise the generator. A **real fit** needs your own measurements of your rack, switch, screws, straps and printer, plus a printed fit coupon.

## Limitations

- No strength, load, temperature or fire rating; no claim that a printed part can safely carry any device. Rack-mounted equipment can be heavy; use the device's own rack kit or a proper shelf when in doubt.
- No physical print, rack installation, or hardware test has been done. Fit depends on your measurements, printer, material and slicer.
- Seven part shapes. No cage-nut, threaded-insert or rear-support features. Panels are single pieces: there is no split/joint option, so 19-inch panels need a large printer or another process (see above).
- Switch cradle: port/vent clearance is checked only against the rectangles you enter, not against cable bend radius or plug bodies; the strap is your own hardware; insertion is from above.
- Cable panel: the rounded edge is a 6-step polygonal approximation of a quarter round (each step ends on the arc); there are no brush strips or covers.
- Rack shelf: 10-inch only; 19-inch is deferred (no bolt-together joint). Cantilevered from the front rail screws only, with no rear support, no load rating and no deflection estimate. Vent slots are not an airflow or thermal design; the device's own cooling requirements are yours to check.
- PDU mount: keepout and cord clearance are checked only along the strip against what you enter; the tool cannot know where your outlets, plug bodies or cords really are. Holding force, strap creep, vibration and fire behaviour of the printed part are unknown. Not an electrical product; it must not be used to modify, cover or support a strip in a way its maker does not allow.
- Starting slicer settings are untested suggestions.
- Self-intersection is not tested directly (see above).
- Not every bad idea is detectable: the validator checks the relationships listed above, nothing more.

## License and provenance

MIT — see [`LICENSE`](LICENSE). All code, SCAD modules and example data in this directory were written for this repository (with AI assistance via Claude Code) and contain no third-party geometry, vendor drawings, or measurements of any real rack or device. Standard dimensions are cited from public summaries of EIA/ECIA-310-E and IEC 60297-3-100 as noted above. The switch cradle, blanking panel, cable panel, rack shelf and PDU mount are generic CAD written for this repository under the same MIT license; they are not derived from any vendor's design, and no switch, power-strip or rack vendor's dimensions are included.
