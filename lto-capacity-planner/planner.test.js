/*
 * planner.test.js — run with:  node --test
 * Node's built-in runner only; no npm install needed for these.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  GENERATIONS,
  DEFAULTS,
  DEFAULT_PRICES,
  UNITS,
  plan,
  normalizeInput,
  ceilTapes,
  dataAtYear,
  effectiveTB,
  encodeState,
  decodeState,
  fmtTB,
} from './planner.js';

const row = (p, id) => p.rows.find((r) => r.id === id);
const base = { size: 100, unit: 'TB', growth: 0, years: 0, copies: 1, inLibrary: 1, ratio: 1, fill: 100, spare: 0 };

test('generation table matches the published capacities', () => {
  const caps = Object.fromEntries(GENERATIONS.map((g) => [g.name, [g.native, g.native * g.ratedRatio]]));
  assert.deepEqual(caps, {
    'LTO-7': [6, 15],
    'LTO-8': [12, 30],
    'LTO-9': [18, 45],
    'LTO-10 (30 TB)': [30, 75],
    'LTO-10 (40 TB)': [40, 100],
  });
  for (const g of GENERATIONS) assert.match(g.source, /^https:\/\//, `${g.name} has a source URL`);
});

test('exact multiples do not round up an extra cartridge', () => {
  const p = plan({ ...base, size: 120 });
  assert.equal(row(p, 'lto8').tapesPerCopy, 10);
  assert.equal(row(p, 'lto7').tapesPerCopy, 20);
  // 0.1 + 0.2 style noise: 3 × 6 TB cartridges must be 18, not 19
  assert.equal(ceilTapes((0.1 + 0.2) * 60), 18);
  assert.equal(ceilTapes(18.0001), 19, 'a real remainder still rounds up');
});

test('a partial cartridge rounds up', () => {
  const p = plan({ ...base, size: 121 });
  assert.equal(row(p, 'lto8').tapesPerCopy, 11);
  assert.equal(row(p, 'lto10x').tapesPerCopy, 4);
});

test('zero data needs zero cartridges and costs nothing', () => {
  const p = plan({ ...base, size: 0, copies: 3, spare: 50 });
  for (const r of p.rows) {
    assert.equal(r.tapes, 0);
    assert.equal(r.slots, 0);
    assert.deepEqual(r.cost, [0, 0]);
    assert.deepEqual(r.costPerTB, [0, 0]);
  }
});

test('growth compounds yearly and the plan sizes for the end of retention', () => {
  assert.ok(Math.abs(dataAtYear(100, 10, 2) - 121) < 1e-9);
  const p = plan({ ...base, size: 100, growth: 10, years: 2 });
  assert.ok(Math.abs(p.peakTB - 121) < 1e-9);
  assert.equal(row(p, 'lto9').tapesPerCopy, 7); // 121 / 18 = 6.72
});

test('negative growth plans for today, not the shrunken future', () => {
  const p = plan({ ...base, size: 100, growth: -50, years: 3 });
  assert.equal(p.peakTB, 100);
  assert.equal(row(p, 'lto10').tapesPerCopy, 4);
});

test('compression ratio and fill scale the effective capacity', () => {
  const g = GENERATIONS.find((x) => x.id === 'lto9');
  assert.equal(effectiveTB(g, 2.5, 100), 45);
  assert.equal(effectiveTB(g, 1, 50), 9);
  const p = plan({ ...base, size: 90, ratio: 2.5 });
  assert.equal(row(p, 'lto9').tapesPerCopy, 2);
  const q = plan({ ...base, size: 90, fill: 50 });
  assert.equal(row(q, 'lto9').tapesPerCopy, 10);
});

test('copies multiply cartridges; slots count only library copies plus spare', () => {
  const p = plan({ ...base, size: 120, copies: 3, inLibrary: 1, spare: 10 });
  const r = row(p, 'lto8');
  assert.equal(r.tapesPerCopy, 10);
  assert.equal(r.tapes, 30);
  assert.equal(r.libraryTapes, 10);
  assert.equal(r.slots, 11);
  const offsite = plan({ ...base, size: 120, copies: 2, inLibrary: 0 });
  assert.equal(row(offsite, 'lto8').slots, 0);
});

test('copies in library cannot exceed total copies', () => {
  assert.equal(normalizeInput({ copies: 2, inLibrary: 5 }).inLibrary, 2);
});

test('cost range is cartridges × [low, high] price', () => {
  const p = plan({ ...base, size: 120, copies: 2, prices: { lto8: [50, 80] } });
  const r = row(p, 'lto8');
  assert.deepEqual(r.cost, [20 * 50, 20 * 80]);
  assert.deepEqual(r.costPerTB, [(20 * 50) / 120, (20 * 80) / 120]);
});

test('swapped price bounds are put back in order', () => {
  assert.deepEqual(normalizeInput({ prices: { lto9: [200, 100] } }).prices.lto9, [100, 200]);
});

test('units: decimal and binary prefixes convert to decimal TB', () => {
  assert.equal(UNITS.PB, 1000);
  assert.equal(UNITS.GB, 0.001);
  // 1 PiB = 1.1259 PB, so 100 PiB on 30 TB cartridges is 3753, not 3334
  const p = plan({ ...base, size: 100, unit: 'PiB' });
  assert.equal(row(p, 'lto10').tapesPerCopy, Math.ceil((100 * 2 ** 50) / 1e12 / 30));
  assert.equal(row(p, 'lto10').tapesPerCopy, 3753);
});

test('schedule is per year, monotone, and ends at the headline number', () => {
  const p = plan({ ...base, size: 100, growth: 30, years: 4, copies: 2 });
  for (const r of p.rows) {
    assert.equal(r.schedule.length, 5);
    assert.equal(r.schedule.at(-1).tapes, r.tapes);
    for (let i = 1; i < r.schedule.length; i++) assert.ok(r.schedule[i].tapes >= r.schedule[i - 1].tapes);
  }
  const shrink = plan({ ...base, size: 100, growth: -20, years: 3 });
  for (const r of shrink.rows) assert.ok(r.schedule.every((s) => s.tapes === r.tapes), 'never un-buys tapes');
});

test('newer generations never need more cartridges than older ones', () => {
  const p = plan({ size: 3.7, unit: 'PB', growth: 25, years: 7, copies: 2 });
  for (let i = 1; i < p.rows.length; i++) assert.ok(p.rows[i].tapes <= p.rows[i - 1].tapes);
});

test('normalizeInput clamps and falls back on garbage', () => {
  const n = normalizeInput({ size: 'abc', unit: 'XB', growth: '1e9', years: -3, copies: 2.6, ratio: 0, fill: 500, spare: '' });
  assert.equal(n.size, DEFAULTS.size);
  assert.equal(n.unit, DEFAULTS.unit);
  assert.equal(n.growth, 1000);
  assert.equal(n.years, 0);
  assert.equal(n.copies, 3);
  assert.equal(n.ratio, 1);
  assert.equal(n.fill, 100);
  assert.equal(n.spare, DEFAULTS.spare);
  assert.deepEqual(n.prices, DEFAULT_PRICES);
  assert.ok(normalizeInput({ size: Infinity }).size === DEFAULTS.size);
});

test('URL state: defaults encode to nothing and round-trip', () => {
  assert.equal(encodeState({}), '');
  assert.deepEqual(decodeState(''), normalizeInput({}));
  const input = { size: 2.5, unit: 'PB', growth: 15, years: 7, copies: 3, inLibrary: 2, ratio: 1.5, fill: 85, spare: 20, prices: { lto10: [180, 260] } };
  const qs = encodeState(input);
  assert.deepEqual(decodeState(qs), normalizeInput(input));
  assert.deepEqual(decodeState(`?${qs}`), normalizeInput(input));
  assert.match(qs, /p_lto10=180-260/);
  assert.doesNotMatch(qs, /p_lto9/, 'unchanged prices stay out of the URL');
});

test('URL state: hostile or malformed params fall back to defaults', () => {
  const d = decodeState('?s=-5&u=<script>&c=999&p_lto8=1e3-x&p_lto9=--&y=NaN');
  assert.equal(d.size, 0);
  assert.equal(d.unit, 'TB');
  assert.equal(d.copies, 10);
  assert.equal(d.years, DEFAULTS.years);
  assert.deepEqual(d.prices.lto8, DEFAULT_PRICES.lto8);
  assert.deepEqual(d.prices.lto9, DEFAULT_PRICES.lto9);
});

test('fmtTB switches to PB at 1000 TB', () => {
  assert.equal(fmtTB(12), '12 TB');
  assert.equal(fmtTB(1500), '1.5 PB');
  assert.equal(fmtTB(2.345), '2.35 TB');
});

test('planner.js stays pure (browser and Node load the same file)', () => {
  const src = readFileSync(new URL('./planner.js', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '');
  for (const word of ['document', 'window', 'process', 'require(', 'fetch(', 'Math.random', 'import ']) {
    assert.ok(!src.includes(word), `planner.js must not use ${word}`);
  }
});
