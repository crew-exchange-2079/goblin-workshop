/*
 * app.js — the browser side. All arithmetic lives in planner.js; this file
 * reads the form, draws the tables and keeps the URL in sync so a plan can be
 * shared by copying the address bar. No libraries, no network.
 */
import {
  GENERATIONS,
  DEFAULTS,
  DEFAULT_PRICES,
  plan,
  encodeState,
  decodeState,
  fmtTB,
  fmtInt,
  fmtUSD,
} from './planner.js';

const $ = (id) => document.getElementById(id);
const form = $('form');
const SCALARS = ['size', 'unit', 'growth', 'years', 'copies', 'inLibrary', 'ratio', 'fill', 'spare'];

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  for (const k of kids) n.append(k);
  return n;
}

/* price inputs: one low/high pair per generation */
for (const g of GENERATIONS) {
  const lo = el('input', { id: `p-${g.id}-lo`, type: 'number', min: '0', step: 'any', 'aria-label': `${g.name} low price` });
  const hi = el('input', { id: `p-${g.id}-hi`, type: 'number', min: '0', step: 'any', 'aria-label': `${g.name} high price` });
  $('pricefields').append(el('div', { class: 'field' }, el('label', {}, g.name), el('div', { class: 'pair' }, lo, '–', hi)));
}

for (const g of GENERATIONS) {
  const a = el('a', { href: g.source, rel: 'noopener noreferrer' }, g.source);
  $('sources').append(el('li', {}, `${g.name}: ${g.native} TB native, ${g.native * g.ratedRatio} TB at ${g.ratedRatio}:1 — `, a));
}

function fill(input) {
  for (const k of SCALARS) $(k).value = String(input[k]);
  for (const g of GENERATIONS) {
    $(`p-${g.id}-lo`).value = String(input.prices[g.id][0]);
    $(`p-${g.id}-hi`).value = String(input.prices[g.id][1]);
  }
}

function read() {
  const raw = { prices: {} };
  for (const k of SCALARS) raw[k] = $(k).value;
  for (const g of GENERATIONS) raw.prices[g.id] = [$(`p-${g.id}-lo`).value, $(`p-${g.id}-hi`).value];
  return raw;
}

function render(p) {
  const { input } = p;
  const growthWord = input.growth === 0 ? 'no growth' : `${input.growth}%/yr growth`;
  $('summary').textContent =
    `${fmtTB(p.startTB)} today, ${growthWord}, kept ${input.years} yr → plan for ${fmtTB(p.peakTB)} per copy, ` +
    `${input.copies} ${input.copies === 1 ? 'copy' : 'copies'} (${input.inLibrary} in the library), ` +
    `${input.ratio}:1 compression, ${input.fill}% fill.`;

  const tbody = $('results').tBodies[0];
  tbody.replaceChildren();
  for (const r of p.rows) {
    const tr = el('tr', { 'data-gen': r.id });
    tr.append(
      el('th', { scope: 'row' }, r.name),
      el('td', {}, `${r.nativeTB} / ${r.ratedCompressedTB} TB`),
      el('td', {}, fmtTB(r.effectiveTB)),
      el('td', { class: 'num', 'data-k': 'per-copy' }, fmtInt(r.tapesPerCopy)),
      el('td', { class: 'num', 'data-k': 'tapes' }, fmtInt(r.tapes)),
      el('td', { class: 'num', 'data-k': 'slots' }, fmtInt(r.slots)),
      el('td', { class: 'num', 'data-k': 'cost' }, `${fmtUSD(r.cost[0])} – ${fmtUSD(r.cost[1])}`),
    );
    tbody.append(tr);
  }

  const sched = $('schedule');
  sched.replaceChildren();
  const head = el('tr', {}, el('th', { scope: 'col' }, 'Year'), el('th', { scope: 'col' }, 'Data per copy'));
  for (const r of p.rows) head.append(el('th', { scope: 'col', class: 'num' }, r.name));
  sched.append(el('thead', {}, head));
  const body = el('tbody');
  p.rows[0].schedule.forEach((s, i) => {
    const tr = el('tr', {}, el('th', { scope: 'row' }, String(s.year)), el('td', {}, fmtTB(s.dataTB)));
    for (const r of p.rows) tr.append(el('td', { class: 'num' }, fmtInt(r.schedule[i].tapes)));
    body.append(tr);
  });
  sched.append(body);
}

function update() {
  const p = plan(read());
  render(p);
  const qs = encodeState(p.input);
  history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
}

form.addEventListener('input', update);
form.addEventListener('submit', (e) => e.preventDefault());

$('reset').addEventListener('click', () => {
  fill({ ...DEFAULTS, prices: DEFAULT_PRICES });
  update();
});

$('copylink').addEventListener('click', async () => {
  const status = $('copystatus');
  try {
    await navigator.clipboard.writeText(location.href);
    status.textContent = 'link copied';
  } catch {
    status.textContent = 'copy the address bar — clipboard unavailable';
  }
  setTimeout(() => (status.textContent = ''), 2500);
});

fill(decodeState(location.search));
update();
