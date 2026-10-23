# tape-label-gen — LTO barcode label sheets

**Generates Code 39 LTO cartridge labels as print-ready PDF (CLI) or SVG pages
(static web page). Tested by rendering the output and decoding it back with a
real barcode reader library; it has not been printed on any vendor's label
stock or scanned by any tape library, and no compatibility is claimed.**

**Reproduce:**
`python3 -m unittest discover -s tests` (stdlib only; the 6 decode tests skip) —
with `pip install --require-hashes --only-binary=:all: -r requirements-test.txt`
first they all run — and `node --test` for the page module.

## The label

Eight characters, Code 39 (USS-39), no check character, `*` start/stop:

| part | rule |
|---|---|
| volser | 6 characters, `A–Z` and `0–9` only, uppercase |
| media ID | `L5`–`L9` (LTO-5..9 data), `LV`–`LZ` (LTO-5..9 WORM), `CU` (universal cleaning) |
| cleaning | `CU` needs a `CLN…` volser, and a `CLN…` volser must be `CU`: libraries treat `CLN` as cleaning |

Geometry (nominal, mm): narrow element X = 0.423, wide = 2.75 X, inter-character
gap = X, so the 10-character symbol is 151.5 X = 64.08 mm wide. On a 78 mm
label that leaves 6.96 mm quiet zones each side (USS-39 minimum is 10 X = 4.23).
Bars are 11.9 mm tall (minimum 11.1) under a 4.2 mm band of human-readable
Courier-Bold text. X, ratio and minimum bar height are the values IBM's library
documentation gives for LTO labels; the full LTO label specification was not
available while writing this, so check its tolerances before relying on them.

Not supported: LTO-1..4 (`L1`–`L4`, `LR`–`LU`), LTO-10 (`LA`/`LH` in some
vendors' lists), Type M (`M8`), Code 39 Full ASCII, colour bands.

**Mod-43 look-alikes.** About 1 label in 43 ends in a character that happens to
equal the Code 39 mod-43 check of its first seven (`STUVWXLW` is one). ZXing
reports those as `]A1`, "check character validated". A reader configured to
*strip* Code 39 check characters would hand the library seven characters. The CLI
and page print a note when a batch contains any; it is not an error, but make
sure readers are not set to strip check characters.

## CLI

`tapelabel.py` is one file, Python 3.12+, standard library only.

```console
$ python3 tapelabel.py validate TAP001L9 CLN001CU TAP001L4 tap001L9
ok TAP001L9 (LTO-9)
ok CLN001CU (universal cleaning)
error: 'TAP001L4': unknown media ID 'L4' (known: L5, L6, L7, L8, L9, LV, LW, LX, LY, LZ, CU)
error: 'tap001L9': volser must be 6 of A-Z 0-9 (uppercase only)
$ echo $?
1
$ python3 tapelabel.py pdf --first TAP000 --count 40 --media L9 -o tap.pdf
$ python3 tapelabel.py pdf AAA001L8 AAA007L8 --stock single -o -  > two.pdf
```

`--first` increments the trailing digits (`TAP098` → `TAP099`, `TAP100`) and
refuses a range that would overflow them. `validate --strict` and `pdf --strict`
also refuse volsers that mix `O`/`0` or `I`/`1`. Exit codes: 0 ok, 1 invalid
label (`validate`), 2 refused (`pdf`: bad label, duplicate, bad stock, I/O).

Output is deterministic (same arguments, same bytes) and has no fonts embedded:
the text uses the PDF standard Courier-Bold.

### Stocks

| name | sheet | labels |
|---|---|---|
| `letter-2x15` | US Letter | 2 × 15, 78 × 16.9 mm, 10 mm column gap, centred |
| `a4-2x16` | A4 | 2 × 16, 78 × 16.9 mm, 10 mm column gap, centred |
| `single` | 78 × 16.9 mm page | 1, for label/roll printers |

These are generic geometries, not any vendor's product. Measure your stock and
give it as JSON if it differs (all mm; `left`/`top` are the first label's
top-left corner, `pitch_*` the distance between label corners):

```json
{"page_w": 215.9, "page_h": 279.4, "cols": 2, "rows": 15, "left": 24.95, "top": 12.95,
 "pitch_x": 88.0, "pitch_y": 16.9, "label_w": 78.0, "label_h": 16.9}
```

`--stock-json FILE` refuses a layout that runs off the page, overlaps, or is too
small for the quiet zones or minimum bar height. Barcodes are never scaled.
For a partly used sheet, `--skip N` leaves the first N positions empty. For
printer drift, print once with `--outline` on plain paper, hold it over a label
sheet against the light, and correct with `--offset-x/--offset-y` (mm). An
offset that would push any label position (and so its quiet zones or bars) off
the page is refused, in the CLI and the page alike; the error gives the allowed
range. Offsets must be finite, and stock values must lie between 0 and 5080 mm
(PDF's 200-inch page limit), so `1e308`, `1e400`, `NaN` and `Infinity` in the
JSON are refused. Print at 100 % / "actual size": any fit-to-page scaling
changes X.

## Web page

`index.html` + `label.js`: the same tables and rules, drawing SVG pages sized in
mm and setting `@page` to the stock, so the browser's print dialog produces the
sheet. No libraries, no network. Serve the directory (ES modules do not load
over `file://`):

```console
$ python3 -m http.server -d tape-label-gen 8000   # then open http://localhost:8000/
```

It covers ranges on the built-in stocks; custom stock JSON is CLI-only.

## Tests

| file | what |
|---|---|
| `tests/test_tapelabel.py` | stdlib: Code 39 table shape, symbol width, volser and media rules, range overflow, stock refusals, PDF xref and page structure, determinism, CLI exit codes |
| `tests/test_decode.py` | renders the PDF with PDFium and decodes it with ZXing (`requirements-test.txt`): every character and media ID, every stock, skip and offset, 150 dpi, decoded text and position per cell, the PDF text matching the bars, and bar widths measured at 1200 dpi within 1.5 px of X / 2.75 X |
| `label.test.js` | `node --test`: `label.js` tables equal `tests/golden.json`, rules agree with the CLI, SVG bar positions equal the PDF's |
| `tests/golden.json` | `python3 tapelabel.py spec-json`; both implementations are tested against it, so they cannot drift apart silently |

The decode tests skip without their two packages unless
`TAPELABEL_REQUIRE_DECODE=1` (CI sets it), in which case a missing package fails.
The hashed wheels are Linux x86_64. The web page's rendering is not decoded in CI;
it was checked once by hand (Chromium screenshot and print-to-PDF, both decoded
by ZXing, print widths within 1 px at 1200 dpi).

## Licence

MIT, see `LICENSE`.
