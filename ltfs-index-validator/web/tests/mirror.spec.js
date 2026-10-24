// The STRUCT_* checks mirror Annex B by hand. These cases pin the mirror's
// verdict, and — when LTFS_PYTHON points at an interpreter with lxml — check
// that validate.py (the real XSD) agrees on every one of them.
"use strict";
const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const S = require("./synth");

const REPO = path.resolve(__dirname, "..", "..");
const fixture = (n) => fs.readFileSync(path.join(REPO, "fixtures", n), "utf8");

const ok = S.index({ hi: 3, root: S.dir(1, "root", [S.file(2, "a", 10, { extents: [S.extent("b", 9, 10, 0)] }), S.file(3, "l", 0, { symlink: "a" })]) });

// [name, xml, expected status: VALID | INVALID | UNSUPPORTED]
const CASES = [
  ["fixture valid-empty", fixture("valid-empty.xml"), "VALID"],
  ["fixture valid-nested", fixture("valid-nested.xml"), "VALID"],
  ["fixture valid-reordered", fixture("valid-reordered.xml"), "VALID"],
  ["synthetic ok", ok, "VALID"],
  ["duplicate uid is schema-valid", S.index({ hi: 2, root: S.dir(1, "root", [S.file(2, "a", 0), S.file(2, "b", 0)]) }), "VALID"],
  ["collapsed whitespace in integer", ok.replace("<generationnumber>1<", "<generationnumber> 1 <"), "VALID"],
  ["comment element optional", S.index({ head: "<comment>hello</comment>" }), "VALID"],
  ["policy", S.index({ head: "<dataplacementpolicy><indexpartitioncriteria><name>*.txt</name><size>1024</size></indexpartitioncriteria></dataplacementpolicy>" }), "VALID"],
  ["percentencoded name", ok.replace("<name>a</name>", '<name percentencoded="true">a%2Fb</name>'), "VALID"],
  ["empty xattr value", S.index({ root: S.dir(1, "root", [], { extra: "<extendedattributes><xattr><key>k</key><value/></xattr></extendedattributes>" }) }), "VALID"],
  ["missing creator", ok.replace("<creator>synthetic test</creator>", ""), "INVALID"],
  ["duplicate creator", ok.replace("<creator>", "<creator>x</creator><creator>"), "INVALID"],
  ["bad datetime", ok.replace("<updatetime>2026-01-01T00:00:00.000000000Z", "<updatetime>2026-01-01T00:00:00Z"), "INVALID"],
  ["upper-case partition", ok.replace("<partition>b</partition><startblock>9", "<partition>B</partition><startblock>9"), "INVALID"],
  ["bad uuid", ok.replace("11111111-2222", "zzzzzzzz-2222"), "INVALID"],
  ["unknown element", ok.replace("<allowpolicyupdate>", "<bogus/><allowpolicyupdate>"), "INVALID"],
  ["extentinfo and symlink", ok.replace("<symlink>a</symlink>", "<extentinfo></extentinfo><symlink>a</symlink>"), "INVALID"],
  ["zero bytecount", ok.replace("<bytecount>10</bytecount>", "<bytecount>0</bytecount>"), "INVALID"],
  ["negative length", ok.replace("<length>10</length>", "<length>-1</length>"), "INVALID"],
  ["bad boolean", ok.replace("<allowpolicyupdate>true", "<allowpolicyupdate>yes"), "INVALID"],
  ["lock state with padding", S.index({ head: "<volumelockstate> locked</volumelockstate>" }), "INVALID"],
  ["xattr text value (Annex B quirk)", S.index({ root: S.dir(1, "root", [], { extra: "<extendedattributes><xattr><key>k</key><value>v</value></xattr></extendedattributes>" }) }), "INVALID"],
  ["unknown attribute", S.index({ rootAttrs: ' foo="1"' }), "INVALID"],
  ["unknown element in contents", ok.replace("<contents>", "<contents><junk/>"), "INVALID"],
  ["missing version", ok.replace(' version="2.4.0"', ""), "INVALID"],
  ["malformed version", S.index({ version: "2.4" }), "INVALID"],
  ["doctype", ok.replace("<ltfsindex", "<!DOCTYPE ltfsindex []><ltfsindex"), "INVALID"],
  ["not well-formed", ok.replace("</ltfsindex>", ""), "INVALID"],
  ["other version", S.index({ version: "2.5.0" }), "UNSUPPORTED"],
  ["namespaced root", S.index({ rootAttrs: ' xmlns="urn:x"' }), "UNSUPPORTED"],
  ["namespaced attribute", S.index({ rootAttrs: ' xmlns:x="urn:x" x:y="1"' }), "UNSUPPORTED"],
  ["wrong root", '<?xml version="1.0" encoding="UTF-8"?><ltfslabel version="2.4.0"/>', "UNSUPPORTED"],
  ["latin-1 declaration", ok.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"'), "UNSUPPORTED"],
];

function mirrorStatus(r) {
  if (r.status !== "PARSED") return r.status;
  return r.codes.some((c) => c.startsWith("STRUCT_")) ? "INVALID" : "VALID";
}

test.describe("schema mirror", () => {
  test.beforeEach(async ({ page }) => { await page.goto("index.html"); });

  for (const [name, xml, want] of CASES) {
    test(`${name} → ${want}`, async ({ page }, info) => {
      const r = await page.evaluate((x) => {
        const res = window.LTFS.parse(x);
        return { status: res.status, codes: res.findings.map((f) => f.code) };
      }, xml);
      expect(mirrorStatus(r), JSON.stringify(r.codes)).toBe(want);

      const py = process.env.LTFS_PYTHON;
      if (!py) return;
      const p = info.outputPath("case.xml");
      fs.writeFileSync(p, xml);
      let out;
      try {
        out = execFileSync(py, [path.join(REPO, "validate.py"), "--input", p], { encoding: "utf8" });
      } catch (e) {
        out = e.stdout;
      }
      const cli = JSON.parse(out).status;
      expect(cli === "SCHEMA_VALID_ONLY" ? "VALID" : cli, "validate.py verdict").toBe(want);
    });
  }
});
