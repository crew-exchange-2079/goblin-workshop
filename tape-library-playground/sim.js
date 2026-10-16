/*
 * sim.js — a small, pure simulator of a robotic cartridge tape library.
 *
 * Pure: no DOM, no Node-only globals (no process, no require), no Math.random.
 * Loadable straight into a browser as a module and imported by Node for tests.
 *
 * Everything here is INVENTED. The numbers are "illustrative" — plausible for a
 * mid-size cartridge library, not measured from any real site or product.
 * The cartridge labels, file names and workload shapes are made up.
 *
 * Model in one paragraph: slots in a grid hold cartridges, D drives read them,
 * one robot moves cartridges between slots and drives. A request asks to read
 * one file from one cartridge. A drive must mount (robot fetch + thread) a
 * cartridge before it can read; reading costs a seek proportional to how far
 * the requested file sits from where the head already is, plus a read
 * proportional to the file size; unmounting rewinds the whole tape. A policy
 * decides which queued request gets the next free drive.
 */

export const BLOCKS = 1200; // invented: how many addressable blocks we pretend a tape has
export const TAPE_MB = 1200000; // invented: capacity of one cartridge (1.2 TB), for the docs only

/* ------------------------------------------------------------------ *
 * Tunable costs. Every value is ILLUSTRATIVE: invented and plausible-looking, not measured on any real hardware.
 * Units: seconds, and library "slot units" for robot travel.
 * ------------------------------------------------------------------ */
export const DEFAULT_COSTS = {
  robotSecondsPerSlot: 0.30, // travel time per unit of library distance
  robotHomeRow: 1, // robot parks this many rows below the bottom slot row
  robotPickSeconds: 2.5, // gripper closes on a cartridge
  robotPlaceSeconds: 2.0, // gripper releases a cartridge
  robotEjectSeconds: 4.0, // gripper takes a cartridge out of a drive
  loadSeconds: 40, // thread a tape in a drive (the famous ~30-60s)
  seekBaseSeconds: 1.0, // fixed spin-up cost per seek
  seekSecondsPerTape: 12, // extra cost of a seek across the WHOLE tape, end to end
  readMBps: 30, // sequential read throughput, MB per second
  unloadSeconds: 25, // rewind to beginning of tape before ejecting
  recoverySeconds: 180, // operator time to free a cartridge out of a dead drive
  agingPerMinute: 0.06, // how much a request's urgency rises per minute waited
  agingCap: 0.9, // ...but never enough to outrank a fresh urgent request
  urgentBoost: 1.0, // starting urgency of an urgent request
  arrivalJitter: 0.35, // +/- fraction applied to each inter-arrival gap
};

/* ------------------------------------------------------------------ *
 * Scenarios. Seeds are fixed, so every run is reproducible.
 * ------------------------------------------------------------------ */
export const SCENARIOS = {
  'quiet-morning': {
    id: 'quiet-morning',
    title: 'Quiet morning',
    blurb: 'A small library, light traffic. Everything keeps up — nothing teaches much.',
    seed: 20261013,
    grid: { cols: 10, rows: 2 },
    drives: 3,
    carts: 14,
    filesPerCart: [4, 10],
    requestsPerMinute: 2.0,
    everyNthUrgent: 9,
    burst: null,
    arrivalStopMinutes: 20,
    horizonMinutes: 35,
    driveFailure: null,
  },
  'big-restore': {
    id: 'big-restore',
    title: 'Big restore',
    blurb: '40 tapes, hundreds of small files, only 2 drives. Throughput is the whole game.',
    seed: 20261014,
    grid: { cols: 10, rows: 4 },
    drives: 2,
    carts: 40,
    filesPerCart: [10, 18],
    requestsPerMinute: 8.0, // ~480 small files over an hour = a real restore
    everyNthUrgent: 24,
    burst: null,
    arrivalStopMinutes: 60,
    horizonMinutes: 90,
    driveFailure: null,
  },
  'urgent-starve': {
    id: 'urgent-starve',
    title: 'Urgent request during a big restore',
    blurb: 'A restore floods the queue, then a page of urgent requests lands on top of it.',
    seed: 20261015,
    grid: { cols: 10, rows: 4 },
    drives: 2,
    carts: 36,
    filesPerCart: [10, 18],
    requestsPerMinute: 7.0,
    everyNthUrgent: 30,
    burst: { atMinutes: 20, count: 10, gapMinutes: 1.0, label: 'operator page' },
    arrivalStopMinutes: 45,
    horizonMinutes: 80,
    driveFailure: null,
  },
  'drive-failure': {
    id: 'drive-failure',
    title: 'A drive fails',
    blurb: 'One drive dies mid-run. Its work goes back on the queue and the other drives absorb it.',
    seed: 20261016,
    grid: { cols: 10, rows: 3 },
    drives: 3,
    carts: 28,
    filesPerCart: [8, 16],
    requestsPerMinute: 6.0,
    everyNthUrgent: 15,
    burst: null,
    arrivalStopMinutes: 30,
    horizonMinutes: 60,
    driveFailure: { atMinutes: 12, drive: 1 },
  },
};

export const SCENARIO_IDS = Object.keys(SCENARIOS);

/* ------------------------------------------------------------------ *
 * Policies
 * ------------------------------------------------------------------ */
export const POLICIES = [
  {
    id: 'fifo',
    name: 'FIFO',
    blurb: 'Oldest queued request first. Simple, fair-ish, and blind to what is already threaded.',
    keepMounted: false,
  },
  {
    id: 'batch',
    name: 'Batch by cartridge',
    blurb: 'Once a tape is in a drive, finish everything queued for that tape before unmounting.',
    keepMounted: true,
  },
  {
    id: 'position',
    name: 'Position order',
    blurb: 'On a mounted tape, read in tape order so the head never has to seek backwards.',
    keepMounted: true,
  },
  {
    id: 'priority',
    name: 'Priority + aging',
    blurb: 'Urgent jumps the queue and preempts a mounted tape; with nothing urgent waiting it stays put, so waiting raises a request toward the front.',
    keepMounted: true,
  },
];

export const POLICY_IDS = POLICIES.map((p) => p.id);
export const policyById = (id) => POLICIES.find((p) => p.id === id) || POLICIES[0];

/* ------------------------------------------------------------------ *
 * Deterministic RNG (mulberry32). No Math.random anywhere in this file.
 * ------------------------------------------------------------------ */
export function makeRng(seed) {
  let a = seed >>> 0;
  return function rng() {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const round3 = (x) => Math.round(x * 1000) / 1000;
const cartLabel = (i) => `CRW${String(i + 1).padStart(3, '0')}L9`;
const fileLabel = (cart, n) => `blk_${String(n + 1).padStart(4, '0')}.img`;

/* ------------------------------------------------------------------ *
 * Configuration
 * ------------------------------------------------------------------ */
export function normalizeConfig(config = {}) {
  const scenario = SCENARIOS[config.scenario] || SCENARIOS[config.scenarioId] || null;
  const base = scenario
    ? {
        scenario: scenario.id,
        seed: scenario.seed,
        grid: scenario.grid,
        drives: scenario.drives,
        carts: scenario.carts,
        filesPerCart: scenario.filesPerCart,
        requestsPerMinute: scenario.requestsPerMinute,
        everyNthUrgent: scenario.everyNthUrgent,
        burst: scenario.burst,
        arrivalStopMinutes: scenario.arrivalStopMinutes,
        horizonMinutes: scenario.horizonMinutes,
        driveFailure: scenario.driveFailure,
      }
    : {
        scenario: config.scenario || 'custom',
        seed: 1,
        grid: { cols: 8, rows: 2 },
        drives: 2,
        carts: 12,
        filesPerCart: [4, 10],
        requestsPerMinute: 4,
        everyNthUrgent: 12,
        burst: null,
        arrivalStopMinutes: 20,
        horizonMinutes: 30,
        driveFailure: null,
      };
  const out = { ...base, ...stripUndefined(config), costs: { ...DEFAULT_COSTS, ...(config.costs || {}) } };
  out.policy = policyById(out.policy).id;
  out.slotCount = out.grid.cols * out.grid.rows;
  if (out.carts > out.slotCount) out.carts = out.slotCount;
  return out;
}

function stripUndefined(obj) {
  const o = {};
  for (const k of Object.keys(obj)) if (obj[k] !== undefined && k !== 'costs') o[k] = obj[k];
  return o;
}

/* ------------------------------------------------------------------ *
 * Layout geometry (in "slot units"). Row changes cost more than columns.
 * ------------------------------------------------------------------ */
const ROW_PITCH = 4;
const DRIVE_X_GAP = 4.5;
const DRIVE_Y_OFFSET = 7;

/* ------------------------------------------------------------------ *
 * The simulator
 * ------------------------------------------------------------------ */
export function createSim(config = {}) {
  const cfg = normalizeConfig(config);
  const C = cfg.costs;
  const rng = makeRng(cfg.seed);

  const slotCount = cfg.slotCount;
  const cols = cfg.grid.cols;
  const driveCount = cfg.drives;

  const slotPos = (i) => ({ x: i % cols, y: Math.floor(i / cols) * ROW_PITCH });
  const drivePos = (i) => ({
    x: (cols - 1) / 2 + (i - (driveCount - 1) / 2) * DRIVE_X_GAP,
    y: (cfg.grid.rows - 1) * ROW_PITCH + C.robotHomeRow + DRIVE_Y_OFFSET,
  });
  const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

  /* ---- cartridges ------------------------------------------------- */
  const carts = [];
  for (let i = 0; i < cfg.carts; i++) {
    const lo = cfg.filesPerCart[0];
    const hi = cfg.filesPerCart[1];
    const n = lo + Math.floor(rng() * (hi - lo + 1));
    const files = [];
    const used = new Set();
    for (let f = 0; f < n; f++) {
      let pos;
      let guard = 0;
      do {
        pos = round3(0.02 + rng() * 0.96);
        guard++;
      } while (used.has(pos) && guard < 200);
      used.add(pos);
      files.push({
        name: fileLabel(i, f),
        pos,
        mb: Math.round(5 + rng() * 115), // invented small-file sizes, 5-120 MB
        block: Math.round(pos * BLOCKS),
        pct: Math.round(pos * 100),
      });
    }
    files.sort((a, b) => a.pos - b.pos);
    carts.push({
      id: cartLabel(i),
      index: i,
      label: cartLabel(i),
      homeSlot: i,
      files,
      loc: { kind: 'slot', slot: i },
      mounts: 0,
      reads: 0,
      claimed: null, // drive id that has asked the robot to move this cartridge
    });
  }
  const cartById = new Map(carts.map((c) => [c.id, c]));
  // the whole grid, including empty slots
  const slots = new Array(slotCount).fill(null);
  for (const cart of carts) slots[cart.homeSlot] = cart.id;

  /* ---- drives ----------------------------------------------------- */
  const drives = [];
  for (let i = 0; i < driveCount; i++) {
    drives.push({
      id: i,
      name: `D${i + 1}`,
      state: 'idle', // idle | queued | loading | mounted | seeking | reading | unloading | returning | broken
      cart: null,
      headPos: 0,
      timer: 0,
      timerTotal: 0,
      work: null, // request id in flight
      mounts: 0,
      reads: 0,
      busySeconds: 0,
      seekSeconds: 0,
      readSeconds: 0,
      lastSeek: 0,
      failureAt: null,
      down: false,
      jobQueued: false,
    });
  }

  /* ---- robot ------------------------------------------------------ */
  const home = { x: (cols - 1) / 2, y: (cfg.grid.rows - 1) * ROW_PITCH + C.robotHomeRow };
  const robot = {
    x: home.x,
    y: home.y,
    state: 'idle', // idle | moving | picking | placing | ejecting | parking
    job: null,
    jobs: [],
    from: { x: home.x, y: home.y },
    to: { x: home.x, y: home.y },
    elapsed: 0,
    duration: 0,
    busySeconds: 0,
    moves: 0,
    label: '',
  };

  /* ---- requests & queues ------------------------------------------ */
  let requestSeq = 0;
  const pending = []; // unassigned, in arrival order
  const inFlight = new Map(); // requestId -> request
  const completed = [];
  const completedIds = new Set();
  const allRequests = [];
  const events = [];
  let arrivalCredit = 0;
  let emitted = 0;
  let burstIdx = 0;
  let arrivalOpen = true;
  let driveFailed = false;
  let failurePending = null; // {at, drive}

  const burstQueue = cfg.burst
    ? Array.from({ length: cfg.burst.count }, (_, i) => ({
        at: (cfg.burst.atMinutes + i * cfg.burst.gapMinutes) * 60,
        priority: 'urgent',
        label: cfg.burst.label,
      }))
    : [];

  if (cfg.driveFailure) {
    failurePending = { at: cfg.driveFailure.atMinutes * 60, drive: cfg.driveFailure.drive };
  }

  const horizonSeconds = cfg.horizonMinutes * 60;
  const arrivalStopSeconds = cfg.arrivalStopMinutes * 60;

  /* ---- helpers ----------------------------------------------------- */
  function logEvent(kind, text, extra) {
    events.push({ t: round1(time), kind, text, ...(extra || {}) });
    if (events.length > 400) events.splice(0, events.length - 400);
  }

  const round1 = (x) => Math.round(x * 10) / 10;
  const mmss = (sec) => {
    const s = Math.max(0, Math.round(sec));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  };

  function makeRequest(priority, label) {
    // pick a file uniformly over all files on all cartridges
    const cart = carts[Math.floor(rng() * carts.length)];
    const file = cart.files[Math.floor(rng() * cart.files.length)];
    emitted++;
    const req = {
      id: emitted,
      cart: cart.id,
      cartLabel: cart.label,
      file: file.name,
      block: file.block,
      pct: file.pct,
      pos: file.pos,
      mb: file.mb,
      priority,
      label: label || (priority === 'urgent' ? 'urgent' : 'restore'),
      arrival: time,
      serviceStart: null,
      doneAt: null,
      wait: null,
      response: null,
      requeues: 0,
      lostDrives: [],
    };
    allRequests.push(req);
    pending.push(req);
    return req;
  }

  function generateArrivals(dt) {
    if (!arrivalOpen) return;
    const gap = 60 / Math.max(0.0001, cfg.requestsPerMinute);
    arrivalCredit += dt / gap;
    while (arrivalCredit >= 1) {
      arrivalCredit -= 1;
      const jitter = 1 + (rng() - 0.5) * 2 * C.arrivalJitter;
      arrivalCredit += jitter - 1; // carry jitter into the next gap
      const urgent = cfg.everyNthUrgent > 0 && emitted % cfg.everyNthUrgent === 0;
      makeRequest(urgent ? 'urgent' : 'normal');
    }
    while (burstIdx < burstQueue.length && burstQueue[burstIdx].at <= time) {
      const b = burstQueue[burstIdx++];
      makeRequest(b.priority, b.label);
      logEvent('request', `${b.label} request arrives`, { priority: 'urgent' });
    }
    if (time >= arrivalStopSeconds) arrivalOpen = false;
  }

  /* ---- robot ------------------------------------------------------- */
  function robotCanMove() {
    return robot.jobs.length === 0;
  }

  function enqueueJob(job) {
    robot.jobs.push(job);
  }

  function startNextRobotJob() {
    if (robot.job || robot.jobs.length === 0) return;
    const job = robot.jobs.shift();
    robot.job = job;
    robot.elapsed = 0;
    if (job.type === 'load' || job.type === 'unload') {
      const target = slotPos(job.slot);
      robot.from = { x: robot.x, y: robot.y };
      robot.to = target;
      robot.duration = Math.max(0.6, dist(robot.from, robot.to) * C.robotSecondsPerSlot);
      robot.state = 'moving';
      robot.label = job.cart;
      job.phase = 'toCartridge';
    } else {
      robot.duration = 0;
      robot.state = 'idle';
      robot.job = null;
    }
  }

  function stepRobot(dt) {
    if (!robot.job) {
      startNextRobotJob();
      return;
    }
    robot.elapsed += dt;
    robot.busySeconds += dt;
    const job = robot.job;
    if (robot.state === 'moving') {
      const t = Math.min(1, robot.elapsed / Math.max(1e-6, robot.duration));
      robot.x = robot.from.x + (robot.to.x - robot.from.x) * t;
      robot.y = robot.from.y + (robot.to.y - robot.from.y) * t;
      if (t >= 1) {
        robot.moves++;
        if (job.phase === 'toCartridge') {
          robot.state = job.type === 'load' ? 'picking' : 'ejecting';
          robot.duration = job.type === 'load' ? C.robotPickSeconds : C.robotEjectSeconds;
          robot.elapsed = 0;
        } else {
          robot.state = 'placing';
          robot.duration = C.robotPlaceSeconds;
          robot.elapsed = 0;
        }
      }
      return;
    }
    if (robot.elapsed >= robot.duration) {
      const drive = drives[job.drive];
      if (job.type === 'load') {
        if (robot.state === 'picking') {
          const cart = cartById.get(job.cart);
          if (cart.loc.kind !== 'slot' || slots[cart.loc.slot] !== cart.id) {
            // something moved this tape while the robot was on its way: put it
            // back and forget the trip rather than teleporting it into a drive
            abortJob(job);
            return;
          }
          const slot = cart.loc.slot;
          slots[slot] = null;
          cart.loc = { kind: 'robot' };
          robot.state = 'moving';
          robot.from = { x: robot.x, y: robot.y };
          robot.to = drivePos(drive.id);
          robot.duration = Math.max(0.6, dist(robot.from, robot.to) * C.robotSecondsPerSlot);
          robot.elapsed = 0;
          job.phase = 'toDrive';
          return;
        }
        // placed into drive
        drive.state = 'loading';
        drive.cart = job.cart;
        drive.timer = C.loadSeconds;
        drive.timerTotal = C.loadSeconds;
        drive.headPos = 0;
        cartById.get(job.cart).loc = { kind: 'drive', drive: drive.id };
        cartById.get(job.cart).claimed = null;
        cartById.get(job.cart).mounts++;
        drive.mounts++;
        logEvent('mount', `${drive.name} threaded ${job.cart}`, { drive: drive.id, cart: job.cart });
        drive.jobQueued = false;
        robot.job = null;
        robot.state = 'idle';
        robot.label = '';
        startNextRobotJob();
        return;
      }
      // unload
      if (robot.state === 'ejecting') {
        const cart = cartById.get(job.cart);
        cart.loc = { kind: 'robot' };
        cart.claimed = drive.id;
        robot.state = 'moving';
        robot.from = { x: robot.x, y: robot.y };
        robot.to = slotPos(job.slot);
        robot.duration = Math.max(0.6, dist(robot.from, robot.to) * C.robotSecondsPerSlot);
        robot.elapsed = 0;
        job.phase = 'toSlot';
        return;
      }
      // placed back in a slot
      const cart = cartById.get(job.cart);
      slots[job.slot] = job.cart;
      cart.loc = { kind: 'slot', slot: job.slot };
      cart.claimed = null;
      // if we already queued a request for the next tape, go straight to fetching it
      drive.state = drive.work !== null ? 'queued' : 'idle';
      drive.cart = null;
      logEvent('unmount', `${drive.name} ejected ${job.cart} to slot ${job.slot}`, {
        drive: drive.id,
        cart: job.cart,
      });
      drive.jobQueued = false;
      robot.job = null;
      robot.state = 'idle';
      robot.label = '';
      startNextRobotJob();
    }
  }

  function freeSlotNear(slot) {
    if (slot != null && slots[slot] === null) return slot;
    for (let d = 0; d < slotCount; d++) {
      if (slot != null && slots[(slot + d) % slotCount] === null) return (slot + d) % slotCount;
      if (slot != null && slots[(slot - d + slotCount) % slotCount] === null) return (slot - d + slotCount) % slotCount;
    }
    return null;
  }

  /* Put a cartridge the robot is holding (or about to hold) back where it came
     from. Used when a job is cancelled — a cartridge must never evaporate. */
  function stashCartridge(cart) {
    const slot = freeSlotNear(cart.homeSlot);
    if (slot == null) {
      cart.loc = { kind: 'stuck', drive: -1, until: time + 30 };
      cart.claimed = null;
      return false;
    }
    slots[slot] = cart.id;
    cart.loc = { kind: 'slot', slot };
    cart.claimed = null;
    return true;
  }

  function abortJob(job) {
    const cart = cartById.get(job.cart);
    if (cart && (cart.loc.kind === 'robot' || cart.claimed === job.drive)) stashCartridge(cart);
    if (job.drive >= 0 && drives[job.drive]) drives[job.drive].jobQueued = false;
    if (robot.job === job) {
      robot.job = null;
      robot.state = 'idle';
      robot.label = '';
      startNextRobotJob();
    }
  }

  /* ---- request selection ------------------------------------------- */
  function tapeAvailable(req, drive) {
    const cart = cartById.get(req.cart);
    if (!cart) return false;
    if (cart.loc.kind === 'slot') return true;
    if (cart.loc.kind === 'drive' && drive && cart.loc.drive === drive.id && drive.cart === req.cart) return true;
    return false;
  }

  function pickFifo(list, drive) {
    let best = null;
    for (const r of list) {
      if (!tapeAvailable(r, drive)) continue;
      if (!best || r.arrival < best.arrival || (r.arrival === best.arrival && r.id < best.id)) best = r;
    }
    return best;
  }

  function urgency(req) {
    // urgent work outranks normal work; waiting raises both, but aging is capped
    // so a patient normal request can never overtake a fresh urgent one.
    const waited = Math.max(0, time - req.arrival) / 60;
    const base = req.priority === 'urgent' ? 2 * C.urgentBoost : C.urgentBoost;
    return base + Math.min(C.agingCap, waited * C.agingPerMinute);
  }

  function pickPriority(list, drive) {
    let best = null;
    let bestScore = -Infinity;
    for (const r of list) {
      if (!tapeAvailable(r, drive)) continue;
      const s = urgency(r);
      if (s > bestScore || (s === bestScore && best && r.arrival < best.arrival) || (s === bestScore && !best)) {
        best = r;
        bestScore = s;
      }
    }
    return best;
  }

  function pickPosition(list, drive, headPos) {
    // Among requests whose tape is already threaded, read in tape order from
    // where the head is. Otherwise fall back to FIFO for choosing a new tape.
    let best = null;
    for (const r of list) {
      if (drive.cart !== r.cart) continue;
      if (!best) best = r;
      else if (headPos != null && r.pos >= headPos - 1e-9) {
        if (best.pos < headPos - 1e-9 || r.pos < best.pos) best = r;
      } else if (r.pos < best.pos) best = r;
    }
    return best || pickFifo(list.filter((r) => tapeAvailable(r, drive)), drive);
  }

  function pickForDrive(drive) {
    const policy = cfg.policy;
    const hasTape = drive.state === 'mounted' && drive.cart;
    const avail = (r) => tapeAvailable(r, drive);
    if (policy === 'priority') {
      // Urgent work preempts, even mid-tape. With no urgent work queued we
      // prefer what is already threaded, so the policy does not thrash mounts.
      const urgent = pending.filter((r) => r.priority === 'urgent' && (avail(r) || (hasTape && r.cart === drive.cart)));
      if (urgent.length) return pickPriority(urgent, drive);
      if (hasTape) {
        const mine = pending.filter((r) => r.cart === drive.cart && avail(r));
        if (mine.length) return pickPriority(mine, drive);
      }
      return pickPriority(pending, drive);
    }
    if (hasTape) {
      const mine = pending.filter((r) => r.cart === drive.cart);
      if (policy === 'batch') {
        const p = pickFifo(mine, drive);
        if (p) return p;
      } else if (policy === 'position') {
        const p = pickPosition(mine, drive, drive.headPos);
        if (p) return p;
      }
    }
    return pickFifo(pending, drive);
  }

  /* ---- dispatch ---------------------------------------------------- */
  function assign(drive, req) {
    const idx = pending.indexOf(req);
    if (idx >= 0) pending.splice(idx, 1);
    if (req.serviceStart === null) req.serviceStart = time;
    drive.work = req.id;
    inFlight.set(req.id, req);
    return req;
  }

  function shouldKeepMounted(drive) {
    // batch and position never drop a tape that still has queued work; fifo and
    // priority will swap tapes the moment something else looks better.
    if (cfg.policy !== 'batch' && cfg.policy !== 'position') return false;
    return pending.some((r) => r.cart === drive.cart);
  }

  function dispatch() {
    for (const drive of drives) {
      if (drive.down || drive.work !== null) continue;
      if (drive.state !== 'idle' && drive.state !== 'mounted') continue;
      const req = pickForDrive(drive);
      if (!req) {
        // nothing worth reading: give the slot back so the library can breathe
        if (drive.state === 'mounted' && drive.cart && !shouldKeepMounted(drive)) startUnload(drive);
        continue;
      }
      if (drive.state === 'mounted' && req.cart !== drive.cart) {
        assign(drive, req);
        startUnload(drive);
        continue;
      }
      assign(drive, req);
      if (drive.state === 'mounted') {
        drive.timer = 0;
        beginRead(drive, req);
      } else {
        drive.state = 'queued';
        queueMount(drive, req);
      }
    }
  }

  function queueMount(drive, req) {
    if (!req || drive.jobQueued) return;
    const cart = cartById.get(req.cart);
    // the tape is in the gripper, already on its way, or not where we think: wait
    if (!cart.loc || cart.loc.kind !== 'slot') return;
    // one cartridge, one trip: a second drive must not queue the same tape
    if (cart.claimed !== null && cart.claimed !== drive.id) return;
    const slot = cart.loc.slot;
    if (slots[slot] !== cart.id) return;
    cart.claimed = drive.id;
    enqueueJob({ type: 'load', cart: cart.id, slot, drive: drive.id });
    drive.jobQueued = true;
    if (robotCanMove()) startNextRobotJob();
  }

  function startUnload(drive) {
    drive.state = 'unloading';
    drive.timer = C.unloadSeconds;
    drive.timerTotal = C.unloadSeconds;
  }

  /* ---- drive behaviour --------------------------------------------- */
  function beginRead(drive, req) {
    const distFromHead = Math.abs(req.pos - drive.headPos);
    const seek = C.seekBaseSeconds + C.seekSecondsPerTape * distFromHead;
    drive.state = 'seeking';
    drive.timer = seek;
    drive.timerTotal = seek;
    drive.seekSeconds += seek;
    drive.lastSeek = seek;
    drive.pendingSeekTo = req.pos;
  }

  function finishRead(drive, req) {
    drive.headPos = req.pos;
    cartById.get(req.cart).reads++;
    drive.reads++;
    req.doneAt = time;
    req.wait = req.serviceStart - req.arrival;
    req.response = req.doneAt - req.arrival;
    completed.push(req);
    if (completedIds.has(req.id)) {
      // conservation guard: a request may never complete twice
      logEvent('bug', `request ${req.id} completed twice`, { request: req.id });
    }
    completedIds.add(req.id);
    inFlight.delete(req.id);
    drive.work = null;
    logEvent('read', `${drive.name} read ${req.file} from ${req.cart} (${req.pct}% of tape)`, {
      drive: drive.id,
      cart: req.cart,
      request: req.id,
      priority: req.priority,
      wait: round1(req.wait),
    });
  }

  function stepDrive(drive, dt) {
    if (drive.down) return;
    switch (drive.state) {
      case 'queued':
        drive.busySeconds += dt;
        queueMount(drive, inFlight.get(drive.work));
        break;
      case 'loading':
      case 'unloading':
        drive.timer -= dt;
        drive.busySeconds += dt;
        if (drive.timer <= 0) {
          if (drive.state === 'loading') {
            drive.state = 'mounted';
            const req = inFlight.get(drive.work);
            if (req) beginRead(drive, req);
            else drive.state = 'mounted';
          } else {
            // robot ejects it back to a slot
            const cart = cartById.get(drive.cart);
            const slot = freeSlotNear(cart.homeSlot);
            if (slot == null) {
              // nowhere to put it: keep the tape in the drive
              drive.state = 'mounted';
              break;
            }
            enqueueJob({ type: 'unload', cart: drive.cart, slot, drive: drive.id });
            drive.state = 'returning';
            if (robotCanMove()) startNextRobotJob();
          }
        }
        break;
      case 'seeking':
        drive.timer -= dt;
        drive.busySeconds += dt;
        if (drive.timer <= 0) {
          const req = inFlight.get(drive.work);
          if (!req) {
            drive.state = 'mounted';
            break;
          }
          const read = req.mb / C.readMBps;
          drive.state = 'reading';
          drive.timer = read;
          drive.timerTotal = read;
          drive.readSeconds += read;
        }
        break;
      case 'reading':
        drive.timer -= dt;
        drive.busySeconds += dt;
        if (drive.timer <= 0) {
          const req = inFlight.get(drive.work);
          if (req) finishRead(drive, req);
          drive.state = 'mounted';
        }
        break;
      case 'mounted':
        if (drive.timer > 0) drive.timer -= dt;
        break;
      default:
        break;
    }
  }

  /* ---- drive failure ------------------------------------------------ */
  function failDrive(index) {
    if (index < 0 || index >= drives.length) return;
    const drive = drives[index];
    if (drive.down) return;
    drive.down = true;
    drive.state = 'broken';
    drive.failureAt = time;
    drive.timer = 0;
    const req = drive.work !== null ? inFlight.get(drive.work) : null;
    if (req) {
      // in-flight work goes back on the queue, wait clock keeps running
      inFlight.delete(req.id);
      drive.work = null;
      req.serviceStart = null;
      req.requeues++;
      req.lostDrives.push(drive.id);
      pending.push(req);
      logEvent('failure', `${drive.name} failed — ${req.file} re-queued`, {
        drive: drive.id,
        request: req.id,
        cart: req.cart,
      });
    }
    if (drive.cart) {
      cartById.get(drive.cart).loc = { kind: 'stuck', drive: drive.id, until: time + C.recoverySeconds };
      cartById.get(drive.cart).claimed = null;
    }
    // drop any robot job aimed at this drive, without losing a cartridge
    const doomed = robot.jobs.filter((j) => j.drive === drive.id);
    robot.jobs = robot.jobs.filter((j) => j.drive !== drive.id);
    if (robot.job && robot.job.drive === drive.id) {
      abortJob(robot.job);
    }
    for (const j of doomed) abortJobQueued(j);
    drive.jobQueued = false;
    // any other drive that had this cartridge claimed can have it back
    for (const cart of carts) if (cart.claimed === drive.id) cart.claimed = null;
    startNextRobotJob();
    logEvent('failure', `${drive.name} is offline`, { drive: drive.id });
  }

  function abortJobQueued(job) {
    const cart = cartById.get(job.cart);
    if (!cart) return;
    if (cart.claimed === job.drive) cart.claimed = null;
    if (cart.loc.kind === 'robot') stashCartridge(cart);
  }

  function recoverStrandedTapes() {
    for (const cart of carts) {
      if (cart.loc.kind === 'stuck' && time >= cart.loc.until) {
        const slot = freeSlotNear(cart.homeSlot);
        if (slot == null) {
          cart.loc.until = time + 30;
          continue;
        }
        slots[slot] = cart.id;
        cart.loc = { kind: 'slot', slot };
        logEvent('recovery', `operator freed ${cart.id} into slot ${slot}`, { cart: cart.id });
      }
    }
  }

  /* ---- stepping ----------------------------------------------------- */
  let time = 0;

  function step(dt) {
    if (!(dt > 0)) return;
    time += dt;
    if (failurePending && !driveFailed && time >= failurePending.at) {
      driveFailed = true;
      failDrive(failurePending.drive);
    }
    generateArrivals(dt);
    stepRobot(dt);
    for (const drive of drives) stepDrive(drive, dt);
    recoverStrandedTapes();
    dispatch();
  }

  function settled() {
    if (pending.length > 0 || inFlight.size > 0) return false;
    for (const d of drives) if (d.work !== null || (d.state !== 'idle' && d.state !== 'broken')) return false;
    return robot.job === null && robot.jobs.length === 0;
  }

  /* ---- stats -------------------------------------------------------- */
  function percentile(values, p) {
    if (values.length === 0) return null;
    const s = values.slice().sort((a, b) => a - b);
    const idx = Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1));
    return s[idx];
  }
  const mean = (vs) => (vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null);

  function stats() {
    const waits = completed.map((r) => r.wait);
    const responses = completed.map((r) => r.response);
    const urgentWaits = completed.filter((r) => r.priority === 'urgent').map((r) => r.wait);
    const normalWaits = completed.filter((r) => r.priority === 'normal').map((r) => r.wait);
    const simMinutes = Math.max(1e-6, time / 60);
    const mounts = drives.reduce((a, d) => a + d.mounts, 0);
    const passes = carts.reduce((a, c) => a + c.mounts, 0);
    return {
      time,
      policy: cfg.policy,
      scenario: cfg.scenario,
      seed: cfg.seed,
      generated: allRequests.length,
      completed: completed.length,
      backlog: pending.length + inFlight.size,
      duplicateCompletions: completed.length - completedIds.size,
      meanWait: mean(waits),
      p95Wait: percentile(waits, 95),
      maxWait: waits.length ? Math.max(...waits) : null,
      meanResponse: mean(responses),
      urgentMeanWait: mean(urgentWaits),
      urgentP95Wait: percentile(urgentWaits, 95),
      normalMeanWait: mean(normalWaits),
      urgentDone: urgentWaits.length,
      normalDone: normalWaits.length,
      mounts,
      tapePasses: passes,
      mountsPerHour: mounts / simMinutes * 60,
      seekSeconds: drives.reduce((a, d) => a + d.seekSeconds, 0),
      readSeconds: drives.reduce((a, d) => a + d.readSeconds, 0),
      meanSeekSeconds: (() => {
        const n = completed.length;
        const s = drives.reduce((a, d) => a + d.seekSeconds, 0);
        return n ? s / n : null;
      })(),
      filesPerHour: completed.length / simMinutes * 60,
      robotBusyFraction: robot.busySeconds / time,
      drivesDown: drives.filter((d) => d.down).length,
      requeues: allRequests.reduce((a, r) => a + r.requeues, 0),
      settled: settled(),
      driveStates: drives.map((d) => d.name + ':' + d.state),
    };
  }

  /* ---- view for the UI --------------------------------------------- */
  function view() {
    return {
      time,
      mmss,
      policy: cfg.policy,
      policyName: policyById(cfg.policy).name,
      scenario: cfg.scenario,
      grid: cfg.grid,
      home,
      slotCount,
      slots,
      carts: carts.map((c) => ({
        id: c.id,
        label: c.label,
        files: c.files.length,
        mounts: c.mounts,
        reads: c.reads,
        loc: c.loc,
      })),
      robot: {
        x: robot.x,
        y: robot.y,
        state: robot.state,
        label: robot.label,
        progress: robot.duration > 0 ? Math.min(1, robot.elapsed / robot.duration) : 1,
        busyFraction: time > 0 ? robot.busySeconds / time : 0,
      },
      drives: drives.map((d) => ({
        id: d.id,
        name: d.name,
        state: d.state,
        cart: d.cart,
        headPos: d.headPos,
        progress:
          d.timer > 0 && d.timerTotal > 0
            ? 1 - d.timer / d.timerTotal
            : ['loading', 'unloading', 'seeking', 'reading'].includes(d.state)
              ? 1
              : 0,
        timer: d.timer,
        work: d.work,
        down: d.down,
        mounts: d.mounts,
        reads: d.reads,
      })),
      pending: pending.slice(0, 14),
      pendingCount: pending.length,
      inFlightCount: inFlight.size,
      stats: stats(),
      events: events.slice(-40),
    };
  }

  return {
    config: cfg,
    step,
    settled,
    stats,
    view,
    get time() {
      return time;
    },
    get pending() {
      return pending;
    },
    get inFlight() {
      return inFlight;
    },
    get completed() {
      return completed;
    },
    get allRequests() {
      return allRequests;
    },
    get carts() {
      return carts;
    },
    get drives() {
      return drives;
    },
    get events() {
      return events;
    },
    get robot() {
      return robot;
    },
    failDrive,
  };
}

/* ------------------------------------------------------------------ *
 * Headless runs — used by compare.mjs and by the tests.
 * ------------------------------------------------------------------ */
export function runHeadless(config = {}) {
  const cfg = normalizeConfig(config);
  const sim = createSim(cfg);
  const dt = config.dt || 0.5;
  const maxSeconds = config.maxSeconds || cfg.horizonMinutes * 60;
  let steps = 0;
  while (sim.time < maxSeconds && steps < 2_000_000) {
    sim.step(dt);
    steps++;
    if (config.stopWhenSettled !== false && sim.time > (cfg.arrivalStopMinutes * 60 + 60) && sim.settled()) break;
  }
  return { sim, stats: sim.stats(), steps };
}

export function comparePolicies(scenarioId, opts = {}) {
  const cfg = normalizeConfig({ ...opts, scenario: scenarioId });
  return POLICIES.map((p) => {
    const run = runHeadless({ ...cfg, ...opts, policy: p.id });
    return { policy: p.id, name: p.name, ...run.stats };
  });
}

export function fmt(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return Number(v).toFixed(digits);
}

export function secondsToClock(sec) {
  if (sec === null || sec === undefined) return '—';
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
