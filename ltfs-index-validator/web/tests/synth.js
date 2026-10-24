// Synthetic LTFS 2.4.0 index builder for tests. Everything here is invented.
"use strict";

const TIMES = ["creationtime", "changetime", "modifytime", "accesstime", "backuptime"];
const T = "2026-01-01T00:00:00.000000000Z";
const times = (t = T) => TIMES.map((n) => `<${n}>${t}</${n}>`).join("");

const extent = (p, startblock, bytecount, fileoffset, byteoffset = 0) =>
  `<extent><partition>${p}</partition><startblock>${startblock}</startblock><byteoffset>${byteoffset}</byteoffset>` +
  `<bytecount>${bytecount}</bytecount><fileoffset>${fileoffset}</fileoffset></extent>`;

function file(uid, name, length, { extents = [], symlink = null, extra = "", t = T } = {}) {
  const tail = symlink !== null ? `<symlink>${symlink}</symlink>` : extents.length ? `<extentinfo>${extents.join("")}</extentinfo>` : "";
  return `<file><fileuid>${uid}</fileuid><name>${name}</name><length>${length}</length>${times(t)}<readonly>false</readonly>${extra}${tail}</file>`;
}

function dir(uid, name, children = [], { extra = "" } = {}) {
  return `<directory><fileuid>${uid}</fileuid><name>${name}</name>${times()}<readonly>false</readonly>${extra}<contents>${children.join("")}</contents></directory>`;
}

function index({
  gen = 1, loc = 5, prev = null, hi = 1, root = dir(1, "root"), version = "2.4.0",
  uuid = "11111111-2222-4333-8444-555555555555", updatetime = T, head = "", rootAttrs = "",
} = {}) {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<ltfsindex version="${version}"${rootAttrs}>${head}<creator>synthetic test</creator>` +
    `<volumeuuid>${uuid}</volumeuuid><generationnumber>${gen}</generationnumber><updatetime>${updatetime}</updatetime>` +
    `<location><partition>b</partition><startblock>${loc}</startblock></location>` +
    (prev !== null ? `<previousgenerationlocation><partition>b</partition><startblock>${prev}</startblock></previousgenerationlocation>` : "") +
    `<allowpolicyupdate>true</allowpolicyupdate><highestfileuid>${hi}</highestfileuid>${root}</ltfsindex>\n`;
}

module.exports = { index, dir, file, extent, times, T };
