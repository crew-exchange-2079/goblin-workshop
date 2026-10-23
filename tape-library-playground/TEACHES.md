# Five things this toy is meant to illustrate

Everything here is invented. The library, the cartridges (`CRW001L9`), the files
(`blk_0042.img`), the workload and every cost are illustrative — a teaching
model, not a measurement of any real site or product. The numbers quoted below
are what `node compare.mjs` prints for this simulator and these four scenarios
(see `compare.txt`); they describe the simulator, not real tape systems, and
other workloads or cost settings could rank the policies differently.

---

## 1. The slow parts are physical, so "just add a drive" is not a plan

Reading one small file off one cartridge is not a disk read. Before a single byte
moves, the robot has to travel to the slot (a few seconds), grip the cartridge,
carry it to the drive, and the drive has to *thread* the tape — modelled here at
`loadSeconds: 40`, illustrative for a 30–60 s load. Then the head seeks, which
costs `seekBaseSeconds + seekSecondsPerTape × distance along the tape` — about a
second for a file nearby, up to ~13 s for one at the far end. Then it reads at
`readMBps: 30`, so a 5 MB file is a sixth of a second and a 120 MB file is four.
And when the tape is done, the drive rewinds the whole thing (`unloadSeconds:
25`) before anyone can touch it again. Look at the "Quiet morning" table: three
drives, light traffic, 41 files, and still **55–57% of the run is the robot
moving cartridges**. The robot, not the data, is the bottleneck you are most
likely to forget about.

## 2. FIFO does worst of the four policies here — the queue order fights the physics

In the three congested scenarios (Big restore, Urgent request, A drive fails) FIFO
is the worst of the four tested policies on files served and on waits. In Quiet
morning there is no throughput gap to speak of: all four policies finish 41/41, and
FIFO's reported rate is even marginally higher (111 vs 110 files/h). What it loses
there is waits (mean 27 s vs 18 s, p95 81 s vs 44 s) and seek time per read (6.13 s
vs 5.65 s). That is a result for these policies and scenarios, not a claim about
every possible workload. Take the Big restore scenario: 40 cartridges, 2 drives, ~480 small files coming
in over an hour. FIFO served **110 of 476 and left 366 files queued**, at 73
files/hour. Batch by cartridge, same seed, same library, same costs, served
**452** at 301 files/hour. FIFO is not slower because it is *stupid* about
requests; it is slower because it is blind to the fact that the tape is already
sitting in the drive. It reads one file, then walks the whole library again for
the next one, paying 40 s of threading, a rewind and two robot trips for every
single file — 110 mounts for 110 files, one file per mount. That is the lesson
underneath the toy: **on tape, the unit of work is the cartridge, not the file.**
Scheduling has to think about what is already threaded.

## 3. Serving in tape order is cheaper per mount — locality is free performance

Once you stop throwing tapes away, the next question is what order to read the
files in. Batch by cartridge reads a tape's queue in arrival order, so the head
ping-pongs — a real capture from one mount under Batch, as percentages along the
tape: `86 → 37 → 16 → 2 → 63 → 86`, four backward seeks out of five. Position
order serves the same mounted tape in ascending tape order, so the head only
moves forward and wraps at most once per mount: the same cartridge reads
`86 → 86 → 2 → 16 → 37 → 63`, one wrap. The comparison table shows the
difference in this simulator: **seek time per file read drops from 4.95 s to 4.02 s**, and the Big
restore finishes more files (464 vs 452) with a better tail (p95 2272 s vs
2440 s) even though it mounts *more* tapes (87 vs 82) — it wastes less motion
per mount, and in the urgent scenario it is the only policy that clears the
entire queue (327/327). The general rule: **on a sequential medium, locality is
cheap performance.** These are simulated seconds under illustrative costs; they
do not show what the saving would be, in time or money, on a real library.

## 4. Urgent work waits a long time under FIFO — and priority without aging risks starving the rest

An urgent request is only urgent relative to what is already queued. In the
"Urgent request during a big restore" scenario a page of ten urgent requests
lands at minute 20 on top of a queue that is already deep. Under FIFO the
urgent requests wait for everything ahead of them: **urgent p95 wait 3395 s
(56 minutes)** — an hour for a "get me this now" ticket. Under Priority + aging
the same ten requests get **p95 82 s**, because urgent work preempts the mounted
tape. But priority alone is not the answer either: if urgent work always wins,
then *normal* work can starve. (That is starvation, not necessarily priority
inversion, which is a different failure where a low-priority holder blocks
a high-priority waiter.) This toy has no selectable policy that
demonstrates that starvation; its priority policy is
**priority with aging**: every minute of waiting raises a request's score, which
pushes long-waiting normal work up toward the front but is capped so it can never
overtake a fresh urgent request. That cap is not a general guarantee: dispatch still
picks eligible urgent work first, so under sustained urgent arrivals normal work
could still wait indefinitely. The scenarios here use a finite urgent burst, and
the tests do not establish fairness in general. And because preempting costs a rewind and a
remount, the policy prefers whatever is already threaded when nothing urgent is
waiting — which is why it still serves 321 of 327 files, against FIFO's 101.
The lesson: **priority is better with an aging term, and preemption is
not free.**

## 5. A drive dying is a re-queue problem, and the tape inside it is stranded too

In the "A drive fails" scenario drive 2 fails at minute 12, mid-restore. Two
things happen that a queue-discipline change cannot fix on its own. First, the
request it was reading goes back on the queue with its wait clock *not* reset —
it arrived at, say, minute 9, so its wait keeps growing from there. The test
`drive failure: work in flight is re-queued, not lost` asserts exactly this: every
request in flight at the moment of failure is re-queued and later finishes
exactly once, on a different drive. Second, the cartridge inside the dead drive is
*stranded* — it cannot be scheduled anywhere until an operator frees it
(`recoverySeconds: 180`, illustrative). Requests for that tape simply stop being
eligible until then, which is why a drive failure costs more than the loss of one of three drives'
read capacity until the operator recovers the tape. Under Batch, Position and
Priority + aging the surviving two drives still clear the entire backlog (177/177);
under FIFO, 95 files are still queued at the end. The lesson: **capacity failures are
also scheduling problems, and the recovery path matters as much as the policy.**

---

These five are about scheduling. The operator's side — mounting by hand, cleaning,
the I/E slots, reading TapeAlert flags, recovering from a robot jam — is covered by
the five guided lessons in `lessons.html` (see the README's "Guided lessons").
