# LTO capacity planner

A static, dependency-free web calculator. Give it a dataset size, a growth rate, a
retention period, a number of copies and a compression assumption; it tells you, for
each LTO generation from LTO-7 to LTO-10, how many cartridges you need, how many
library slots they take, and a media-cost range. The plan lives in the URL, so a
link reproduces it exactly.

It is arithmetic, not a vendor sizing tool: it knows nothing about your backup
software, your library's model, or your data's real compressibility.

## Use it

ES modules will not load over `file://`, so serve the folder:

```sh
node serve.mjs          # http://127.0.0.1:8944/ (binds localhost only)
# or: python3 -m http.server 8944 --bind 127.0.0.1
```

Nothing is fetched at runtime; the page works offline.

## What it computes

All capacities are decimal terabytes (10^12 bytes), the unit LTO cartridges are sold
in. TiB/PiB inputs are converted, which matters: 100 PiB is 112.6 PB.

| quantity | formula |
|---|---|
| data per copy, year *y* | `size × (1 + growth)^y`, compounded yearly |
| planned size | the largest of those over 0…retention years (so negative growth plans for today) |
| TB per cartridge | `native × compression ratio × fill %` |
| cartridges per copy | `ceil(planned size / TB per cartridge)` |
| cartridges total | `per copy × copies` |
| library slots | `ceil(per copy × copies kept in the library × (1 + spare %))` |
| media cost | `cartridges total × [low, high] price` |

The year-by-year table shows how many cartridges (all copies) you own by the end of
each year, and never goes down: shrinking data does not un-buy tapes.

Assumptions you should argue with:

- **Retention** is read as "everything written must still be on tape at the end of
  this period". The model does not expire or recycle anything mid-period, and it
  does not model backup schedules (full/incremental chains, GFS rotations). If your
  retention means "keep N weekly fulls", enter the size of N fulls as the dataset.
- **Compression** defaults to 1:1. The 2.5:1 figure on every LTO box is a rated
  ratio, not a promise; video, images, already-compressed archives and anything
  encrypted before it reaches the drive get roughly 1:1.
- **Fill** defaults to 90%: cartridges are rarely filled to the byte (LTFS index
  partitions, append boundaries, end-of-job leftovers).
- **Spare** slots (default 10%) cover scratch tapes, cleaning cartridges and room to grow.
- **Prices** are placeholders, not quotes (see below).

## Capacities and sources

| generation | native | rated compressed (2.5:1) | source |
|---|---|---|---|
| LTO-7 | 6 TB | 15 TB | <https://www.lto.org/lto-7/> |
| LTO-8 | 12 TB | 30 TB | <https://www.lto.org/lto-8/> |
| LTO-9 | 18 TB | 45 TB | <https://www.lto.org/lto-9/>; [StorageNewsletter, 2020-09-10](https://storagenewsletter.com/2020/09/10/lto-9-arriving-as-specs-available) |
| LTO-10 | 30 TB | 75 TB | [LTO Program press release, 2025-08-13](https://www.businesswire.com/news/home/20250813251001/en/) |
| LTO-10, 40 TB cartridge | 40 TB | 100 TB | [LTO Program announcement via StorageNewsletter, 2025-11-19](https://www.storagenewsletter.com/2025/11/19/lto-program-announces-new-40tb-lto-10-tape-cartridge-specifications/) |

How these were checked (2026-10-23): the figures were cross-checked against search
results quoting the LTO Program releases above, plus vendor media listings (Dell,
Fujifilm) that agree on native capacity and 2.5:1. The build environment could not
fetch lto.org directly, so the lto.org links are the canonical pages rather than
pages re-read on that date. A unit test pins the table so any change is deliberate.

The 40 TB LTO-10 cartridge is listed separately because it coexists with the 30 TB
one and needs an LTO-10 drive. Not modelled: LTO-7 Type M (M8) cartridges (9 TB
when initialised in an LTO-8 drive), drive-to-media compatibility, and drive
counts or throughput.

## Prices

The default per-cartridge USD ranges in `planner.js` (`DEFAULT_PRICES`) are rough
street-price guesses, **not sourced and not quotes**. Media prices vary by vendor,
volume, region and month. Open *Cartridge prices* in the form and put in your own;
changed prices travel in the shared URL. Cost covers cartridges only.

## Shareable URL

Only values that differ from the defaults are written, e.g.
`?s=2.5&u=PB&g=25&y=7&c=3&p_lto10=180-260`. Keys: `s` size, `u` unit, `g` growth %,
`y` retention years, `c` copies, `l` copies in library, `r` compression ratio, `f`
fill %, `x` spare %, `p_<gen>=low-high` prices. Out-of-range values are clamped and
anything unparseable falls back to the default.

## Files

| file | what it is |
|---|---|
| `planner.js` | all the math as a pure ES module (no DOM, no Node globals); the browser and the tests load the same file |
| `planner.test.js` | unit tests on Node's built-in runner |
| `serve.test.js` | checks the local server rejects malformed and out-of-folder paths without crashing |
| `index.html`, `app.js`, `style.css` | the page; no libraries |
| `serve.mjs` | tiny localhost-only static server for local use and the smoke test |
| `e2e/smoke.spec.mjs`, `playwright.config.mjs` | one Playwright smoke test |
| `package.json`, `package-lock.json` | dev-only: pins `@playwright/test`. The calculator itself has no dependencies |

## Tests

```sh
node --test                       # unit + server tests, Node 22+, no install
npm ci && npx playwright install chromium && npx playwright test   # smoke test
```

The smoke test loads the page, edits inputs, checks the table recomputes and the URL
updates, then opens that URL in a fresh page and checks the plan is restored. To use
an already-installed Chromium instead of downloading one, set `PW_CHROMIUM_PATH`.

## Licence

MIT — see `LICENSE`.
