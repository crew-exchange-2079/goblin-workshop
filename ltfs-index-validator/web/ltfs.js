/* ltfs.js — parse and check LTFS 2.4.0 index XML entirely in the browser.
 *
 * Two kinds of findings:
 *  - STRUCT_*: a hand-written mirror of the Annex B schema rules. It is NOT XSD
 *    validation; validate.py (lxml + the pinned XSD) is the authority.
 *  - SEM_* / GEN_*: semantic checks the schema cannot express (UID uniqueness,
 *    extent overlap and coverage, generation chains). Heuristic, not compliance.
 *
 * Classic script (not a module) so index.html also works from file://.
 * Nothing here touches the network or the DOM outside DOMParser.
 */
(function (global) {
  "use strict";

  var MAX_BYTES = 64 * 1024 * 1024;
  var MAX_DEPTH = 64;
  var MAX_ELEMENTS = 5000000;
  var DEFAULT_BLOCKSIZE = 524288;
  var SUPPORTED_VERSION = "2.4.0";

  var RE = {
    nonNeg: /^(\+?[0-9]+|-0+)$/,
    pos: /^\+?0*[1-9][0-9]*$/,
    bool: /^(true|false|1|0)$/,
    datetime: /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{9}Z$/,
    partition: /^[a-z]$/,
    uuid: /^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$/,
    version: /^[0-9]+\.[0-9]+\.[0-9]+$/,
    lock: /^(unlocked|locked|permlocked)$/,
  };

  var TIMES = ["creationtime", "changetime", "modifytime", "accesstime", "backuptime"];

  // Element content models from Annex B. "all" = xs:all (each child at most once,
  // any order); value is [type, required].
  var MODELS = {
    ltfsindex: {
      creator: ["string", true], comment: ["string", false], volumeuuid: ["uuid", true],
      generationnumber: ["nonNeg", true], updatetime: ["datetime", true],
      location: ["tapeposition", true], previousgenerationlocation: ["tapeposition", false],
      allowpolicyupdate: ["bool", true], dataplacementpolicy: ["policy", false],
      volumelockstate: ["lock", false], highestfileuid: ["nonNeg", true],
      directory: ["directory", true],
    },
    directory: {
      fileuid: ["nonNeg", true], name: ["name", true], creationtime: ["datetime", true],
      changetime: ["datetime", true], modifytime: ["datetime", true],
      accesstime: ["datetime", true], backuptime: ["datetime", true],
      readonly: ["bool", true], extendedattributes: ["xattrs", false],
      contents: ["contents", true],
    },
    file: {
      fileuid: ["nonNeg", true], name: ["name", true], length: ["nonNeg", true],
      creationtime: ["datetime", true], changetime: ["datetime", true],
      modifytime: ["datetime", true], accesstime: ["datetime", true],
      backuptime: ["datetime", true], readonly: ["bool", true],
      extendedattributes: ["xattrs", false], openforwrite: ["bool", false],
      extentinfo: ["extentinfo", false], symlink: ["name", false],
    },
    tapeposition: { partition: ["partition", true], startblock: ["nonNeg", true] },
    extent: {
      partition: ["partition", true], startblock: ["nonNeg", true],
      byteoffset: ["nonNeg", true], bytecount: ["pos", true], fileoffset: ["nonNeg", true],
    },
    xattr: { key: ["name", true], value: ["xvalue", true] },
  };

  function Ctx(sourceName) {
    this.findings = [];
    this.source = sourceName || "";
  }
  Ctx.prototype.add = function (severity, code, message, path) {
    this.findings.push({ severity: severity, code: code, message: message, path: path || "/" });
  };

  function elementChildren(el) {
    var out = [];
    for (var c = el.firstElementChild; c; c = c.nextElementSibling) out.push(c);
    return out;
  }

  function directText(el) {
    var s = "";
    for (var n = el.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3 || n.nodeType === 4) s += n.nodeValue;
    }
    return s;
  }

  function big(s) {
    try { return BigInt(String(s).trim().replace(/^\+/, "")); } catch (e) { return null; }
  }

  function checkAttrs(ctx, el, allowed, where) {
    for (var i = 0; i < el.attributes.length; i++) {
      var a = el.attributes[i];
      if (a.name === "xmlns" || a.prefix === "xmlns") continue;
      if (allowed.indexOf(a.name) < 0) {
        ctx.add("error", "STRUCT_ATTRIBUTE", "Attribute “" + a.name + "” is not allowed on <" + el.localName + ">.", where);
      }
    }
  }

  function checkSimple(ctx, el, type, where) {
    var v = directText(el);
    var t = v.trim(); // xs whitespace collapse for the numeric/boolean/pattern types used here
    if (elementChildren(el).length) {
      ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<" + el.localName + "> must contain text only.", where);
      return t;
    }
    var ok = true;
    if (type === "nonNeg") ok = RE.nonNeg.test(t);
    else if (type === "pos") ok = RE.pos.test(t);
    else if (type === "bool") ok = RE.bool.test(t);
    else if (type === "datetime") ok = RE.datetime.test(v);
    else if (type === "partition") ok = RE.partition.test(v);
    else if (type === "uuid") ok = RE.uuid.test(v);
    else if (type === "lock") ok = RE.lock.test(v);
    if (!ok) {
      ctx.add("error", "STRUCT_VALUE", "<" + el.localName + "> value “" + clip(v) + "” is not a valid " + type + ".", where);
    } else if (type === "datetime" && !datetimeInRange(v)) {
      ctx.add("warning", "SEM_DATETIME_RANGE", "<" + el.localName + "> matches the schema pattern but is not a real UTC time: " + v + ".", where);
    }
    return type === "datetime" || type === "partition" || type === "uuid" || type === "lock" ? v : t;
  }

  function datetimeInRange(v) {
    var y = +v.slice(0, 4), mo = +v.slice(5, 7), d = +v.slice(8, 10);
    var h = +v.slice(11, 13), mi = +v.slice(14, 16), s = +v.slice(17, 19);
    if (mo < 1 || mo > 12 || h > 23 || mi > 59 || s > 60) return false;
    var dim = new Date(Date.UTC(y, mo, 0)).getUTCDate();
    return d >= 1 && d <= dim;
  }

  function clip(s) {
    s = String(s);
    return s.length > 80 ? s.slice(0, 77) + "…" : s;
  }

  function parseBool(t) { return t === "true" || t === "1"; }

  // Generic xs:all walker: returns {name: element}; reports unknown/duplicate/missing.
  function walkAll(ctx, el, model, where) {
    var seen = {};
    elementChildren(el).forEach(function (c) {
      var n = c.localName;
      if (!Object.prototype.hasOwnProperty.call(model, n)) {
        ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Unexpected element <" + n + "> inside <" + el.localName + ">.", where);
        return;
      }
      if (seen[n]) {
        ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<" + n + "> appears more than once inside <" + el.localName + ">.", where);
        return;
      }
      seen[n] = c;
    });
    Object.keys(model).forEach(function (n) {
      if (model[n][1] && !seen[n]) {
        ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Missing required <" + n + "> inside <" + el.localName + ">.", where);
      }
    });
    return seen;
  }

  function readName(ctx, el, where) {
    checkAttrs(ctx, el, ["percentencoded"], where);
    var pe = false;
    var raw = el.getAttribute("percentencoded");
    if (raw !== null) {
      if (!RE.bool.test(raw.trim())) ctx.add("error", "STRUCT_VALUE", "percentencoded=“" + clip(raw) + "” is not a boolean.", where);
      else pe = parseBool(raw.trim());
    }
    if (elementChildren(el).length) ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<" + el.localName + "> must contain text only.", where);
    var text = directText(el);
    var decoded = text;
    if (pe) {
      try { decoded = decodeURIComponent(text); } catch (e) {
        ctx.add("error", "SEM_NAME_ENCODING", "Percent-encoded name “" + clip(text) + "” does not decode.", where);
      }
    }
    return { raw: text, value: decoded, percentEncoded: pe };
  }

  function readPosition(ctx, el, where) {
    checkAttrs(ctx, el, [], where);
    var k = walkAll(ctx, el, MODELS.tapeposition, where);
    return {
      partition: k.partition ? checkSimple(ctx, k.partition, "partition", where) : null,
      startblock: k.startblock ? big(checkSimple(ctx, k.startblock, "nonNeg", where)) : null,
    };
  }

  function readXattrs(ctx, el, where) {
    checkAttrs(ctx, el, [], where);
    var out = [];
    elementChildren(el).forEach(function (x) {
      if (x.localName !== "xattr") {
        ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Unexpected element <" + x.localName + "> inside <extendedattributes>.", where);
        return;
      }
      checkAttrs(ctx, x, [], where);
      var k = walkAll(ctx, x, MODELS.xattr, where);
      var item = { key: k.key ? readName(ctx, k.key, where).value : "", type: "text", value: "" };
      if (k.value) {
        checkAttrs(ctx, k.value, ["type"], where);
        var ty = k.value.getAttribute("type");
        if (ty !== null) {
          if (ty.trim() !== "text" && ty.trim() !== "base64") ctx.add("error", "STRUCT_VALUE", "xattr value type=“" + clip(ty) + "” must be text or base64.", where);
          else item.type = ty.trim();
        }
        item.value = directText(k.value);
        if (item.value.length || elementChildren(k.value).length) {
          // Annex B as printed gives <value> an attribute-only complex type (empty
          // content), so lxml rejects any text; real indexes carry text here.
          ctx.add("error", "STRUCT_XATTR_VALUE_CONTENT", "xattr “" + clip(item.key) + "” has a text value; Annex B as printed declares <value> empty, so validate.py reports INVALID.", where);
        }
      }
      out.push(item);
    });
    return out;
  }

  function readPolicy(ctx, el, where) {
    checkAttrs(ctx, el, [], where);
    var kids = elementChildren(el);
    var policy = { names: [], size: null };
    if (kids.length !== 1 || kids[0].localName !== "indexpartitioncriteria") {
      ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<dataplacementpolicy> must contain exactly one <indexpartitioncriteria>.", where);
      return policy;
    }
    var crit = kids[0];
    checkAttrs(ctx, crit, [], where);
    var sawSize = false;
    elementChildren(crit).forEach(function (c) {
      if (c.localName === "name" && !sawSize) policy.names.push(readName(ctx, c, where).value);
      else if (c.localName === "size" && !sawSize) { sawSize = true; policy.size = big(checkSimple(ctx, c, "nonNeg", where)); }
      else ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<indexpartitioncriteria> expects name* then size.", where);
    });
    if (!sawSize) ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Missing required <size> inside <indexpartitioncriteria>.", where);
    return policy;
  }

  function readNode(ctx, el, parentPath) {
    var kind = el.localName === "directory" ? "dir" : "file";
    var model = MODELS[el.localName];
    var provisional = parentPath === null ? "/" : (parentPath === "/" ? "" : parentPath) + "/?";
    checkAttrs(ctx, el, [], provisional);
    var k = walkAll(ctx, el, model, provisional);
    var name = k.name ? readName(ctx, k.name, provisional) : { raw: "", value: "", percentEncoded: false };
    var path = parentPath === null ? "/" : (parentPath === "/" ? "/" : parentPath + "/") + name.value;
    var node = {
      kind: kind, name: name.value, rawName: name.raw, percentEncoded: name.percentEncoded,
      path: path, uid: null, times: {}, readonly: null, xattrs: [], children: [],
      length: null, extents: [], symlink: null, openforwrite: false,
    };
    if (k.fileuid) node.uid = big(checkSimple(ctx, k.fileuid, "nonNeg", path));
    TIMES.forEach(function (t) { if (k[t]) node.times[t] = checkSimple(ctx, k[t], "datetime", path); });
    if (k.readonly) node.readonly = parseBool(checkSimple(ctx, k.readonly, "bool", path));
    if (k.extendedattributes) node.xattrs = readXattrs(ctx, k.extendedattributes, path);
    if (kind === "file") {
      if (k.length) node.length = big(checkSimple(ctx, k.length, "nonNeg", path));
      if (k.openforwrite) node.openforwrite = parseBool(checkSimple(ctx, k.openforwrite, "bool", path));
      if (k.extentinfo && k.symlink) {
        ctx.add("error", "STRUCT_ELEMENT_CONTENT", "A file may carry <extentinfo> or <symlink>, not both.", path);
      }
      if (k.symlink) node.symlink = readName(ctx, k.symlink, path).value;
      if (k.extentinfo) {
        checkAttrs(ctx, k.extentinfo, [], path);
        elementChildren(k.extentinfo).forEach(function (x) {
          if (x.localName !== "extent") {
            ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Unexpected element <" + x.localName + "> inside <extentinfo>.", path);
            return;
          }
          checkAttrs(ctx, x, [], path);
          var e = walkAll(ctx, x, MODELS.extent, path);
          node.extents.push({
            partition: e.partition ? checkSimple(ctx, e.partition, "partition", path) : null,
            startblock: e.startblock ? big(checkSimple(ctx, e.startblock, "nonNeg", path)) : null,
            byteoffset: e.byteoffset ? big(checkSimple(ctx, e.byteoffset, "nonNeg", path)) : null,
            bytecount: e.bytecount ? big(checkSimple(ctx, e.bytecount, "pos", path)) : null,
            fileoffset: e.fileoffset ? big(checkSimple(ctx, e.fileoffset, "nonNeg", path)) : null,
          });
        });
      }
    } else if (k.contents) {
      checkAttrs(ctx, k.contents, [], path);
      elementChildren(k.contents).forEach(function (c) {
        if (c.localName === "directory" || c.localName === "file") {
          node.children.push(readNode(ctx, c, path));
        } else {
          ctx.add("error", "STRUCT_ELEMENT_CONTENT", "Unexpected element <" + c.localName + "> inside <contents>.", path);
        }
      });
    }
    return node;
  }

  function limits(root) {
    var count = 0, stack = [[root, 1]];
    while (stack.length) {
      var top = stack.pop();
      if (++count > MAX_ELEMENTS) return "LIMIT_ELEMENTS_EXCEEDED";
      if (top[1] > MAX_DEPTH) return "LIMIT_DEPTH_EXCEEDED";
      for (var c = top[0].firstElementChild; c; c = c.nextElementSibling) stack.push([c, top[1] + 1]);
    }
    return null;
  }

  function hasNamespaces(root) {
    var stack = [root];
    while (stack.length) {
      var el = stack.pop();
      if (el.namespaceURI) return "NAMESPACE_UNSUPPORTED";
      for (var i = 0; i < el.attributes.length; i++) {
        var a = el.attributes[i];
        if (a.name === "xmlns" || a.prefix === "xmlns") continue; // declarations alone are harmless
        if (a.prefix) return "NAMESPACED_ATTRIBUTE_UNSUPPORTED";
      }
      for (var c = el.firstElementChild; c; c = c.nextElementSibling) stack.push(c);
    }
    return null;
  }

  function refuse(ctx, status, code, message) {
    ctx.add("fatal", code, message, "/");
    return { status: status, index: null, findings: ctx.findings };
  }

  /* parse(text, opts) -> {status, index, findings}
   * status: "PARSED" | "INVALID" | "UNSUPPORTED" (the latter two carry no index). */
  function parse(text, opts) {
    opts = opts || {};
    var ctx = new Ctx(opts.name);
    if (typeof text !== "string") return refuse(ctx, "INVALID", "INPUT_NOT_TEXT", "Input is not text.");
    if (text.length > MAX_BYTES) return refuse(ctx, "INVALID", "INPUT_TOO_LARGE", "Input exceeds " + (MAX_BYTES >> 20) + " MiB.");
    var enc = /^﻿?\s*<\?xml[^>]*?encoding\s*=\s*["']([^"']*)["']/.exec(text);
    if (enc && ["UTF-8", "UTF8"].indexOf(enc[1].trim().toUpperCase().replace(/_/g, "-")) < 0) {
      return refuse(ctx, "UNSUPPORTED", "ENCODING_NOT_UTF8", "Only UTF-8 indexes are supported.");
    }
    if (text.indexOf("\u0000") >= 0) return refuse(ctx, "UNSUPPORTED", "ENCODING_NOT_UTF8", "Input contains NUL bytes.");
    if (text.indexOf("<!DOCTYPE") >= 0 || text.indexOf("<!ENTITY") >= 0) {
      return refuse(ctx, "INVALID", "DTD_FORBIDDEN", "DOCTYPE and entity declarations are refused.");
    }
    var doc;
    try {
      doc = new global.DOMParser().parseFromString(text, "application/xml");
    } catch (e) {
      return refuse(ctx, "INVALID", "XML_PARSE_FAILED", "The XML parser failed.");
    }
    var root = doc.documentElement;
    if (!root || doc.getElementsByTagName("parsererror").length) {
      return refuse(ctx, "INVALID", "XML_MALFORMED", "Not well-formed XML.");
    }
    var lim = limits(root);
    if (lim) return refuse(ctx, "INVALID", lim, lim === "LIMIT_DEPTH_EXCEEDED" ? "Nesting deeper than " + MAX_DEPTH + "." : "More than " + MAX_ELEMENTS + " elements.");
    var ns = hasNamespaces(root);
    if (ns) return refuse(ctx, "UNSUPPORTED", ns, "Namespaced elements or attributes are not supported (unnamespaced LTFS 2.4.0 only).");
    if (root.localName !== "ltfsindex") return refuse(ctx, "UNSUPPORTED", "ROOT_NOT_FULL_INDEX", "Root element is <" + root.localName + ">, not <ltfsindex>.");
    var version = root.getAttribute("version");
    if (version === null) return refuse(ctx, "INVALID", "VERSION_MISSING", "<ltfsindex> has no version attribute.");
    if (!RE.version.test(version)) return refuse(ctx, "INVALID", "VERSION_MALFORMED", "Version “" + clip(version) + "” is malformed.");
    if (version !== SUPPORTED_VERSION) return refuse(ctx, "UNSUPPORTED", "VERSION_UNSUPPORTED", "Version " + version + " is not supported (2.4.0 only).");

    checkAttrs(ctx, root, ["version"], "/");
    var k = walkAll(ctx, root, MODELS.ltfsindex, "/");
    var idx = {
      name: opts.name || "", version: version,
      creator: k.creator ? directText(k.creator) : null,
      comment: k.comment ? directText(k.comment) : null,
      volumeuuid: k.volumeuuid ? checkSimple(ctx, k.volumeuuid, "uuid", "/") : null,
      generation: k.generationnumber ? big(checkSimple(ctx, k.generationnumber, "nonNeg", "/")) : null,
      updatetime: k.updatetime ? checkSimple(ctx, k.updatetime, "datetime", "/") : null,
      location: k.location ? readPosition(ctx, k.location, "/") : null,
      prevLocation: k.previousgenerationlocation ? readPosition(ctx, k.previousgenerationlocation, "/") : null,
      allowpolicyupdate: k.allowpolicyupdate ? parseBool(checkSimple(ctx, k.allowpolicyupdate, "bool", "/")) : null,
      policy: k.dataplacementpolicy ? readPolicy(ctx, k.dataplacementpolicy, "/") : null,
      volumelockstate: k.volumelockstate ? checkSimple(ctx, k.volumelockstate, "lock", "/") : null,
      highestfileuid: k.highestfileuid ? big(checkSimple(ctx, k.highestfileuid, "nonNeg", "/")) : null,
      root: null,
      stats: null,
    };
    [k.creator, k.comment].forEach(function (el) {
      if (el && elementChildren(el).length) ctx.add("error", "STRUCT_ELEMENT_CONTENT", "<" + el.localName + "> must contain text only.", "/");
    });
    if (k.directory) idx.root = readNode(ctx, k.directory, null);
    semantic(ctx, idx, opts.blocksize || DEFAULT_BLOCKSIZE);
    return { status: "PARSED", index: idx, findings: ctx.findings };
  }

  function walk(node, fn) {
    var stack = [node];
    while (stack.length) {
      var n = stack.pop();
      fn(n);
      for (var i = n.children.length - 1; i >= 0; i--) stack.push(n.children[i]);
    }
  }

  function semantic(ctx, idx, blocksize) {
    var stats = { dirs: 0, files: 0, symlinks: 0, extents: 0, bytes: 0n, sparseFiles: 0 };
    idx.stats = stats;
    if (idx.location && idx.prevLocation && idx.location.partition === idx.prevLocation.partition &&
        idx.location.startblock !== null && idx.location.startblock === idx.prevLocation.startblock) {
      ctx.add("error", "SEM_LOCATION_SELF", "previousgenerationlocation equals location (" + fmtPos(idx.location) + ").", "/");
    }
    if (idx.generation !== null && idx.generation > 1n && !idx.prevLocation) {
      ctx.add("info", "SEM_PREV_LOCATION_ABSENT", "Generation " + idx.generation + " has no previousgenerationlocation; older generations cannot be chained.", "/");
    }
    if (!idx.root) return;
    if (idx.root.uid !== null && idx.root.uid !== 1n) {
      ctx.add("warning", "SEM_ROOT_UID", "Root directory fileuid is " + idx.root.uid + "; LTFS assigns 1 to the root.", "/");
    }
    // highestfileuid 0 is the "UIDs exhausted" sentinel (LTFS 2.4 \u00a79.2.2), not an upper bound.
    var exhausted = idx.highestfileuid === 0n;
    if (exhausted) {
      ctx.add("info", "SEM_UID_SPACE_EXHAUSTED", "highestfileuid is 0: the volume's UID space is marked exhausted, so fileuid values are not checked against it.", "/");
    }
    var uids = new Map();
    var bytsz = BigInt(blocksize);
    var tape = []; // [partition, start, end, path]
    walk(idx.root, function (n) {
      if (n.kind === "dir") stats.dirs++;
      else if (n.symlink !== null) stats.symlinks++;
      else stats.files++;
      if (n.uid !== null) {
        if (uids.has(n.uid)) {
          ctx.add("error", "SEM_UID_DUPLICATE", "fileuid " + n.uid + " is also used by " + uids.get(n.uid) + ".", n.path);
        } else uids.set(n.uid, n.path);
        if (idx.highestfileuid !== null && !exhausted && n.uid > idx.highestfileuid) {
          ctx.add("error", "SEM_UID_ABOVE_HIGHEST", "fileuid " + n.uid + " exceeds highestfileuid " + idx.highestfileuid + ".", n.path);
        }
      }
      if (n !== idx.root && (n.name === "" || n.name === "." || n.name === ".." || n.name.indexOf("/") >= 0 || n.name.indexOf("\u0000") >= 0)) {
        ctx.add("error", "SEM_NAME_INVALID", "Name “" + clip(n.name) + "” is empty, a dot entry, or contains ‘/’ or NUL.", n.path);
      }
      if (n.kind === "dir") {
        var names = new Map();
        n.children.forEach(function (c) {
          if (names.has(c.name)) ctx.add("error", "SEM_NAME_DUPLICATE", "Two entries named “" + clip(c.name) + "” in " + n.path + ".", c.path);
          else names.set(c.name, true);
        });
      }
      if (n.openforwrite) ctx.add("info", "SEM_OPEN_FOR_WRITE", "File was open for write when this index was written.", n.path);
      if (n.kind !== "file" || n.symlink !== null) return;
      stats.extents += n.extents.length;
      if (n.length !== null) stats.bytes += n.length;
      checkExtents(ctx, n, stats, tape, bytsz);
    });
    if (idx.highestfileuid !== null && !exhausted && uids.size && !uids.has(idx.highestfileuid)) {
      ctx.add("info", "SEM_HIGHEST_UID_UNUSED", "No entry uses highestfileuid " + idx.highestfileuid + " (normal after deletions).", "/");
    }
    tape.sort(function (a, b) {
      if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
      return a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0;
    });
    var reported = 0, part = null, maxEnd = 0n, owner = null;
    tape.forEach(function (cur) {
      if (cur[0] !== part) { part = cur[0]; maxEnd = cur[2]; owner = cur; return; }
      if (cur[1] < maxEnd && reported < 50) {
        reported++;
        ctx.add("warning", "SEM_TAPE_OVERLAP", "Extent on partition " + cur[0] + " overlaps data of " + owner[3] + " (assuming " + blocksize + "-byte blocks).", cur[3]);
      }
      if (cur[2] > maxEnd) { maxEnd = cur[2]; owner = cur; }
    });
  }

  function checkExtents(ctx, n, stats, tape, bytsz) {
    var ex = n.extents.filter(function (e) {
      return e.fileoffset !== null && e.bytecount !== null;
    });
    for (var i = 1; i < ex.length; i++) {
      if (ex[i].fileoffset < ex[i - 1].fileoffset) {
        ctx.add("warning", "SEM_EXTENT_ORDER", "Extents are not listed in ascending fileoffset order.", n.path);
        break;
      }
    }
    var sorted = ex.slice().sort(function (a, b) { return a.fileoffset < b.fileoffset ? -1 : a.fileoffset > b.fileoffset ? 1 : 0; });
    var covered = 0n, end = 0n, gaps = false;
    sorted.forEach(function (e, j) {
      var eEnd = e.fileoffset + e.bytecount;
      if (j > 0 && e.fileoffset < end) {
        ctx.add("error", "SEM_EXTENT_OVERLAP", "Extents overlap in the file at offset " + e.fileoffset + ".", n.path);
      }
      if (e.fileoffset > end) gaps = true;
      if (eEnd > end) { covered += eEnd - (e.fileoffset > end ? e.fileoffset : end); end = eEnd; }
      if (e.partition !== null && e.startblock !== null && e.byteoffset !== null) {
        var start = e.startblock * bytsz + e.byteoffset;
        tape.push([e.partition, start, start + e.bytecount, n.path]);
      }
    });
    n.covered = covered;
    if (n.length === null) return;
    if (end > n.length) {
      ctx.add("error", "SEM_EXTENT_BEYOND_LENGTH", "Extents reach byte " + end + " but length is " + n.length + ".", n.path);
    } else if (gaps || end < n.length) {
      stats.sparseFiles++;
      ctx.add("info", "SEM_EXTENT_SPARSE", "Extents cover " + covered + " of " + n.length + " bytes; the rest reads as zeros (sparse).", n.path);
    }
  }

  function fmtPos(p) {
    return p ? p.partition + ":" + p.startblock : "—";
  }

  /* generations(indexes) -> {order, findings, diffs}
   * Cross-checks several parsed indexes as one volume's history. */
  function generations(indexes) {
    var ctx = new Ctx();
    var list = indexes.filter(function (x) { return x && x.generation !== null; });
    var order = list.slice().sort(function (a, b) {
      if (a.generation !== b.generation) return a.generation < b.generation ? -1 : 1;
      return (a.location ? a.location.partition : "") < (b.location ? b.location.partition : "") ? -1 : 1;
    });
    var vols = {};
    order.forEach(function (x) { vols[x.volumeuuid] = (vols[x.volumeuuid] || 0) + 1; });
    if (Object.keys(vols).length > 1) {
      ctx.add("error", "GEN_VOLUME_MISMATCH", "Loaded indexes come from " + Object.keys(vols).length + " different volumes: " + Object.keys(vols).join(", ") + ".", "/");
    }
    for (var i = 0; i < order.length; i++) {
      var cur = order[i];
      var tag = "gen " + cur.generation + " (" + (cur.name || fmtPos(cur.location)) + ")";
      var older = order.filter(function (o) { return o.generation < cur.generation && o.volumeuuid === cur.volumeuuid; });
      var same = order.filter(function (o) { return o !== cur && o.generation === cur.generation && o.volumeuuid === cur.volumeuuid; });
      same.forEach(function (o) {
        if (order.indexOf(o) > i) return;
        if (o.location && cur.location && o.location.partition === cur.location.partition && o.location.startblock === cur.location.startblock) {
          ctx.add("warning", "GEN_DUPLICATE", tag + " was loaded twice.", "/");
        } else if (sameTree(o, cur)) {
          ctx.add("info", "GEN_COPY", tag + " is a copy of the same generation at " + fmtPos(o.location) + ".", "/");
        } else {
          ctx.add("error", "GEN_CONFLICT", tag + " and the copy at " + fmtPos(o.location) + " share a generation number but differ.", "/");
        }
      });
      if (!older.length) continue;
      var prev = older[older.length - 1];
      if (prev.generation + 1n < cur.generation) {
        ctx.add("info", "GEN_GAP", "Generations " + (prev.generation + 1n) + "–" + (cur.generation - 1n) + " were not loaded.", "/");
      }
      if (cur.prevLocation) {
        var hit = older.concat(same).some(function (o) {
          return o.location && o.location.partition === cur.prevLocation.partition && o.location.startblock === cur.prevLocation.startblock;
        });
        if (!hit && prev.generation + 1n === cur.generation) {
          ctx.add("warning", "GEN_CHAIN_BROKEN", tag + " points back to " + fmtPos(cur.prevLocation) + ", which matches no loaded older index.", "/");
        }
      }
      if (prev.updatetime && cur.updatetime && cur.updatetime < prev.updatetime) {
        ctx.add("warning", "GEN_TIME_REGRESSION", tag + " has an updatetime earlier than generation " + prev.generation + ".", "/");
      }
      if (prev.highestfileuid !== null && cur.highestfileuid !== null &&
          cur.highestfileuid !== 0n && cur.highestfileuid < prev.highestfileuid) {
        ctx.add("warning", "GEN_UID_REGRESSION", tag + " lowers highestfileuid from " + prev.highestfileuid + " to " + cur.highestfileuid + ".", "/");
      }
    }
    return { order: order, findings: ctx.findings };
  }

  function flatten(idx) {
    var m = new Map();
    if (idx.root) walk(idx.root, function (n) { m.set(n.path, n); });
    return m;
  }

  function extSig(n) {
    return n.extents.map(function (e) { return [e.partition, e.startblock, e.byteoffset, e.bytecount, e.fileoffset].join(","); }).join(";");
  }

  function sig(n) {
    return [n.kind, String(n.uid), String(n.length), n.times.modifytime || "", n.symlink || "", extSig(n)].join("|");
  }

  function sameTree(a, b) {
    var d = diff(a, b);
    return !d.added.length && !d.removed.length && !d.changed.length;
  }

  /* diff(older, newer) -> {added, removed, changed} lists of {path, kind, what?} */
  function diff(a, b) {
    var A = flatten(a), B = flatten(b), out = { added: [], removed: [], changed: [] };
    B.forEach(function (n, p) {
      var o = A.get(p);
      if (!o) { out.added.push({ path: p, kind: n.kind }); return; }
      if (sig(o) === sig(n)) return;
      var what = [];
      if (o.kind !== n.kind) what.push("kind");
      if (o.uid !== n.uid) what.push("fileuid");
      if (o.length !== n.length) what.push("length " + o.length + "→" + n.length);
      if ((o.times.modifytime || "") !== (n.times.modifytime || "")) what.push("modifytime");
      if ((o.symlink || "") !== (n.symlink || "")) what.push("symlink target");
      if (extSig(o) !== extSig(n)) what.push("extents");
      out.changed.push({ path: p, kind: n.kind, what: what.join(", ") });
    });
    A.forEach(function (n, p) { if (!B.has(p)) out.removed.push({ path: p, kind: n.kind }); });
    var byPath = function (x, y) { return x.path < y.path ? -1 : x.path > y.path ? 1 : 0; };
    out.added.sort(byPath); out.removed.sort(byPath); out.changed.sort(byPath);
    return out;
  }

  // Synthetic sample volume: three generations with one deliberate fault each in
  // the later ones, so the viewer has something to show without real data.
  function sample() {
    var T = function (d) { return "2026-01-0" + d + "T12:00:00.000000000Z"; };
    var times = function (d) {
      return TIMES.map(function (t) { return "<" + t + ">" + T(d) + "</" + t + ">"; }).join("");
    };
    var ext = function (p, sb, bo, bc, fo) {
      return "<extent><partition>" + p + "</partition><startblock>" + sb + "</startblock><byteoffset>" + bo +
        "</byteoffset><bytecount>" + bc + "</bytecount><fileoffset>" + fo + "</fileoffset></extent>";
    };
    var file = function (uid, name, len, d, extents) {
      return "<file><fileuid>" + uid + "</fileuid><name>" + name + "</name><length>" + len + "</length>" + times(d) +
        "<readonly>false</readonly><extentinfo>" + extents + "</extentinfo></file>";
    };
    var dir = function (uid, name, d, inner) {
      return "<directory><fileuid>" + uid + "</fileuid><name>" + name + "</name>" + times(d) +
        "<readonly>false</readonly><contents>" + inner + "</contents></directory>";
    };
    var head = function (gen, d, loc, prev, hi) {
      return '<?xml version="1.0" encoding="UTF-8"?>\n<ltfsindex version="2.4.0"><creator>ltfs-index-viewer synthetic sample</creator>' +
        "<volumeuuid>5a3e0c1d-2b4f-4c6a-9d8e-0f1a2b3c4d5e</volumeuuid><generationnumber>" + gen + "</generationnumber>" +
        "<updatetime>" + T(d) + "</updatetime><location><partition>b</partition><startblock>" + loc + "</startblock></location>" +
        (prev ? "<previousgenerationlocation><partition>b</partition><startblock>" + prev + "</startblock></previousgenerationlocation>" : "") +
        "<allowpolicyupdate>true</allowpolicyupdate><highestfileuid>" + hi + "</highestfileuid>";
    };
    var B = 524288;
    var g1 = head(1, 1, 20, null, 4) + dir(1, "SAMPLE", 1,
      dir(2, "logs", 1, file(3, "2026-01-01.log", B + 100, 1, ext("b", 10, 0, B, 0) + ext("b", 11, 0, 100, B))) +
      file(4, "README", 42, 1, ext("b", 12, 0, 42, 0))) + "</ltfsindex>\n";
    var g2 = head(2, 2, 40, 20, 6) + dir(1, "SAMPLE", 2,
      dir(2, "logs", 2, file(3, "2026-01-01.log", B + 100, 1, ext("b", 10, 0, B, 0) + ext("b", 11, 0, 100, B)) +
        file(5, "2026-01-02.log", 2 * B, 2, ext("b", 30, 0, B, 0) + ext("b", 31, 0, B, B))) +
      file(4, "README", 64, 2, ext("b", 32, 0, 64, 0)) +
      file(6, "sparse.img", 4 * B, 2, ext("b", 33, 0, B, 0) + ext("b", 34, 0, B, 3 * B))) + "</ltfsindex>\n";
    var g3 = head(3, 3, 60, 40, 7) + dir(1, "SAMPLE", 3,
      dir(2, "logs", 3, file(5, "2026-01-02.log", 2 * B, 2, ext("b", 30, 0, B, 0) + ext("b", 31, 0, B, B)) +
        file(7, "2026-01-03.log", 300, 3, ext("b", 31, 1000, 300, 0))) +
      file(4, "README", 64, 2, ext("b", 32, 0, 64, 0)) +
      file(5, "dup-uid.bin", 10, 3, ext("b", 50, 0, 20, 0))) + "</ltfsindex>\n";
    return [{ name: "sample-gen1.xml", text: g1 }, { name: "sample-gen2.xml", text: g2 }, { name: "sample-gen3.xml", text: g3 }];
  }

  var api = {
    parse: parse, generations: generations, diff: diff, sample: sample, fmtPos: fmtPos,
    MAX_BYTES: MAX_BYTES, DEFAULT_BLOCKSIZE: DEFAULT_BLOCKSIZE, SUPPORTED_VERSION: SUPPORTED_VERSION,
  };
  global.LTFS = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
