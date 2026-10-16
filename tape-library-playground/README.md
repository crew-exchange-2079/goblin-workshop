# Tape library playground

A browser toy that simulates a robotic cartridge tape library, so you can watch why
scheduling reads off tape is harder than scheduling reads off disk. It is a
**teaching simulator, not an operational controller**: it does not talk to any
hardware and is not meant to drive a real library.

**Look at:** [`shots/2-big-restore.png`](shots/2-big-restore.png) — the FIFO policy
at minute 25 of the "Big restore" scenario (30 of 197 files served so far, queue 167,
31 mounts for 30 reads), next to [`shots/4-drive-failure.png`](shots/4-drive-failure.png) —
drive 2 failed at minute 12, its cartridge stranded inside it, its in-flight file
put back on the queue.

**Reproduce the numbers:** `node --test && node compare.mjs` — runs the 37 tests, then
prints the four "same seed, four policies" tables. Open `index.html` through any
static server to play with it.

You watch one robot, a few drives and a wall of slots while read requests arrive, and
you can switch scheduling policy live to see the queue flow or stall. Everything in it
is invented: the library, the cartridge labels (`CRW001L9`), the files
(`blk_0042.img`), the workload and every cost. It models no real site, product,
customer or person, and its costs are illustrative, not measured or calibrated against
any physical tape, drive or vendor.

## What is here

| File | What it is |
|---|---|
| `sim.js` | the whole simulator as a pure module — library, robot, drives, seeks, reads, 4 policies, 4 scenarios. No DOM, no `process`, no `Math.random`; a test asserts that, so the browser loads the same file the tests exercise |
| `sim.test.js` | 37 tests on Node's built-in runner (`node --test`), no dependencies |
| `compare.mjs` | CLI: runs every scenario under every policy headless and prints the table (`node compare.mjs [scenario] [--seed N]`) |
| `index.html` + `ui.js` + `style.css` | the browser front end: no libraries, no network, works offline |
| `TEACHES.md` | five lessons the toy is meant to illustrate, with the simulator numbers behind them |
| `compare.txt` | the four tables exactly as `node compare.mjs` prints them |
| `shots/` | six screenshots of the UI captured from headless Chromium (see below) |
| `LICENSE` | MIT |

## How to use it

Serve the directory and open `index.html` (ES modules will not load over
`file://`):

```
python3 -m http.server 8943 --bind 127.0.0.1
```

Pick a scenario and a policy from the dropdowns, or use the keyboard:
<kbd>space</kbd> play/pause · <kbd>[</kbd> <kbd>]</kbd> speed · <kbd>1</kbd>–<kbd>4</kbd>
scenario · <kbd>F</kbd> <kbd>B</kbd> <kbd>P</kbd> <kbd>U</kbd> policy. **Compare
policies** runs the current seed under all four policies headless and shows the
table in the page. Switching scenario or policy mid-run restarts that
combination from the same seed, so two policies are always directly comparable.

URL parameters (used for the screenshots, and useful for linking to a state):
`?scenario=big-restore&policy=fifo&autostart=1&speed=10&t=1500` — `t=` fast-forwards
the simulation that many simulated seconds before drawing the first frame, which
makes a capture reproducible.

## Tests and the comparison table

`node --test` runs the 37 tests (determinism, conservation, the batch invariant,
aging, drive failure, and that `sim.js` loads in a browser). `node compare.mjs`
prints no wall-clock values, so the same seed always prints the same table;
`compare.txt` is that output, and CI regenerates it and fails on any difference.
The tests cover `sim.js` only. `ui.js` gets a syntax check (`node --check ui.js`)
and the screenshots; there is no automated browser or click-through test.

## Screenshots

`shots/` holds six screenshots captured from the UI in headless Chromium:
four desktop views, one per scenario (`1-quiet-morning`, `2-big-restore`,
`3-urgent-preempt`, `4-drive-failure`), one phone-sized view (`5-phone`, 390×844)
and one with reduced motion forced (`6-reduced-motion`). Five are 1280×720 and one is
390×844. They are synthetic UI captures of invented data, not evidence from any
physical device.

## Limitations

1. **No tape-wear cost.** Every mount spins media, but nothing is charged for it: no
   policy is penalised for thrashing cartridges. "Tape passes" is reported, never
   costed. A policy that looks good here may look worse once wear matters.
2. **One robot only.** The robot is 34–57% busy across the scenarios here. Adding a
   second robot would be a structural change to the job queue, not a parameter.
3. **Cartridges are unrealistically empty.** `TAPE_MB` says 1.2 TB per tape, but each
   cartridge holds only 4–18 invented files, to keep the library drawable. A dense
   variant (thousands of small files per cartridge) would change the batching
   economics and is not built.
4. **No scenario demonstrates starvation of normal-priority work.** The selected
   scenarios use finite urgent bursts and do not show it. Policy 4's capped aging
   keeps an old normal request's score below a fresh urgent one, but dispatch still
   picks eligible urgent work first, so aging does not guarantee starvation cannot
   happen under sustained urgent arrivals. No uncapped-priority policy is
   selectable, so that failure mode can be read about but not poked.
5. **No automated UI test.** See above: syntax check plus screenshots only.
6. **Accessibility is partial.** Keyboard shortcuts, labels and an `aria-live` event
   log are present. No screen-reader pass and no automated accessibility audit has
   been done. Under `prefers-reduced-motion` the robot is hidden entirely rather than
   replaced by a text readout.
7. **Chromium only.** Screenshots come from Chromium; Firefox and WebKit were not
   tried.
8. **No real-phone testing.** The phone view is a 390×844 headless capture only.
9. **Costs are illustrative.** Every default in `DEFAULT_COSTS` is invented. None is
   measured or taken from a vendor datasheet or public specification, and the
   simulator makes no claim about any physical tape, drive or library. Seek-time
   differences between policies are differences in this model; they do not establish
   real-world time or money savings.
10. **Not packaged or deployed.** It runs from a static file server you start
    yourself; there is no npm package and no hosted copy.

## License

MIT — see [`LICENSE`](LICENSE).
