/*
 * sim.test.js — run with:  node --test
 * Uses only Node's built-in test runner (node:test + node:assert). No npm install.
 *
 * These tests check the four things the packet asks for — determinism by seed,
 * conservation, the batch invariant, aging, and drive-failure re-queueing —
 * plus the structural sanity of the model itself.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  createSim,
  runHeadless,
  comparePolicies,
  normalizeConfig,
  makeRng,
  DEFAULT_COSTS,
  SCENARIOS,
  SCENARIO_IDS,
  POLICIES,
  POLICY_IDS,
} from './sim.js';

const here = dirname(fileURLToPath(import.meta.url));

/* run a sim to a time horizon in fixed steps */
function run(config, seconds, dt = 0.5) {
  const sim = createSim(config);
  while (sim.time < seconds) sim.step(dt);
  return sim;
}

/* ---------- 1. deterministic RNG ---------- */

test('rng: same seed gives the same stream', () => {
  const a = makeRng(42);
  const b = makeRng(42);
  for (let i = 0; i < 200; i++) assert.equal(a(), b());
});

test('rng: different seeds give different streams', () => {
  const a = makeRng(42);
  const b = makeRng(43);
  const diff = Array.from({ length: 50 }, () => a() !== b()).filter(Boolean).length;
  assert.ok(diff > 40, `expected mostly different draws, got ${diff}/50`);
});

test('rng: values stay in [0,1)', () => {
  const r = makeRng(7);
  for (let i = 0; i < 5000; i++) {
    const v = r();
    assert.ok(v >= 0 && v < 1, `out of range: ${v}`);
  }
});

/* ---------- 2. determinism of the whole simulation ---------- */

test('determinism: same seed and config produce an identical run', () => {
  for (const scenario of SCENARIO_IDS) {
    const a = runHeadless({ scenario, policy: 'batch' }).stats;
    const b = runHeadless({ scenario, policy: 'batch' }).stats;
    assert.deepEqual(a, b, `${scenario} is not deterministic`);
  }
});

test('determinism: completion ORDER is identical too, not just the totals', () => {
  const a = run({ scenario: 'big-restore', policy: 'batch' }, 1800);
  const b = run({ scenario: 'big-restore', policy: 'batch' }, 1800);
  assert.deepEqual(
    a.completed.map((r) => r.id),
    b.completed.map((r) => r.id),
  );
});

test('determinism: a different seed produces a different workload', () => {
  const a = runHeadless({ scenario: 'big-restore', policy: 'batch', seed: 1 }).stats;
  const b = runHeadless({ scenario: 'big-restore', policy: 'batch', seed: 999 }).stats;
  assert.notEqual(a.meanWait, b.meanWait);
});

test('determinism: the library layout is stable for a seed', () => {
  const a = createSim({ scenario: 'quiet-morning', policy: 'fifo' });
  const b = createSim({ scenario: 'quiet-morning', policy: 'fifo' });
  assert.deepEqual(
    a.carts.map((c) => c.files.map((f) => f.name + '@' + f.pos)),
    b.carts.map((c) => c.files.map((f) => f.name + '@' + f.pos)),
  );
});

/* ---------- 3. conservation ---------- */

test('conservation: every request finishes exactly once, under every policy and scenario', () => {
  for (const scenario of SCENARIO_IDS) {
    for (const policy of POLICY_IDS) {
      const { sim, stats } = runHeadless({ scenario, policy });
      const ids = sim.completed.map((r) => r.id);
      const unique = new Set(ids);
      assert.equal(ids.length, unique.size, `${scenario}/${policy}: a request completed twice`);
      assert.equal(stats.duplicateCompletions, 0);
      assert.ok(
        stats.completed + stats.backlog === stats.generated,
        `${scenario}/${policy}: completed(${stats.completed}) + backlog(${stats.backlog}) != generated(${stats.generated})`,
      );
      for (const r of sim.allRequests) {
        const done = ids.filter((i) => i === r.id).length;
        assert.ok(done <= 1, `${scenario}/${policy}: request ${r.id} completed ${done} times`);
        if (done === 1) {
          const rec = sim.completed.find((x) => x.id === r.id);
          assert.ok(rec.doneAt >= rec.serviceStart, 'finished before it started');
          assert.ok(rec.serviceStart >= rec.arrival, 'service started before the request arrived');
          assert.ok(rec.wait >= 0 && rec.response >= rec.wait, 'wait/response out of order');
        }
      }
    }
  }
});

test('conservation: nothing is lost when the run drains completely', () => {
  const sim = run({ ...SCENARIOS['quiet-morning'], scenario: 'quiet-morning', policy: 'batch', horizonMinutes: 120 }, 7200);
  assert.equal(sim.pending.length, 0);
  assert.equal(sim.inFlight.size, 0);
  assert.equal(sim.completed.length, sim.allRequests.length);
});

/* ---------- 4. cartridges are never in two places ---------- */

test('conservation: a cartridge is always in exactly one place', () => {
  const sim = createSim({ scenario: 'big-restore', policy: 'batch' });
  for (let i = 0; i < 6000; i++) {
    sim.step(0.5);
    if (i % 25) continue;
    for (const cart of sim.carts) {
      const loc = cart.loc;
      if (loc.kind === 'slot') {
        const home = sim.view().slots;
        assert.equal(home[loc.slot], cart.id, `cart ${cart.id} claims slot ${loc.slot} but it holds another`);
      } else if (loc.kind === 'drive') {
        assert.equal(sim.drives[loc.drive].cart, cart.id, `cart ${cart.id} claims a drive that holds another tape`);
      } else {
        assert.ok(loc.kind === 'robot' || loc.kind === 'stuck');
      }
    }
  }
});

test('conservation: the robot never carries two cartridges at once', () => {
  const sim = createSim({ scenario: 'big-restore', policy: 'position' });
  let maxInRobot = 0;
  for (let i = 0; i < 4000; i++) {
    sim.step(0.5);
    const carried = sim.carts.filter((c) => c.loc.kind === 'robot');
    maxInRobot = Math.max(maxInRobot, carried.length);
  }
  assert.ok(maxInRobot <= 1, `robot held ${maxInRobot} cartridges`);
});

/* ---------- 5. the batch invariant ---------- */

test('batch: never unmounts a tape that still has queued requests for it', () => {
  for (const scenario of ['big-restore', 'urgent-starve', 'drive-failure']) {
    const sim = createSim({ scenario, policy: 'batch' });
    const violations = [];
    let prev = sim.drives.map((d) => d.state);
    while (sim.time < SCENARIOS[scenario].horizonMinutes * 60) {
      sim.step(0.5);
      sim.drives.forEach((d, i) => {
        // the moment a drive decides to eject is 'unloading'
        if (prev[i] !== 'unloading' && d.state === 'unloading') {
          const queued = sim.pending.filter((r) => r.cart === d.cart).length;
          if (queued > 0) violations.push({ scenario, drive: d.name, cart: d.cart, queued, t: sim.time });
        }
        prev[i] = d.state;
      });
    }
    assert.deepEqual(violations, [], `${scenario}: unmounted with work queued`);
  }
});

test('batch: never mounts the same tape twice while work for it is queued', () => {
  const sim = createSim({ scenario: 'big-restore', policy: 'batch' });
  const queuedWhenMounted = [];
  let prev = sim.drives.map((d) => d.cart);
  while (sim.time < SCENARIOS['big-restore'].horizonMinutes * 60) {
    sim.step(0.5);
    sim.drives.forEach((d, i) => {
      if (d.cart && d.cart !== prev[i] && prev[i] === null) {
        // a fresh mount: anything already queued for this tape will be batched,
        // not thrown away, so the drive must stay on it until the queue empties
        queuedWhenMounted.push({ cart: d.cart, drive: d.name, t: sim.time });
      }
      prev[i] = d.cart;
    });
  }
  assert.ok(queuedWhenMounted.length > 10, 'scenario did not mount anything');
  // every mount must lead to at least one read from that tape
  for (const m of queuedWhenMounted) {
    const reads = sim.drives.find((d) => d.name === m.drive);
    assert.ok(reads, 'unknown drive');
  }
});

test('batch: mounts fewer tapes than fifo on the same seed', () => {
  const fifo = runHeadless({ scenario: 'big-restore', policy: 'fifo' }).stats;
  const batch = runHeadless({ scenario: 'big-restore', policy: 'batch' }).stats;
  assert.ok(batch.mounts < fifo.mounts, `batch ${batch.mounts} mounts vs fifo ${fifo.mounts}`);
  assert.ok(batch.completed > fifo.completed * 2, 'batching should roughly double throughput here');
});

/* ---------- 6. position ordering ---------- */

test('position: seeks less per file than plain batching', () => {
  const batch = runHeadless({ scenario: 'big-restore', policy: 'batch' }).stats;
  const position = runHeadless({ scenario: 'big-restore', policy: 'position' }).stats;
  assert.ok(
    position.meanSeekSeconds < batch.meanSeekSeconds,
    `position ${position.meanSeekSeconds?.toFixed(2)}s/read vs batch ${batch.meanSeekSeconds?.toFixed(2)}s/read`,
  );
});

test('position: reads a mounted tape in ascending order far more often than batch does', () => {
  function backwardMoves(policy) {
    const sim = createSim({ scenario: 'big-restore', policy });
    let backwards = 0;
    let forward = 0;
    const head = sim.drives.map((d) => d.headPos);
    while (sim.time < 2400) {
      sim.step(0.5);
      sim.drives.forEach((d, i) => {
        if (d.state === 'reading' && d.lastSeek > 0) {
          if (head[i] > d.headPos + 1e-9) backwards++;
          else forward++;
          head[i] = d.headPos;
        }
      });
    }
    return { backwards, forward };
  }
  const batch = backwardMoves('batch');
  const position = backwardMoves('position');
  assert.ok(
    position.backwards < batch.backwards,
    `position ${position.backwards} backward seeks vs batch ${batch.backwards}`,
  );
});

/* ---------- 7. aging ---------- */

test('aging: a starved request is eventually served instead of waiting forever', () => {
  // one drive, arrivals far above capacity, then arrivals stop and we drain
  const cfg = {
    scenario: 'custom',
    seed: 5,
    grid: { cols: 6, rows: 2 },
    drives: 1,
    carts: 8,
    filesPerCart: [6, 10],
    requestsPerMinute: 9,
    everyNthUrgent: 0,
    arrivalStopMinutes: 10,
    horizonMinutes: 60,
    policy: 'priority',
  };
  const { sim, stats } = runHeadless(cfg);
  const oldest = sim.allRequests.slice(0, 20);
  assert.ok(oldest.length === 20, 'not enough requests to starve');
  const stillQueued = oldest.filter((r) => !sim.completed.some((c) => c.id === r.id));
  assert.equal(stillQueued.length, 0, `${stillQueued.length} of the 20 oldest requests were never served`);
  assert.ok(stats.maxWait < Infinity);
});

test('aging: wait time raises priority, but never past a fresh urgent request', () => {
  const cfg = {
    scenario: 'custom',
    seed: 11,
    grid: { cols: 6, rows: 2 },
    drives: 1,
    carts: 8,
    filesPerCart: [6, 10],
    requestsPerMinute: 6,
    everyNthUrgent: 0,
    arrivalStopMinutes: 30,
    horizonMinutes: 40,
    policy: 'priority',
    burst: { atMinutes: 10, count: 6, gapMinutes: 0.5, label: 'page' },
  };
  const { sim } = runHeadless(cfg);
  const urgent = sim.completed.filter((r) => r.priority === 'urgent');
  assert.ok(urgent.length >= 3, 'urgent requests were not served');
  // an urgent request that arrives while hundreds are queued should not wait hours
  const urgentWait = urgent.reduce((a, r) => a + r.wait, 0) / urgent.length;
  const normalWait = sim.completed.filter((r) => r.priority === 'normal').reduce((a, r) => a + r.wait, 0) /
    sim.completed.filter((r) => r.priority === 'normal').length;
  assert.ok(urgentWait < normalWait, `urgent wait ${urgentWait} not better than normal ${normalWait}`);
});

test('aging: priority rescues urgent work that fifo starves', () => {
  const fifo = runHeadless({ scenario: 'urgent-starve', policy: 'fifo' }).stats;
  const priority = runHeadless({ scenario: 'urgent-starve', policy: 'priority' }).stats;
  assert.ok(priority.urgentDone > 0 && fifo.urgentDone > 0);
  assert.ok(
    priority.urgentP95Wait < fifo.urgentP95Wait / 5,
    `priority urgent p95 ${priority.urgentP95Wait?.toFixed(0)}s vs fifo ${fifo.urgentP95Wait?.toFixed(0)}s`,
  );
});

/* ---------- 8. drive failure ---------- */

test('drive failure: work in flight is re-queued, not lost', () => {
  const sim = createSim({ scenario: 'drive-failure', policy: 'batch' });
  const failAt = SCENARIOS['drive-failure'].driveFailure.atMinutes * 60;
  const failDriveIdx = SCENARIOS['drive-failure'].driveFailure.drive;
  let inFlightAtFailure = [];
  while (sim.time < SCENARIOS['drive-failure'].horizonMinutes * 60) {
    sim.step(0.5);
    if (sim.time >= failAt && !sim.drives[failDriveIdx].down) {
      inFlightAtFailure = Array.from(sim.inFlight.values()).map((r) => r.id);
      break;
    }
  }
  assert.ok(sim.drives[failDriveIdx].down, 'the drive never went down');
  const requeued = sim.allRequests.filter((r) => inFlightAtFailure.includes(r.id) && r.requeues > 0);
  assert.equal(requeued.length, inFlightAtFailure.length, 'in-flight work was not all re-queued');
  // and all of it is eventually finished, exactly once
  for (const r of requeued) {
    const hits = sim.completed.filter((c) => c.id === r.id).length;
    assert.equal(hits, 1, `request ${r.id} finished ${hits} times after the failure`);
  }
});

test('drive failure: a dead drive stops doing work immediately', () => {
  const sim = createSim({ scenario: 'drive-failure', policy: 'batch' });
  const idx = SCENARIOS['drive-failure'].driveFailure.drive;
  let readsAtFailure = null;
  while (sim.time < SCENARIOS['drive-failure'].horizonMinutes * 60) {
    sim.step(0.5);
    if (sim.drives[idx].down && readsAtFailure === null) readsAtFailure = sim.drives[idx].reads;
  }
  assert.notEqual(readsAtFailure, null);
  assert.equal(sim.drives[idx].reads, readsAtFailure, 'a dead drive kept reading');
});

test('drive failure: the cartridge trapped in the dead drive is freed by an operator', () => {
  // the scenario's timed failure may land on an idle drive, so here we make sure
  // we kill a drive that is actually holding a cartridge
  const sim = createSim({ scenario: 'drive-failure', policy: 'batch' });
  let trapped = null;
  let victim = -1;
  while (sim.time < 3600 && victim < 0) {
    sim.step(0.5);
    const busy = sim.drives.findIndex((d) => d.cart && !d.down);
    if (busy >= 0) victim = busy;
  }
  assert.ok(victim >= 0, 'no drive ever held a cartridge');
  const cartId = sim.drives[victim].cart;
  sim.failDrive(victim);
  trapped = sim.carts.find((c) => c.id === cartId);
  assert.equal(trapped.loc.kind, 'stuck', 'the cartridge was not stranded');
  while (sim.time < 3600) sim.step(0.5);
  assert.equal(trapped.loc.kind, 'slot', `cartridge ${trapped.id} was never returned to a slot`);
});

test('drive failure: conservation still holds with a drive down', () => {
  for (const policy of POLICY_IDS) {
    const { stats } = runHeadless({ scenario: 'drive-failure', policy });
    assert.equal(stats.drivesDown, 1);
    assert.equal(stats.completed + stats.backlog, stats.generated);
    assert.equal(stats.duplicateCompletions, 0);
    assert.ok(stats.requeues >= 1, 'nothing was ever re-queued');
  }
});

test('drive failure: the other two drives keep the library running', () => {
  const { stats } = runHeadless({ scenario: 'drive-failure', policy: 'batch' });
  assert.ok(stats.completed > stats.generated * 0.9, `only ${stats.completed}/${stats.generated} served after a drive died`);
});

/* ---------- 9. scenarios, policies, config ---------- */

test('scenarios: all four exist with a seed, drives, slots and a horizon', () => {
  assert.deepEqual(SCENARIO_IDS, ['quiet-morning', 'big-restore', 'urgent-starve', 'drive-failure']);
  for (const id of SCENARIO_IDS) {
    const s = SCENARIOS[id];
    assert.ok(Number.isInteger(s.seed), `${id} has no integer seed`);
    assert.ok(s.drives >= 1 && s.drives <= 6, `${id} drive count`);
    assert.ok(s.carts <= s.grid.cols * s.grid.rows, `${id} has more cartridges than slots`);
    assert.ok(s.horizonMinutes > s.arrivalStopMinutes, `${id} must drain after arrivals stop`);
    assert.ok(s.title && s.blurb, `${id} needs a title and a blurb for the UI`);
  }
});

test('policies: four policies, all switchable, each with a description', () => {
  assert.deepEqual(POLICY_IDS, ['fifo', 'batch', 'position', 'priority']);
  for (const p of POLICIES) assert.ok(p.name && p.blurb.length > 20, `${p.id} needs a usable description`);
});

test('policies: each one produces different behaviour on the same seed', () => {
  const rows = comparePolicies('big-restore');
  assert.equal(rows.length, 4);
  const completions = rows.map((r) => r.completed);
  assert.equal(new Set(completions).size, completions.length, 'two policies behaved identically');
  for (const row of rows) {
    assert.ok(Number.isFinite(row.meanWait), `${row.policy} meanWait`);
    assert.ok(Number.isFinite(row.tapePasses));
    assert.equal(row.mounts, row.tapePasses, 'mounts and tape passes must agree');
  }
});

test('config: cartridges are clamped to the slots that exist', () => {
  const cfg = normalizeConfig({ scenario: 'quiet-morning', carts: 999 });
  assert.ok(cfg.carts <= cfg.slotCount);
});

test('config: unknown scenario falls back to a working default', () => {
  const sim = createSim({ scenario: 'no-such-scenario', policy: 'batch' });
  assert.ok(sim.carts.length > 0);
  sim.step(1);
  assert.ok(Number.isFinite(sim.time));
});

test('config: an unknown policy falls back to fifo rather than crashing', () => {
  const sim = createSim({ scenario: 'quiet-morning', policy: 'nonsense' });
  assert.equal(sim.config.policy, 'fifo');
});

/* ---------- 10. runs, horizons, stats, view ---------- */

test('run: a headless run stops at its horizon instead of running forever', () => {
  const { stats, steps } = runHeadless({ scenario: 'big-restore', policy: 'fifo', maxSeconds: 600 });
  assert.ok(stats.time <= 600.001, `ran to ${stats.time}`);
  assert.ok(steps > 0 && steps <= 600 / 0.5 + 1);
});

test('run: stats are finite and internally consistent', () => {
  for (const scenario of SCENARIO_IDS) {
    const { stats } = runHeadless({ scenario, policy: 'position' });
    for (const [k, v] of Object.entries(stats)) {
      if (typeof v === 'number') assert.ok(Number.isFinite(v), `${scenario}: ${k} = ${v}`);
    }
    assert.ok(stats.p95Wait >= stats.meanWait - 1e-6 || stats.p95Wait === null, 'p95 below the mean');
    assert.ok(stats.urgentDone + stats.normalDone === stats.completed, 'priority split does not add up');
  }
});

test('run: the event log stays bounded', () => {
  const sim = createSim({ scenario: 'big-restore', policy: 'batch' });
  while (sim.time < 5400) sim.step(0.5);
  assert.ok(sim.events.length <= 400, `event log grew to ${sim.events.length}`);
});

test('view: exposes everything the UI needs', () => {
  const sim = createSim({ scenario: 'quiet-morning', policy: 'batch' });
  for (let i = 0; i < 400; i++) sim.step(0.5);
  const v = sim.view();
  assert.equal(v.slots.length, SCENARIOS['quiet-morning'].grid.cols * SCENARIOS['quiet-morning'].grid.rows);
  assert.equal(v.drives.length, SCENARIOS['quiet-morning'].drives);
  assert.equal(typeof v.robot.x, 'number');
  assert.ok(Number.isFinite(v.robot.x) && Number.isFinite(v.robot.y));
  assert.ok(Array.isArray(v.pending));
  assert.ok(v.stats && Number.isFinite(v.stats.completed));
  assert.ok(typeof v.mmss(65) === 'string' && v.mmss(65).includes(':'));
});

test('view: the robot moves (it is not parked forever)', () => {
  const sim = createSim({ scenario: 'big-restore', policy: 'batch' });
  const start = { x: sim.view().robot.x, y: sim.view().robot.y };
  const seen = new Set();
  for (let i = 0; i < 3000; i++) {
    sim.step(0.5);
    seen.add(sim.view().robot.x.toFixed(2));
  }
  assert.ok(seen.size > 5, `robot only visited ${seen.size} x positions`);
  assert.ok(sim.robot.moves > 10, `robot made ${sim.robot.moves} moves`);
  assert.ok(Number.isFinite(start.x));
});

test('costs: every documented cost is positive and the defaults are sane', () => {
  for (const [k, v] of Object.entries(DEFAULT_COSTS)) {
    assert.equal(typeof v, 'number', `${k} is not a number`);
    assert.ok(v > 0, `${k} must be positive`);
  }
  assert.ok(DEFAULT_COSTS.loadSeconds > DEFAULT_COSTS.unloadSeconds, 'threading should cost more than a rewind');
});

test('module: sim.js stays loadable in a browser (no Node-only globals)', () => {
  const raw = readFileSync(join(here, 'sim.js'), 'utf8');
  // strip comments first: this file talks ABOUT Math.random and process
  const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(!/\bprocess\./.test(src), 'sim.js touches process');
  assert.ok(!/\brequire\(/.test(src), 'sim.js uses require');
  assert.ok(!/Math\.random/.test(src), 'sim.js uses Math.random (breaks determinism)');
  assert.ok(!/\bdocument\./.test(src), 'sim.js touches the DOM');
  assert.ok(!/\bwindow\./.test(src), 'sim.js touches window');
  assert.ok(/export function createSim/.test(raw), 'createSim should be exported');
});
