/*
 * planner.js — the arithmetic behind the LTO capacity planner, as a pure module.
 *
 * Pure: no DOM, no Node-only globals, no network. The browser loads this exact
 * file and planner.test.js imports it under `node --test`.
 *
 * Units: every capacity is in decimal terabytes (1 TB = 10^12 bytes), which is
 * how the LTO Program and the media vendors quote cartridge capacity. Binary
 * input units (TiB, PiB) are converted on the way in.
 */

/*
 * Cartridge capacities per generation. `native` is the uncompressed capacity
 * in TB; `ratedRatio` is the compression ratio the LTO Program uses for its
 * "compressed" figure (2.5:1 for every generation listed here), so the rated
 * compressed capacity is native × ratedRatio. The planner never uses the rated
 * ratio for sizing: it uses the ratio the user enters (default 1:1, native).
 *
 * Sources (accessed 2026-10-23; see README.md for how they were checked):
 *   LTO-7  — https://www.lto.org/lto-7/
 *   LTO-8  — https://www.lto.org/lto-8/
 *   LTO-9  — https://www.lto.org/lto-9/ ; StorageNewsletter, 2020-09-10,
 *            https://storagenewsletter.com/2020/09/10/lto-9-arriving-as-specs-available
 *   LTO-10 — LTO Program press release, 2025-08-13,
 *            https://www.businesswire.com/news/home/20250813251001/en/
 *   LTO-10 40 TB cartridge — LTO Program announcement, StorageNewsletter 2025-11-19,
 *            https://www.storagenewsletter.com/2025/11/19/lto-program-announces-new-40tb-lto-10-tape-cartridge-specifications/
 */
export const GENERATIONS = [
  { id: 'lto7', name: 'LTO-7', native: 6, ratedRatio: 2.5, source: 'https://www.lto.org/lto-7/' },
  { id: 'lto8', name: 'LTO-8', native: 12, ratedRatio: 2.5, source: 'https://www.lto.org/lto-8/' },
  { id: 'lto9', name: 'LTO-9', native: 18, ratedRatio: 2.5, source: 'https://www.lto.org/lto-9/' },
  {
    id: 'lto10',
    name: 'LTO-10 (30 TB)',
    native: 30,
    ratedRatio: 2.5,
    source: 'https://www.businesswire.com/news/home/20250813251001/en/',
  },
  {
    id: 'lto10x',
    name: 'LTO-10 (40 TB)',
    native: 40,
    ratedRatio: 2.5,
    source:
      'https://www.storagenewsletter.com/2025/11/19/lto-program-announces-new-40tb-lto-10-tape-cartridge-specifications/',
  },
];

export const GENERATION_IDS = GENERATIONS.map((g) => g.id);

/*
 * Default per-cartridge price ranges in US dollars. These are PLACEHOLDERS:
 * rough street-price guesses, not quotes and not sourced. Prices move by
 * vendor, volume and month, so the UI makes every one editable and they ride
 * along in the shareable URL.
 */
export const DEFAULT_PRICES = {
  lto7: [30, 60],
  lto8: [50, 100],
  lto9: [70, 140],
  lto10: [150, 300],
  lto10x: [200, 400],
};

export const UNITS = { GB: 1e-3, TB: 1, PB: 1e3, TiB: 2 ** 40 / 1e12, PiB: 2 ** 50 / 1e12 };

export const DEFAULTS = {
  size: 500, // current dataset
  unit: 'TB',
  growth: 20, // % per year, compounded
  years: 5, // retention: how long the data must stay on tape
  copies: 2, // full copies of the data on tape
  inLibrary: 1, // how many of those copies live in the library (the rest go offsite)
  ratio: 1, // assumed compression ratio x:1 — 1 means plan on native capacity
  fill: 90, // % of a cartridge you expect to actually fill
  spare: 10, // % extra slots for scratch tapes, cleaning and headroom
};

/* Bounds used to clamp anything that arrives from a form or a URL. */
export const LIMITS = {
  size: [0, 1e9],
  growth: [-90, 1000],
  years: [0, 50],
  copies: [1, 10],
  inLibrary: [0, 10],
  ratio: [1, 10],
  fill: [1, 100],
  spare: [0, 200],
  price: [0, 1e6],
};

const clamp = (x, [lo, hi]) => Math.min(hi, Math.max(lo, x));

function num(v, fallback, bounds) {
  const x = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
  return Number.isFinite(x) ? clamp(x, bounds) : fallback;
}

/*
 * Ceiling that forgives floating-point noise: 12.000000000000002 tapes is 12
 * tapes, not 13. The tolerance is NOISE_ULPS units in the last place of the
 * result: x comes from about ten multiplications and divisions, each off by at
 * most half an ulp, so anything further from a whole number is a real
 * remainder and rounds up at any scale. Any positive amount of data needs at
 * least one tape.
 */
const NOISE_ULPS = 16;
export function ceilTapes(x) {
  if (!(x > 0)) return 0;
  const r = Math.round(x);
  return r >= 1 && Math.abs(x - r) <= NOISE_ULPS * Number.EPSILON * r ? r : Math.ceil(x);
}

/* Turn loose input (form values, URL params, partial objects) into a valid plan input. */
export function normalizeInput(raw = {}) {
  const d = DEFAULTS;
  const unit = Object.hasOwn(UNITS, raw.unit) ? raw.unit : d.unit;
  const copies = Math.round(num(raw.copies, d.copies, LIMITS.copies));
  const inLibrary = Math.min(copies, Math.round(num(raw.inLibrary, d.inLibrary, LIMITS.inLibrary)));
  const prices = {};
  for (const id of GENERATION_IDS) {
    const [dlo, dhi] = DEFAULT_PRICES[id];
    const p = raw.prices?.[id] ?? [];
    let lo = num(p[0], dlo, LIMITS.price);
    let hi = num(p[1], dhi, LIMITS.price);
    if (lo > hi) [lo, hi] = [hi, lo];
    prices[id] = [lo, hi];
  }
  return {
    size: num(raw.size, d.size, LIMITS.size),
    unit,
    growth: num(raw.growth, d.growth, LIMITS.growth),
    years: Math.round(num(raw.years, d.years, LIMITS.years)),
    copies,
    inLibrary,
    ratio: num(raw.ratio, d.ratio, LIMITS.ratio),
    fill: num(raw.fill, d.fill, LIMITS.fill),
    spare: num(raw.spare, d.spare, LIMITS.spare),
    prices,
  };
}

export const toTB = (size, unit) => size * UNITS[unit];

/* Dataset size in TB after `year` years of compounded growth. */
export const dataAtYear = (tb, growthPct, year) => tb * (1 + growthPct / 100) ** year;

/* TB one cartridge of `gen` is expected to hold under these assumptions. */
export const effectiveTB = (gen, ratio, fillPct) => gen.native * ratio * (fillPct / 100);

/*
 * The plan. For each generation:
 *   peakTB        = dataset at the end of retention (growth compounded yearly;
 *                   with negative growth the peak is today's size)
 *   tapesPerCopy  = ceil(peakTB / effective TB per cartridge)
 *   tapes         = tapesPerCopy × copies
 *   slots         = ceil(tapesPerCopy × copies in library × (1 + spare%))
 *   cost          = tapes × [low, high] price per cartridge
 * plus a per-year schedule of how many cartridges you own by the end of each year.
 */
export function plan(raw) {
  const input = normalizeInput(raw);
  const startTB = toTB(input.size, input.unit);
  const years = [];
  for (let y = 0; y <= input.years; y++) years.push(y);
  const peakTB = Math.max(...years.map((y) => dataAtYear(startTB, input.growth, y)));

  const rows = GENERATIONS.map((gen) => {
    const eff = effectiveTB(gen, input.ratio, input.fill);
    const tapesPerCopy = ceilTapes(peakTB / eff);
    const tapes = tapesPerCopy * input.copies;
    const libraryTapes = tapesPerCopy * input.inLibrary;
    const slots = ceilTapes(libraryTapes * (1 + input.spare / 100));
    const [plo, phi] = input.prices[gen.id];
    const schedule = years.map((y) => {
      // cartridges are never un-bought, so the running count is monotone
      const need = Math.max(...years.slice(0, y + 1).map((k) => dataAtYear(startTB, input.growth, k)));
      return { year: y, dataTB: dataAtYear(startTB, input.growth, y), tapes: ceilTapes(need / eff) * input.copies };
    });
    return {
      id: gen.id,
      name: gen.name,
      nativeTB: gen.native,
      ratedCompressedTB: gen.native * gen.ratedRatio,
      ratedRatio: gen.ratedRatio,
      effectiveTB: eff,
      tapesPerCopy,
      tapes,
      libraryTapes,
      slots,
      cost: [tapes * plo, tapes * phi],
      costPerTB: peakTB > 0 ? [(tapes * plo) / peakTB, (tapes * phi) / peakTB] : [0, 0],
      schedule,
      source: gen.source,
    };
  });

  return { input, startTB, peakTB, rows };
}

/* ---------------- shareable URL state ---------------- */

const SCALAR_KEYS = ['size', 'unit', 'growth', 'years', 'copies', 'inLibrary', 'ratio', 'fill', 'spare'];
const SHORT = { size: 's', unit: 'u', growth: 'g', years: 'y', copies: 'c', inLibrary: 'l', ratio: 'r', fill: 'f', spare: 'x' };

/*
 * Serialise to a compact query string. Only values that differ from the
 * defaults are written, so the default plan is an empty string.
 */
export function encodeState(raw) {
  const input = normalizeInput(raw);
  const q = new URLSearchParams();
  for (const k of SCALAR_KEYS) if (input[k] !== DEFAULTS[k]) q.set(SHORT[k], String(input[k]));
  for (const id of GENERATION_IDS) {
    const [lo, hi] = input.prices[id];
    const [dlo, dhi] = DEFAULT_PRICES[id];
    if (lo !== dlo || hi !== dhi) q.set(`p_${id}`, `${lo}-${hi}`);
  }
  return q.toString();
}

/* Parse a query string (with or without a leading "?" or "#"). Garbage falls back to defaults. */
export function decodeState(str = '') {
  const q = new URLSearchParams(String(str).replace(/^[?#]/, ''));
  const raw = { prices: {} };
  for (const k of SCALAR_KEYS) if (q.has(SHORT[k])) raw[k] = q.get(SHORT[k]);
  for (const id of GENERATION_IDS) {
    const v = q.get(`p_${id}`);
    const m = v && /^(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)$/.exec(v);
    if (m) raw.prices[id] = [m[1], m[2]];
  }
  return normalizeInput(raw);
}

/* ---------------- formatting helpers (shared by UI and tests) ---------------- */

export function fmtTB(tb) {
  if (tb >= 1000) return `${(tb / 1000).toLocaleString('en-US', { maximumFractionDigits: 2 })} PB`;
  return `${tb.toLocaleString('en-US', { maximumFractionDigits: tb < 10 ? 2 : 1 })} TB`;
}

export const fmtInt = (n) => n.toLocaleString('en-US');

export const fmtUSD = (n) =>
  n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
