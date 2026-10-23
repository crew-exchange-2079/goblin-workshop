import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as L from "./label.js";

const golden = JSON.parse(readFileSync(new URL("./tests/golden.json", import.meta.url), "utf8"));

test("tables match tests/golden.json (and so tapelabel.py)", () => {
  assert.deepEqual(L.CODE39, golden.code39);
  assert.deepEqual(L.MEDIA, golden.media);
  assert.deepEqual(L.STOCKS, golden.stocks);
  for (const [k, v] of Object.entries({
    narrow_mm: L.NARROW_MM, ratio: L.RATIO, quiet_min_mm: L.QUIET_MIN_MM, bar_min_mm: L.BAR_MIN_MM,
    text_band_mm: L.TEXT_BAND_MM, text_size_mm: L.TEXT_SIZE_MM, pad_mm: L.PAD_MM,
  })) assert.equal(v, golden[k], k);
});

test("symbol width", () => {
  assert.ok(Math.abs(L.symbolWidthMm("TAP001L9") - 151.5 * 0.423) < 1e-9);
  assert.equal(L.barsMm("TAP001L9").length, 50);
});

test("check accepts and refuses like tapelabel.check", () => {
  for (const ok of ["TAP001L9", "000000L5", "ZZZZZZLZ", "CLN001CU"]) assert.deepEqual(L.check(ok), []);
  for (const [bad, why] of [["TAP01L9", /6-char/], ["tap001L9", /uppercase only/], ["TAP 01L9", /A-Z 0-9/],
    ["TAP001L4", /unknown media/], ["TAP001CU", /CLN-prefixed/], ["CLN001L9", /cleaning/]]) {
    assert.throws(() => L.check(bad), why, bad);
  }
  assert.equal(L.check("O0AAAAL9").length, 1);
  assert.throws(() => L.check("I1AAAAL9", true));
});

test("mod-43 look-alikes", () => {
  assert.equal(L.mod43("STUVWXL"), "W");
  assert.ok(L.looksChecked("STUVWXLW"));
  assert.ok(!L.looksChecked("TAP001L9"));
  assert.deepEqual(L.check("STUVWXLW", true), []);
});

test("volserRange", () => {
  assert.deepEqual(L.volserRange("TAP098", 3), ["TAP098", "TAP099", "TAP100"]);
  for (const [f, n] of [["TAP998", 3], ["TAPEAA", 1], ["TAP001", 0], ["TAP001", NaN]]) {
    assert.throws(() => L.volserRange(f, n), undefined, `${f} ${n}`);
  }
});

test("sheetSvgs: pages, skip, bar count", () => {
  const labels = L.volserRange("TAP000", 31).map((v) => v + "L9");
  const pages = L.sheetSvgs(labels, L.STOCKS["letter-2x15"], { skip: 1 });
  assert.equal(pages.length, 2);
  assert.equal((pages[0].match(/<text /g) || []).length, 29);
  assert.equal((pages[1].match(/<text /g) || []).length, 2);
  assert.equal((pages[0].match(/<rect x=/g) || []).length, 29 * 50);
  assert.match(pages[0], /width="215.9mm" height="279.4mm"/);
  assert.throws(() => L.sheetSvgs(labels, L.STOCKS.single, { skip: 1 }));
});

test("bar geometry matches what the PDF draws for the same label", () => {
  // Same x positions relative to the label's left edge as tapelabel.label_ops.
  const s = L.STOCKS.single;
  const svg = L.sheetSvgs(["MNOPQRL6"], s)[0];
  const xs = [...svg.matchAll(/<rect x="([\d.]+)" y="[\d.]+" width="([\d.]+)"/g)].map((m) => [+m[1], +m[2]]);
  const quiet = (s.label_w - L.symbolWidthMm("MNOPQRL6")) / 2;
  const want = L.barsMm("MNOPQRL6");
  assert.equal(xs.length, want.length);
  xs.forEach(([x, w], i) => {
    assert.ok(Math.abs(x - (quiet + want[i][0])) < 0.001);
    assert.ok(Math.abs(w - want[i][1]) < 0.001);
  });
});

test("module is pure (no DOM or process access)", () => {
  const src = readFileSync(new URL("./label.js", import.meta.url), "utf8");
  for (const word of ["document", "window", "process", "fetch", "Math.random"]) {
    assert.ok(!new RegExp(`\\b${word.replace(".", "\\.")}\\b`).test(src), word);
  }
});
