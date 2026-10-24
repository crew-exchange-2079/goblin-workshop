"use strict";
const { test, expect } = require("@playwright/test");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const S = require("./synth");

const REPO = path.resolve(__dirname, "..", "..");
const nested = fs.readFileSync(path.join(REPO, "fixtures", "valid-nested.xml"));
const xmlFile = (name, body) => ({ name, mimeType: "application/xml", buffer: Buffer.isBuffer(body) ? body : Buffer.from(body) });
const load = (page, ...files) => page.setInputFiles("#file", files);

test.beforeEach(async ({ page }) => {
  await page.goto("index.html");
});

test("starts empty and offers a drop zone", async ({ page }) => {
  await expect(page.locator("#drop")).toBeVisible();
  await expect(page.locator("#workspace")).toBeHidden();
  await expect(page.locator("#clear")).toBeDisabled();
  await page.locator("#sample").click();
  await expect(page.locator("#workspace")).toBeVisible();
  await page.locator("#clear").click();
  await expect(page.locator("#workspace")).toBeHidden();
  await expect(page.locator("#loaded")).toBeHidden();
});

test("shows summary, tree, and extents for a valid index", async ({ page }) => {
  await load(page, xmlFile("valid-nested.xml", nested));
  const summary = page.locator("#summary");
  await expect(summary).toContainText("00000000-0000-4000-8000-000000000000");
  await expect(summary).toContainText("b:5");
  await expect(summary).toContainText("2 dirs · 1 files · 1 symlinks");
  await expect(page.locator("#verdict")).toContainText("no structural issues");

  const tree = page.locator("#tree");
  await expect(tree.getByRole("button", { name: "/ (root)" })).toBeVisible();
  await expect(tree.locator('li[data-path="/link"] .tsize')).toHaveText("→ sub/a.bin");
  await tree.locator('li[data-path="/sub"] > .trow > .tog').click();
  await tree.getByRole("button", { name: "a.bin" }).click();

  const detail = page.locator("#detail");
  await expect(detail).toContainText("/sub/a.bin");
  await expect(detail).toContainText("612 B");
  await expect(detail.locator("table.grid tbody tr")).toHaveCount(2);
  await expect(detail.locator(".emap .eseg")).toHaveCount(2);
  await expect(detail.locator("table.grid tbody tr").nth(1)).toContainText("512");

  await tree.locator('li[data-path="/sub"] > .trow > .tog').click();
  await expect(tree.locator('li[data-path="/sub/a.bin"]')).toHaveCount(0);
});

test("accepts files dropped onto the page", async ({ page }) => {
  const xml = nested.toString("utf8");
  const dt = await page.evaluateHandle((text) => {
    const d = new DataTransfer();
    d.items.add(new File([text], "dropped.xml", { type: "application/xml" }));
    return d;
  }, xml);
  await page.dispatchEvent("#drop", "dragover", { dataTransfer: dt });
  await expect(page.locator("body")).toHaveClass(/dragging/);
  await page.dispatchEvent("#drop", "drop", { dataTransfer: dt });
  await expect(page.locator("#gens")).toContainText("dropped.xml");
  await expect(page.locator("body")).not.toHaveClass(/dragging/);
});

test("lists semantic findings and jumps to the entry", async ({ page }) => {
  const root = S.dir(1, "root", [
    S.dir(2, "d", [S.file(3, "x", 100, { extents: [S.extent("b", 10, 60, 0), S.extent("b", 11, 60, 50)] })]),
    S.file(3, "dup", 0),
    S.file(9, "toohigh", 5, { extents: [S.extent("b", 12, 5, 0)] }),
  ]);
  await load(page, xmlFile("faults.xml", S.index({ hi: 4, root })));
  const findings = page.locator("#findings");
  for (const code of ["SEM_UID_DUPLICATE", "SEM_UID_ABOVE_HIGHEST", "SEM_EXTENT_OVERLAP", "SEM_EXTENT_BEYOND_LENGTH"]) {
    await expect(findings.locator(`li[data-code="${code}"]`)).toHaveCount(1);
  }
  await expect(page.locator("#verdict")).toContainText("no structural issues");

  await findings.locator('li[data-code="SEM_EXTENT_OVERLAP"] .pathlink').click();
  await expect(page.locator("#tree .trow.selected")).toContainText("x");
  await expect(page.locator("#detail")).toContainText("/d/x");
  await expect(page.locator("#detail .finding")).toHaveCount(2); // overlap + beyond length (110 > 100)
  await expect(page.locator("#detail .emap .eseg.over")).toHaveCount(1);

  await page.locator('#sevfilter button[data-sev="error"]').click();
  await expect(findings.locator("li.finding.sev-error")).toHaveCount(0);
  await expect(page.locator('#sevfilter button[data-sev="error"]')).toHaveAttribute("aria-pressed", "false");
});

test("reads several indexes as generations and diffs them", async ({ page }) => {
  await page.locator("#sample").click();
  const gens = page.locator("#gens .gen");
  await expect(gens).toHaveCount(3);
  await expect(gens.nth(2)).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#summary")).toContainText("b:40"); // previous location of gen 3
  const diff = page.locator("#diff");
  await expect(page.locator("#diffpanel")).toBeVisible();
  await expect(diff).toContainText("gen 2 (sample-gen2.xml) → gen 3 (sample-gen3.xml)");
  await expect(diff.locator(".d-added")).toContainText("/logs/2026-01-03.log");
  await expect(diff.locator(".d-removed")).toContainText("/sparse.img");
  await expect(diff.locator(".d-changed li").first()).toHaveText("~/ modifytime"); // root dir, not "//"
  await expect(page.locator('#findings li[data-code="SEM_UID_DUPLICATE"]')).toHaveCount(1);
  await expect(page.locator('#findings li[data-code="SEM_TAPE_OVERLAP"]')).toHaveCount(1);
  await expect(page.locator('#findings li[data-code^="GEN_"]')).toHaveCount(0);

  await gens.nth(0).click();
  await expect(page.locator("#diffpanel")).toBeHidden();
  await expect(page.locator("#findings")).toContainText("No findings.");

  await gens.nth(1).click();
  await expect(diff.locator(".d-changed")).toContainText("/README");
  await expect(diff.locator(".d-changed")).toContainText("length 42→64");
  await expect(page.locator('#findings li[data-code="SEM_EXTENT_SPARSE"]')).toContainText("/sparse.img");
});

test("flags broken generation chains and mixed volumes", async ({ page }) => {
  const g1 = S.index({ gen: 1, loc: 10 });
  const g2 = S.index({ gen: 2, loc: 20, prev: 999, updatetime: "2025-12-31T00:00:00.000000000Z" });
  const other = S.index({ gen: 3, loc: 30, prev: 20, uuid: "99999999-2222-4333-8444-555555555555" });
  await load(page, xmlFile("g2.xml", g2), xmlFile("g1.xml", g1), xmlFile("other.xml", other));
  await expect(page.locator("#gens .gen .gen-no")).toHaveText(["gen 1", "gen 2", "gen 3"]);
  await page.locator("#gens .gen").nth(1).click();
  const f = page.locator("#findings");
  await expect(f.locator('li[data-code="GEN_CHAIN_BROKEN"]')).toHaveCount(1);
  await expect(f.locator('li[data-code="GEN_TIME_REGRESSION"]')).toHaveCount(1);
  await expect(f.locator('li[data-code="GEN_VOLUME_MISMATCH"]')).toHaveCount(1);
  await expect(f.locator('li[data-code="GEN_CHAIN_BROKEN"]')).toContainText("across generations");
});

test("treats highestfileuid 0 as the exhausted-UID sentinel, not a bound", async ({ page }) => {
  const root = S.dir(1, "root", [S.file(7, "a", 0)]);
  const g1 = S.index({ gen: 1, loc: 10, hi: 7, root });
  const g2 = S.index({ gen: 2, loc: 20, prev: 10, hi: 0, root });
  await load(page, xmlFile("g1.xml", g1), xmlFile("g2.xml", g2));
  const f = page.locator("#findings");
  await expect(f.locator('li[data-code="SEM_UID_SPACE_EXHAUSTED"]')).toHaveCount(1);
  await expect(f.locator('li[data-code="SEM_UID_ABOVE_HIGHEST"]')).toHaveCount(0);
  await expect(f.locator('li[data-code="SEM_HIGHEST_UID_UNUSED"]')).toHaveCount(0);
  await expect(f.locator('li[data-code="GEN_UID_REGRESSION"]')).toHaveCount(0);
  // A real decrease is still flagged.
  await page.locator("#clear").click();
  await load(page, xmlFile("g1.xml", g1), xmlFile("g2.xml", S.index({ gen: 2, loc: 20, prev: 10, hi: 8, root: S.dir(1, "root", [S.file(8, "a", 0)]) })),
    xmlFile("g3.xml", S.index({ gen: 3, loc: 30, prev: 20, hi: 7, root })));
  await expect(f.locator('li[data-code="GEN_UID_REGRESSION"]')).toHaveCount(1);
});

test("treats same-generation copies on two partitions as copies", async ({ page }) => {
  const a = S.index({ gen: 4, loc: 100, prev: 50 }).replace("<partition>b</partition><startblock>100", "<partition>a</partition><startblock>100");
  const b = S.index({ gen: 4, loc: 100, prev: 50 });
  await load(page, xmlFile("ip.xml", a), xmlFile("dp.xml", b));
  await expect(page.locator('#findings li[data-code="GEN_COPY"]')).toHaveCount(1);
  await expect(page.locator('#findings li[data-code="GEN_CONFLICT"]')).toHaveCount(0);
});

test("block size drives the cross-file tape overlap check", async ({ page }) => {
  const root = S.dir(1, "root", [
    S.file(2, "big", 600000, { extents: [S.extent("b", 10, 600000, 0)] }),
    S.file(3, "next", 10, { extents: [S.extent("b", 11, 10, 0)] }),
  ]);
  await load(page, xmlFile("tape.xml", S.index({ hi: 3, root })));
  const overlap = page.locator('#findings li[data-code="SEM_TAPE_OVERLAP"]');
  await expect(overlap).toHaveCount(1);
  await expect(overlap).toContainText("524288-byte blocks");
  await page.locator("#blocksize").fill("1048576");
  await page.locator("#blocksize").dispatchEvent("change");
  await expect(overlap).toHaveCount(0);
});

test("refuses DTDs and non-UTF-8 input without reading a tree", async ({ page }) => {
  const dtd = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ltfsindex version="2.4.0">&a;</ltfsindex>';
  await load(page, xmlFile("dtd.xml", dtd), xmlFile("latin1.xml", Buffer.from([0x3c, 0x61, 0xe9, 0x3e])));
  await page.locator("#gens .gen", { hasText: "dtd.xml" }).click();
  await expect(page.locator('#findings li[data-code="DTD_FORBIDDEN"]')).toHaveCount(1);
  await expect(page.locator("#verdict")).toContainText("Refused (INVALID)");
  await expect(page.locator("#tree")).toContainText("No tree to show.");
  await page.locator("#gens .gen", { hasText: "latin1.xml" }).click();
  await expect(page.locator('#findings li[data-code="ENCODING_NOT_UTF8"]')).toHaveCount(1);
  await expect(page.locator("#verdict")).toContainText("Refused (UNSUPPORTED)");
});

test("renders hostile names as text", async ({ page }) => {
  const evil = "&lt;img src=x onerror=&quot;window.pwned=1&quot;&gt;";
  const root = S.dir(1, "root", [S.file(2, evil, 0, { extra: `<extendedattributes><xattr><key>${evil}</key><value/></xattr></extendedattributes>` })]);
  await load(page, xmlFile("evil.xml", S.index({ hi: 2, root })));
  await page.locator("#tree .tname", { hasText: "<img" }).click();
  await expect(page.locator("#detail")).toContainText('<img src=x onerror="window.pwned=1">');
  await expect(page.locator("main img")).toHaveCount(0);
  expect(await page.evaluate(() => window.pwned)).toBeUndefined();
});

test("filters the tree and pages large directories", async ({ page }) => {
  const kids = [];
  for (let i = 0; i < 1200; i++) kids.push(S.file(i + 2, `f${String(i).padStart(4, "0")}`, 0));
  await load(page, xmlFile("big.xml", S.index({ hi: 1201, root: S.dir(1, "root", kids) })));
  const items = page.locator("#tree > li > ul > li.titem");
  await expect(items).toHaveCount(500);
  await page.locator("#tree .more button").click();
  await expect(items).toHaveCount(1000);
  await page.locator("#filter").fill("f1199");
  await expect(page.locator("#tree li.titem")).toHaveCount(1);
  await expect(page.locator("#tree")).toContainText("/f1199");
});

test("never touches the network", async ({ page }) => {
  const requests = [];
  page.on("request", (r) => requests.push(r.url()));
  await load(page, xmlFile("valid-nested.xml", nested));
  await page.locator("#sample").click();
  await expect(page.locator("#gens .gen")).toHaveCount(4);
  expect(requests).toEqual([]);
  // The CSP forbids connections outright, same origin included.
  const fetched = await page.evaluate(() => fetch("index.html").then(() => "allowed", () => "blocked"));
  expect(fetched).toBe("blocked");
  const csp = await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute("content");
  expect(csp).toContain("connect-src 'none'");
});

test("works when opened straight from disk", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(pathToFileURL(path.join(__dirname, "..", "index.html")).href);
  await page.locator("#sample").click();
  await expect(page.locator("#gens .gen")).toHaveCount(3);
  await expect(page.locator("#tree")).toContainText("logs");
  expect(errors).toEqual([]);
});

test("fits a phone-width screen", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await page.locator("#sample").click();
  await page.locator("#tree .tname", { hasText: "README" }).click();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
