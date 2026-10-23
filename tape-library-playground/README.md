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

**Reproduce the numbers:** `node --test && node compare.mjs` — runs the 84 unit tests,
then prints the four "same seed, four policies" tables. Open `index.html` through any
static server to play with it, or `lessons.html` for the five guided operator lessons.

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
| `sim.test.js` | 37 tests of the simulator on Node's built-in runner (`node --test`), no dependencies |
| `lessons.js` | the guided lessons as a pure module: an operator-level library model (console commands, I/E door, cleaning cartridges, TapeAlert flags, robot jam, inventory) plus the step checker and the five lessons. Same purity rules as `sim.js` |
| `lessons.test.js` | 47 tests of the lessons: each is solvable by its script and by its hints, steps never advance on a false goal, the library refuses what an operator must not do, no cartridge is lost or duplicated |
| `lessons.html` + `lessons-ui.js` | the lesson page: picker, library view, console, step list, hint button. Static, no libraries |
| `e2e/` + `playwright.config.mjs` | Playwright smoke tests of both pages in Chromium at desktop and phone size, served by a dependency-free static server (`e2e/serve.mjs`) |
| `compare.mjs` | CLI: runs every scenario under every policy headless and prints the table (`node compare.mjs [scenario] [--seed N]`) |
| `index.html` + `ui.js` + `style.css` | the browser front end: no libraries, no network, works offline |
| `TEACHES.md` | five lessons the toy is meant to illustrate, with the simulator numbers behind them |
| `compare.txt` | the four tables exactly as `node compare.mjs` prints them |
| `shots/` | seven screenshots of the UI captured from headless Chromium (see below) |
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

## Guided lessons

`lessons.html` is a second page with five short drills from the operator's side of
the library — the part `sim.js` does not model. You type commands into a small
console (`load 3 D1`, `tapealert D2`, `door open`, `inventory`, …; `help` lists
them) and a step list tells you what to do next:

| # | Lesson | What it teaches |
|---|---|---|
| 1 | Mount and unmount a tape | find a cartridge, thread it, verify it, return it to its home slot |
| 2 | Run a cleaning cycle | TapeAlert 20 *Clean now*; an expired cleaner (22); exporting the dead cleaner |
| 3 | Swap tapes through the I/E slots | export, the I/E door as an airlock, scan on close, import |
| 4 | A failing drive and its TapeAlert flags | 3 + 30 *Hardware A* means the drive, not the tape; offline it empty; prove the media in another drive |
| 5 | Inventory after a robot jam | reset, the library refusing to move on an unverified inventory, a full rescan, putting a hand-swapped pair back |

**The step checker** asks, after every command, whether the current step's goal is
now true of the library — "CRW003L9 is in D1", "D2 has no flags" — not whether you
typed a particular string, so any correct path counts (mount by barcode or by slot,
park a tape anywhere on the way home). A few steps are about *finding something
out* and pass when the relevant command ran (`status`, `tapealert`, a successful
`verify`). Legal-but-bad moves (loading data into a drive that wants cleaning,
cleaning a drive with a hardware fault) get a warning rather than a block. **Hint**
puts a command that makes progress into the console; it is derived from the live
state, so it still works after a detour. Finished lessons are remembered in this
browser's `localStorage` (and simply forgotten if storage is blocked). `?lesson=robot-jam`
opens a lesson directly.

The console syntax is invented (mtx-flavoured, but no real product's CLI). TapeAlert
flag numbers and names follow the public TapeAlert table; which flags a drive raises
and when is a teaching simplification. Lesson times reuse `DEFAULT_COSTS` from
`sim.js` and are just as illustrative.

## Tests and the comparison table

`node --test` runs 84 tests: 37 for the simulator (determinism, conservation, the
batch invariant, aging, drive failure, and that `sim.js` loads in a browser) and 47
for the lessons (see the table above). `node compare.mjs` prints no wall-clock
values, so the same seed always prints the same table; `compare.txt` is that output,
and CI regenerates it and fails on any difference.

`npx playwright test` (after `npm ci`; uses Chromium) runs the browser smoke tests
against both pages at desktop and phone (Pixel 7) size: the playground draws its
library and its clock advances; the lesson picker lists all five; every lesson can
be finished by typing its solution into the console; the hint button works; the
jam lesson refuses a move before `inventory`; progress survives a reload; no page
logs an error or scrolls sideways. CI runs them in a separate job. They are smoke
tests: they do not check the playground's simulation results in the browser, which
the unit tests and `compare.txt` cover.

## Screenshots

`shots/` holds seven screenshots captured from the UI in headless Chromium:
four desktop views, one per scenario (`1-quiet-morning`, `2-big-restore`,
`3-urgent-preempt`, `4-drive-failure`), one phone-sized view (`5-phone`, 390×844),
one with reduced motion forced (`6-reduced-motion`) and the cleaning lesson just
after the expired cleaner was rejected (`7-lessons`). Six are 1280×720 and one is
390×844. Shots 1–6 predate the "guided lessons" link in the playground's header. They are synthetic UI captures of invented data, not evidence from any
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
5. **UI tests are smoke tests.** Playwright checks that both pages load, the lessons
   can be completed and nothing errors; it does not assert on the playground's
   animation or on-screen numbers.
6. **Accessibility is partial.** Keyboard shortcuts, labels and an `aria-live` event
   log are present. No screen-reader pass and no automated accessibility audit has
   been done. Under `prefers-reduced-motion` the robot is hidden entirely rather than
   replaced by a text readout.
7. **Chromium only.** Screenshots and the Playwright smoke tests use Chromium;
   Firefox and WebKit were not tried.
8. **No real-phone testing.** The phone view is a 390×844 headless capture only.
9. **Costs are illustrative.** Every default in `DEFAULT_COSTS` is invented. None is
   measured or taken from a vendor datasheet or public specification, and the
   simulator makes no claim about any physical tape, drive or library. Seek-time
   differences between policies are differences in this model; they do not establish
   real-world time or money savings.
10. **Not packaged or deployed.** It runs from a static file server you start
    yourself; there is no npm package and no hosted copy. `@playwright/test` is the
    only dependency, dev-only, for the smoke tests; the pages themselves load nothing.
11. **The lessons are scripted, not simulated.** The lesson library is a separate
    state machine from `sim.js`: no request queue runs underneath, nothing happens
    unless you type, and each lesson starts from a fixed hand-written setup. It
    teaches the order of operations and why the library refuses some moves, not
    timing under load.

## License

MIT — see [`LICENSE`](LICENSE).
