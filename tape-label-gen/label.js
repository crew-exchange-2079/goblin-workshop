// tape-label-gen browser module: the same Code 39 tables, volser rules and
// sheet layouts as tapelabel.py, rendering SVG pages instead of PDF.
// Pure: no DOM, no network. label.test.js checks the tables against
// tests/golden.json, which the Python tests check against tapelabel.py.

export const CODE39 = {
  0: "nnnwwnwnn", 1: "wnnwnnnnw", 2: "nnwwnnnnw", 3: "wnwwnnnnn",
  4: "nnnwwnnnw", 5: "wnnwwnnnn", 6: "nnwwwnnnn", 7: "nnnwnnwnw",
  8: "wnnwnnwnn", 9: "nnwwnnwnn", A: "wnnnnwnnw", B: "nnwnnwnnw",
  C: "wnwnnwnnn", D: "nnnnwwnnw", E: "wnnnwwnnn", F: "nnwnwwnnn",
  G: "nnnnnwwnw", H: "wnnnnwwnn", I: "nnwnnwwnn", J: "nnnnwwwnn",
  K: "wnnnnnnww", L: "nnwnnnnww", M: "wnwnnnnwn", N: "nnnnwnnww",
  O: "wnnnwnnwn", P: "nnwnwnnwn", Q: "nnnnnnwww", R: "wnnnnnwwn",
  S: "nnwnnnwwn", T: "nnnnwnwwn", U: "wwnnnnnnw", V: "nwwnnnnnw",
  W: "wwwnnnnnn", X: "nwnnwnnnw", Y: "wwnnwnnnn", Z: "nwwnwnnnn",
  "*": "nwnnwnwnn",
};

export const NARROW_MM = 0.423;
export const RATIO = 2.75;
export const QUIET_MIN_MM = 10 * NARROW_MM;
export const BAR_MIN_MM = 11.1;
export const TEXT_BAND_MM = 4.2;
export const TEXT_SIZE_MM = 4.4;
export const PAD_MM = 0.4;

export const MEDIA = {
  L5: "LTO-5", L6: "LTO-6", L7: "LTO-7", L8: "LTO-8", L9: "LTO-9",
  LV: "LTO-5 WORM", LW: "LTO-6 WORM", LX: "LTO-7 WORM",
  LY: "LTO-8 WORM", LZ: "LTO-9 WORM",
  CU: "universal cleaning",
};

export const STOCKS = {
  "letter-2x15": { page_w: 215.9, page_h: 279.4, cols: 2, rows: 15, left: 24.95,
    top: 12.95, pitch_x: 88.0, pitch_y: 16.9, label_w: 78.0, label_h: 16.9 },
  "a4-2x16": { page_w: 210.0, page_h: 297.0, cols: 2, rows: 16, left: 22.0,
    top: 13.3, pitch_x: 88.0, pitch_y: 16.9, label_w: 78.0, label_h: 16.9 },
  single: { page_w: 78.0, page_h: 16.9, cols: 1, rows: 1, left: 0.0,
    top: 0.0, pitch_x: 78.0, pitch_y: 16.9, label_w: 78.0, label_h: 16.9 },
};

const VOLSER = /^[A-Z0-9]{6}$/;

const MOD43 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%";
export const mod43 = (text) => MOD43[[...text].reduce((a, c) => a + MOD43.indexOf(c), 0) % 43];

// LTO labels carry no check character, but about 1 in 43 look as if they do;
// a reader set to strip Code 39 check characters would report 7 chars.
export const looksChecked = (label) => mod43(label.slice(0, 7)) === label[7];

export function modules(text) {
  const out = [];
  [..."*" + text + "*"].forEach((ch, i) => {
    const pat = CODE39[ch];
    if (!pat) throw new Error(`character ${JSON.stringify(ch)} not encodable`);
    if (i) out.push(1);
    for (const e of pat) out.push(e === "w" ? RATIO : 1);
  });
  return out;
}

export function barsMm(text) {
  let x = 0;
  const out = [];
  modules(text).forEach((w, i) => {
    if (i % 2 === 0) out.push([x, w * NARROW_MM]);
    x += w * NARROW_MM;
  });
  return out;
}

export const symbolWidthMm = (text) => modules(text).reduce((a, b) => a + b, 0) * NARROW_MM;

// Throws on an invalid label; returns a list of warnings otherwise.
export function check(label, strict = false) {
  if (label.length !== 8) throw new Error(`${label}: need 6-char volser + 2-char media ID, got ${label.length} chars`);
  const volser = label.slice(0, 6), media = label.slice(6);
  if (!VOLSER.test(volser)) {
    const hint = VOLSER.test(volser.toUpperCase()) ? " (uppercase only)" : "";
    throw new Error(`${label}: volser must be 6 of A-Z 0-9${hint}`);
  }
  if (!(media in MEDIA)) throw new Error(`${label}: unknown media ID ${media}`);
  if (media === "CU" && !volser.startsWith("CLN")) throw new Error(`${label}: cleaning media ID CU needs a CLN-prefixed volser`);
  if (volser.startsWith("CLN") && media !== "CU") throw new Error(`${label}: CLN volsers are treated as cleaning cartridges; use CU`);
  const warns = [];
  const has = (c) => volser.includes(c);
  if ((has("O") && has("0")) || (has("I") && has("1"))) warns.push(`${label}: mixes O/0 or I/1, easy to misread by eye`);
  if (strict && warns.length) throw new Error(warns[0]);
  return warns;
}

export function volserRange(first, count) {
  const m = /^([A-Z0-9]*?)([0-9]+)$/.exec(first);
  if (!VOLSER.test(first) || !m) throw new Error(`${first}: first volser must be 6 chars ending in digits`);
  const [, prefix, digits] = m;
  const start = Number(digits), last = start + count - 1;
  if (!(count >= 1) || last >= 10 ** digits.length) throw new Error(`${first} + ${count}: range overflows ${digits.length} digit(s)`);
  const out = [];
  for (let n = start; n <= last; n++) out.push(prefix + String(n).padStart(digits.length, "0"));
  return out;
}

const r3 = (v) => Math.round(v * 1000) / 1000;

function labelSvg(label, x0, y0, s, outline) {
  const quiet = (s.label_w - symbolWidthMm(label)) / 2;
  const barTop = y0 + PAD_MM + TEXT_BAND_MM;
  const barH = s.label_h - TEXT_BAND_MM - 2 * PAD_MM;
  const rects = barsMm(label).map(([bx, bw]) =>
    `<rect x="${r3(x0 + quiet + bx)}" y="${r3(barTop)}" width="${r3(bw)}" height="${r3(barH)}"/>`).join("");
  const box = outline
    ? `<rect x="${r3(x0)}" y="${r3(y0)}" width="${s.label_w}" height="${s.label_h}" fill="none" stroke="#999" stroke-width="0.07"/>`
    : "";
  const text = `<text x="${r3(x0 + s.label_w / 2)}" y="${r3(barTop - 0.7)}" text-anchor="middle" ` +
    `font-family="Courier New, Courier, monospace" font-weight="bold" font-size="${TEXT_SIZE_MM}">${label}</text>`;
  return box + rects + text;
}

// Every label position, shifted, must still lie wholly on the page; the label
// box holds the quiet zones and bars, so a clipped symbol is refused, not drawn.
export function checkOffsets(s, offsetX, offsetY) {
  if (!Number.isFinite(offsetX) || !Number.isFinite(offsetY)) throw new Error("offsets must be finite numbers");
  const left = s.left + offsetX, top = s.top + offsetY;
  const right = left + (s.cols - 1) * s.pitch_x + s.label_w;
  const bottom = top + (s.rows - 1) * s.pitch_y + s.label_h;
  if (left < -1e-6 || top < -1e-6 || right > s.page_w + 1e-6 || bottom > s.page_h + 1e-6) {
    const g = (v) => String(Math.round(v * 1000) / 1000 + 0);
    throw new Error(`offset ${offsetX},${offsetY} mm moves labels off the page ` +
      `(x allows ${g(-s.left)}..${g(s.page_w - right + offsetX)}, y allows ${g(-s.top)}..${g(s.page_h - bottom + offsetY)})`);
  }
}

// One SVG string per page, sized in mm so a 100% print matches the stock.
export function sheetSvgs(labels, s, { skip = 0, offsetX = 0, offsetY = 0, outline = false } = {}) {
  checkOffsets(s, offsetX, offsetY);
  const per = s.cols * s.rows;
  if (!labels.length) throw new Error("no labels");
  if (!(skip >= 0 && skip < per)) throw new Error(`skip must be 0..${per - 1}`);
  const slots = Array(skip).fill(null).concat(labels);
  const pages = [];
  for (let p = 0; p < slots.length; p += per) {
    const body = slots.slice(p, p + per).map((lab, i) => {
      if (lab === null) return "";
      const r = Math.floor(i / s.cols), c = i % s.cols;
      return labelSvg(lab, s.left + c * s.pitch_x + offsetX, s.top + r * s.pitch_y + offsetY, s, outline);
    }).join("");
    pages.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${s.page_w}mm" height="${s.page_h}mm" ` +
      `viewBox="0 0 ${s.page_w} ${s.page_h}"><rect width="100%" height="100%" fill="#fff"/>` +
      `<g fill="#000">${body}</g></svg>`);
  }
  return pages;
}
