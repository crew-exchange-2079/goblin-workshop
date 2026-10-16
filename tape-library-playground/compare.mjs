#!/usr/bin/env node
/*
 * compare.mjs — run every scenario under every policy, headless, and print a table.
 *
 *   node compare.mjs                 # all scenarios
 *   node compare.mjs big-restore     # one scenario
 *   node compare.mjs --seed 7        # different seed
 *
 * Uses only sim.js (which has no Node-only globals) and console.log. No npm install.
 */
import { SCENARIOS, SCENARIO_IDS, POLICIES, comparePolicies, fmt, secondsToClock } from './sim.js';

const argv = process.argv.slice(2);
const only = argv.filter((a) => !a.startsWith('-'));
const seedIdx = argv.indexOf('--seed');
const seed = seedIdx >= 0 ? Number(argv[seedIdx + 1]) : null;
const scenarios = only.length ? only.filter((id) => SCENARIOS[id]) : SCENARIO_IDS;

if (only.length && only.some((id) => !SCENARIOS[id])) {
  console.log(`unknown scenario. try: ${SCENARIO_IDS.join(', ')}`);
  process.exit(1);
}

const COLS = [
  ['policy', 22, (r) => r.name],
  ['done', 10, (r) => `${r.completed}/${r.generated}`],
  ['left', 7, (r) => String(r.backlog)],
  ['mean wait', 10, (r) => fmt(r.meanWait, 0) + 's'],
  ['p95 wait', 10, (r) => fmt(r.p95Wait, 0) + 's'],
  ['worst wait', 11, (r) => fmt(r.maxWait, 0) + 's'],
  ['urgent p95', 11, (r) => fmt(r.urgentP95Wait, 0) + 's'],
  ['mounts', 8, (r) => String(r.mounts)],
  ['tape passes', 12, (r) => String(r.tapePasses)],
  ['mounts/h', 10, (r) => fmt(r.mountsPerHour, 1)],
  ['files/h', 9, (r) => fmt(r.filesPerHour, 0)],
  ['seek/read', 10, (r) => fmt(r.meanSeekSeconds, 2) + 's'],
  ['robot busy', 11, (r) => fmt(r.robotBusyFraction * 100, 0) + '%'],
];

function row(cells) {
  return cells
    .map((text, i) => String(text).padEnd(COLS[i][1]))
    .join(' ')
    .trimEnd();
}

console.log('TAPE LIBRARY PLAYGROUND — same seed, four policies, headless');
console.log('all times are simulated seconds; waits are measured from request arrival');
console.log('to the moment its cartridge starts being threaded into a drive\n');

for (const id of scenarios) {
  const s = SCENARIOS[id];
  const opts = seed === null ? {} : { seed };
  const rows = comparePolicies(id, opts);
  console.log('='.repeat(118));
  console.log(`${s.title}  [${id}]  seed ${seed ?? s.seed}`);
  console.log(`  ${s.blurb}`);
  console.log(
    `  library: ${s.grid.cols}x${s.grid.rows} slots (${s.carts} cartridges), ${s.drives} drives, one robot; ` +
      `${s.requestsPerMinute}/min for ${s.arrivalStopMinutes} min, then ${s.horizonMinutes} min horizon` +
      (s.driveFailure ? `; drive ${s.driveFailure.drive + 1} dies at ${s.driveFailure.atMinutes} min` : '') +
      (s.burst ? `; ${s.burst.count} urgent "${s.burst.label}" requests from ${s.burst.atMinutes} min` : ''),
  );
  console.log('-'.repeat(118));
  console.log(row(COLS.map((c) => c[0])));
  console.log('-'.repeat(118));
  for (const r of rows) console.log(row(COLS.map((c) => c[2](r))));
  const better = (a, b) => (b.completed > a.completed || (b.completed === a.completed && b.meanWait < a.meanWait));
  const best = rows.reduce((a, b) => (better(a, b) ? b : a));
  const worseUrgent = (a, b) => (b.urgentP95Wait ?? 0) > (a.urgentP95Wait ?? 0);
  const worstUrgent = rows.reduce((a, b) => (worseUrgent(a, b) ? b : a));
  console.log('-'.repeat(118));
  console.log(
    `  served the most: ${best.name} (${best.completed}/${best.generated}, mean wait ${fmt(best.meanWait, 0)}s). ` +
      `worst urgent tail: ${worstUrgent.name} (p95 ${secondsToClock(worstUrgent.urgentP95Wait)}).`,
  );
  console.log(`  (four headless runs, no wall-clock: the same seed always prints this exact table)\n`);
}

console.log('Reading the table:');
console.log('  done        files read / files requested — throughput over the whole horizon');
console.log('  left        still queued when the clock ran out — the backlog nobody got to');
console.log('  mean wait   average seconds a request sat queued before its tape started mounting');
console.log('  p95/worst   the tail: the requests people actually complained about');
console.log('  mounts      how many times a cartridge was threaded into a drive');
console.log('  tape passes mounts counted per cartridge — the same number, one pass per mount');
console.log('  seek/read   seconds spent seeking per file read — how much motion found nothing');
console.log('  robot busy  share of the run the single robot spent moving cartridges');
console.log('\nEvery number is invented: the library, the cartridges, the files and the costs');
console.log('are illustrative, not measurements of any real site.');
