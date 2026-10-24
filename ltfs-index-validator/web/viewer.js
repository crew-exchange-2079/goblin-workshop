/* viewer.js — DOM side of the LTFS index viewer. Every value taken from an
 * index reaches the page through textContent, never innerHTML. */
(function () {
  "use strict";

  var L = window.LTFS;
  var SEVERITIES = ["fatal", "error", "warning", "info"];
  var RANK = { fatal: 0, error: 1, warning: 2, info: 3 };
  var CHILD_PAGE = 500;
  var LIST_CAP = 200;

  var state = {
    entries: [],      // {name, text, result, refusal}
    gen: null,        // LTFS.generations() over the parsed entries
    current: -1,      // index into entries
    selected: null,   // path
    sev: { fatal: true, error: true, warning: true, info: true },
  };

  var $ = function (id) { return document.getElementById(id); };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    var v = Number(n), u = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"], i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i ? v.toFixed(v < 10 ? 2 : 1) : String(n)) + " " + u[i];
  }

  function blocksize() {
    var v = parseInt($("blocksize").value, 10);
    return v > 0 ? v : L.DEFAULT_BLOCKSIZE;
  }

  // ---- loading -----------------------------------------------------------

  function readFile(file) {
    if (file.size > L.MAX_BYTES) {
      return Promise.resolve({ name: file.name, text: null, refusal: ["INPUT_TOO_LARGE", "File exceeds " + (L.MAX_BYTES >> 20) + " MiB."] });
    }
    return file.arrayBuffer().then(function (buf) {
      try {
        return { name: file.name, text: new TextDecoder("utf-8", { fatal: true }).decode(buf) };
      } catch (e) {
        return { name: file.name, text: null, refusal: ["ENCODING_NOT_UTF8", "File is not valid UTF-8."] };
      }
    }, function () {
      return { name: file.name, text: null, refusal: ["INPUT_READ_FAILED", "The browser could not read the file."] };
    });
  }

  function addFiles(files) {
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return Promise.resolve();
    return Promise.all(list.map(readFile)).then(function (items) {
      items.forEach(function (it) { state.entries.push(it); });
      analyse(true);
    });
  }

  function addTexts(items) {
    items.forEach(function (it) { state.entries.push({ name: it.name, text: it.text }); });
    analyse(true);
  }

  function analyse(pickLatest) {
    var bs = blocksize();
    state.entries.forEach(function (e) {
      if (e.refusal) {
        e.result = { status: e.refusal[0] === "ENCODING_NOT_UTF8" ? "UNSUPPORTED" : "INVALID", index: null,
          findings: [{ severity: "fatal", code: e.refusal[0], message: e.refusal[1], path: "/" }] };
      } else {
        e.result = L.parse(e.text, { name: e.name, blocksize: bs });
      }
    });
    state.gen = L.generations(state.entries.map(function (e) { return e.result.index; }));
    if (pickLatest || state.current < 0 || state.current >= state.entries.length) {
      var last = state.gen.order[state.gen.order.length - 1];
      state.current = last ? indexOfIndex(last) : state.entries.length - 1;
      state.selected = null;
    }
    render();
  }

  function indexOfIndex(idx) {
    for (var i = 0; i < state.entries.length; i++) if (state.entries[i].result.index === idx) return i;
    return -1;
  }

  // ---- rendering ---------------------------------------------------------

  function counts(findings) {
    var c = { fatal: 0, error: 0, warning: 0, info: 0 };
    findings.forEach(function (f) { c[f.severity]++; });
    return c;
  }

  function render() {
    var has = state.entries.length > 0;
    $("loaded").hidden = !has;
    $("workspace").hidden = !has;
    $("clear").disabled = !has;
    $("drop").classList.toggle("compact", has);
    if (!has) return;
    renderGens();
    var entry = state.entries[state.current];
    renderSummary(entry);
    renderFindings(entry);
    renderTree(entry);
    renderDetail(entry);
    renderDiff(entry);
  }

  function renderGens() {
    var ol = $("gens");
    ol.textContent = "";
    var ordered = state.gen.order.map(indexOfIndex);
    state.entries.forEach(function (e, i) { if (ordered.indexOf(i) < 0) ordered.push(i); });
    ordered.forEach(function (i) {
      var e = state.entries[i], idx = e.result.index, c = counts(e.result.findings);
      var li = el("li");
      var b = el("button", "gen" + (i === state.current ? " current" : ""));
      b.type = "button";
      b.dataset.entry = String(i);
      b.setAttribute("aria-pressed", i === state.current ? "true" : "false");
      b.appendChild(el("span", "gen-no", idx ? "gen " + idx.generation : e.result.status));
      b.appendChild(el("span", "gen-name", e.name));
      if (idx) b.appendChild(el("span", "gen-loc", "@ " + L.fmtPos(idx.location)));
      var badges = el("span", "badges");
      SEVERITIES.forEach(function (s) { if (c[s]) badges.appendChild(el("span", "badge sev-" + s, c[s] + " " + s)); });
      b.appendChild(badges);
      b.addEventListener("click", function () { state.current = i; state.selected = null; render(); });
      li.appendChild(b);
      ol.appendChild(li);
    });
  }

  function kv(dl, k, v, cls) {
    dl.appendChild(el("dt", null, k));
    var dd = el("dd", cls, v === null || v === undefined || v === "" ? "—" : v);
    dl.appendChild(dd);
    return dd;
  }

  function renderSummary(entry) {
    var dl = $("summary");
    dl.textContent = "";
    var idx = entry.result.index;
    kv(dl, "file", entry.name);
    if (!idx) { kv(dl, "status", entry.result.status, "bad"); return; }
    kv(dl, "volume uuid", idx.volumeuuid, "mono");
    kv(dl, "generation", idx.generation);
    kv(dl, "update time", idx.updatetime, "mono");
    kv(dl, "location", L.fmtPos(idx.location), "mono");
    kv(dl, "previous gen. location", idx.prevLocation ? L.fmtPos(idx.prevLocation) : null, "mono");
    kv(dl, "creator", idx.creator);
    if (idx.comment) kv(dl, "comment", idx.comment);
    kv(dl, "highest fileuid", idx.highestfileuid);
    kv(dl, "policy update", idx.allowpolicyupdate === null ? null : idx.allowpolicyupdate ? "allowed" : "not allowed");
    if (idx.policy) kv(dl, "index-partition policy", (idx.policy.names.length ? idx.policy.names.join(", ") : "no names") + " ≤ " + fmtBytes(idx.policy.size));
    if (idx.volumelockstate) kv(dl, "lock state", idx.volumelockstate);
    var s = idx.stats;
    if (s) {
      kv(dl, "entries", s.dirs + " dirs · " + s.files + " files · " + s.symlinks + " symlinks");
      kv(dl, "data", fmtBytes(s.bytes) + " in " + s.extents + " extents" + (s.sparseFiles ? " · " + s.sparseFiles + " sparse" : ""));
    }
  }

  function allFindings(entry) {
    var own = entry.result.findings.map(function (f) { return Object.assign({ scope: "index" }, f); });
    if (!entry.result.index) return own;
    var gen = state.gen.findings.map(function (f) { return Object.assign({ scope: "generations" }, f); });
    return own.concat(gen);
  }

  function renderFindings(entry) {
    var list = allFindings(entry);
    var c = counts(list);
    var bar = $("sevfilter");
    bar.textContent = "";
    SEVERITIES.forEach(function (s) {
      var b = el("button", "sevbtn sev-" + s, s + " " + c[s]);
      b.type = "button";
      b.dataset.sev = s;
      b.setAttribute("aria-pressed", state.sev[s] ? "true" : "false");
      b.addEventListener("click", function () { state.sev[s] = !state.sev[s]; renderFindings(entry); });
      bar.appendChild(b);
    });
    var structural = list.filter(function (f) { return /^STRUCT_/.test(f.code); }).length;
    var v = $("verdict");
    if (!entry.result.index) {
      v.textContent = "Refused (" + entry.result.status + "): the index was not read.";
      v.className = "verdict bad";
    } else if (structural) {
      v.textContent = structural + " structural issue" + (structural > 1 ? "s" : "") + " found by the schema mirror — validate.py should report INVALID.";
      v.className = "verdict bad";
    } else {
      v.textContent = "The schema mirror found no structural issues. Run validate.py for the authoritative XSD check.";
      v.className = "verdict ok";
    }
    var ul = $("findings");
    ul.textContent = "";
    var shown = list.filter(function (f) { return state.sev[f.severity]; })
      .sort(function (a, b) { return RANK[a.severity] - RANK[b.severity]; });
    if (!shown.length) ul.appendChild(el("li", "dim", list.length ? "All findings hidden by the filter." : "No findings."));
    shown.slice(0, 1000).forEach(function (f) {
      var li = el("li", "finding sev-" + f.severity);
      li.dataset.code = f.code;
      li.appendChild(el("span", "sev", f.severity));
      li.appendChild(el("code", "code", f.code));
      li.appendChild(el("span", "msg", f.message));
      if (f.scope === "generations") li.appendChild(el("span", "scope", "across generations"));
      else if (f.path && f.path !== "/" && !/\?$/.test(f.path)) {
        var a = el("button", "pathlink", f.path);
        a.type = "button";
        a.addEventListener("click", function () { select(f.path, true); });
        li.appendChild(a);
      }
      ul.appendChild(li);
    });
    if (shown.length > 1000) ul.appendChild(el("li", "dim", (shown.length - 1000) + " more not shown."));
  }

  // ---- tree --------------------------------------------------------------

  var nodeByPath = new Map();
  var worstByPath = new Map();

  function indexMaps(entry) {
    nodeByPath = new Map();
    worstByPath = new Map();
    var idx = entry.result.index;
    if (!idx || !idx.root) return;
    var stack = [idx.root];
    while (stack.length) {
      var n = stack.pop();
      nodeByPath.set(n.path, n);
      n.children.forEach(function (c) { stack.push(c); });
    }
    entry.result.findings.forEach(function (f) {
      var w = worstByPath.get(f.path);
      if (f.path !== "/" && (!w || RANK[f.severity] < RANK[w])) worstByPath.set(f.path, f.severity);
    });
  }

  function renderTree(entry) {
    indexMaps(entry);
    var ul = $("tree");
    ul.textContent = "";
    var idx = entry.result.index;
    if (!idx || !idx.root) { ul.appendChild(el("li", "dim", "No tree to show.")); return; }
    var q = $("filter").value.trim().toLowerCase();
    if (q) {
      var hits = [];
      nodeByPath.forEach(function (n, p) { if (n.name.toLowerCase().indexOf(q) >= 0) hits.push(n); });
      hits.sort(function (a, b) { return a.path < b.path ? -1 : 1; });
      hits.slice(0, CHILD_PAGE).forEach(function (n) { ul.appendChild(treeItem(n, true)); });
      if (!hits.length) ul.appendChild(el("li", "dim", "No names match."));
      if (hits.length > CHILD_PAGE) ul.appendChild(el("li", "dim", (hits.length - CHILD_PAGE) + " more matches not shown."));
      return;
    }
    var rootItem = treeItem(idx.root, false);
    ul.appendChild(rootItem);
    expand(rootItem, idx.root);
    if (state.selected) reveal(state.selected);
  }

  function nodeLabel(n) {
    if (n.kind === "dir") return n.path === "/" ? "/ (" + (n.name || "root") + ")" : n.name;
    return n.name;
  }

  function treeItem(n, flat) {
    var li = el("li", "titem");
    li.setAttribute("role", "treeitem");
    li.dataset.path = n.path;
    var row = el("div", "trow" + (state.selected === n.path ? " selected" : ""));
    var tog = el("button", "tog", n.kind === "dir" && !flat ? "▸" : "");
    tog.type = "button";
    tog.tabIndex = -1;
    if (n.kind === "dir" && !flat) {
      tog.setAttribute("aria-label", "expand");
      li.setAttribute("aria-expanded", "false");
      tog.addEventListener("click", function () {
        if (li.getAttribute("aria-expanded") === "true") collapse(li); else expand(li, n);
      });
    } else tog.disabled = true;
    row.appendChild(tog);
    var name = el("button", "tname kind-" + (n.symlink !== null ? "link" : n.kind), flat ? n.path : nodeLabel(n));
    name.type = "button";
    name.addEventListener("click", function () { select(n.path, false); });
    row.appendChild(name);
    if (n.kind === "file") row.appendChild(el("span", "tsize", n.symlink !== null ? "→ " + n.symlink : fmtBytes(n.length)));
    else row.appendChild(el("span", "tsize", n.children.length + " entries"));
    var w = worstByPath.get(n.path);
    if (w && w !== "info") row.appendChild(el("span", "dot sev-" + w, w));
    li.appendChild(row);
    return li;
  }

  function expand(li, n, upTo) {
    collapse(li);
    li.setAttribute("aria-expanded", "true");
    var tog = li.querySelector(".tog");
    tog.textContent = "▾";
    tog.setAttribute("aria-label", "collapse");
    var ul = el("ul", "tgroup");
    ul.setAttribute("role", "group");
    var kids = n.children.slice().sort(function (a, b) {
      if (a.kind !== b.kind) return a.kind === "dir" ? -1 : 1;
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
    });
    var limit = Math.max(CHILD_PAGE, upTo || 0);
    kids.slice(0, limit).forEach(function (c) { ul.appendChild(treeItem(c, false)); });
    if (kids.length > limit) {
      var more = el("li", "more");
      var b = el("button", "linkish", "show " + Math.min(CHILD_PAGE, kids.length - limit) + " more of " + (kids.length - limit));
      b.type = "button";
      b.addEventListener("click", function () { expand(li, n, limit + CHILD_PAGE); });
      more.appendChild(b);
      ul.appendChild(more);
    }
    li.appendChild(ul);
    return kids;
  }

  function collapse(li) {
    var g = li.querySelector(":scope > .tgroup");
    if (g) g.remove();
    li.setAttribute("aria-expanded", "false");
    var tog = li.querySelector(".tog");
    tog.textContent = "▸";
    tog.setAttribute("aria-label", "expand");
  }

  function findItem(path) {
    var items = $("tree").querySelectorAll("li.titem");
    for (var i = 0; i < items.length; i++) if (items[i].dataset.path === path) return items[i];
    return null;
  }

  // Expand ancestors so `path` is visible in the tree.
  function reveal(path) {
    if (!nodeByPath.has(path)) return null;
    var parts = path === "/" ? [] : path.slice(1).split("/");
    var cur = "/", li = findItem("/");
    for (var i = 0; i < parts.length && li; i++) {
      var n = nodeByPath.get(cur);
      var next = (cur === "/" ? "/" : cur + "/") + parts[i];
      if (li.getAttribute("aria-expanded") !== "true" || !findItem(next)) {
        var kids = expand(li, n);
        var pos = kids.findIndex(function (k) { return k.path === next; });
        if (pos >= CHILD_PAGE) expand(li, n, pos + 1);
      }
      cur = next;
      li = findItem(cur);
    }
    return li;
  }

  function select(path, fromFinding) {
    state.selected = path;
    if (fromFinding && $("filter").value) { $("filter").value = ""; renderTree(state.entries[state.current]); }
    var items = $("tree").querySelectorAll(".trow.selected");
    for (var i = 0; i < items.length; i++) items[i].classList.remove("selected");
    var li = reveal(path) || findItem(path);
    if (li) {
      li.querySelector(".trow").classList.add("selected");
      if (fromFinding) li.scrollIntoView({ block: "nearest" });
    }
    renderDetail(state.entries[state.current]);
  }

  // ---- detail ------------------------------------------------------------

  function renderDetail(entry) {
    var box = $("detail");
    box.textContent = "";
    var n = state.selected ? nodeByPath.get(state.selected) : null;
    if (!n) { box.appendChild(el("p", "dim", "Select an entry in the tree.")); return; }
    var dl = el("dl", "kv");
    kv(dl, "path", n.path, "mono");
    kv(dl, "kind", n.kind === "dir" ? "directory" : n.symlink !== null ? "symlink" : "file");
    kv(dl, "fileuid", n.uid);
    if (n.percentEncoded) kv(dl, "stored name", n.rawName + " (percent-encoded)", "mono");
    if (n.kind === "file") kv(dl, "length", n.length === null ? null : n.length + " B (" + fmtBytes(n.length) + ")");
    if (n.symlink !== null) kv(dl, "target", n.symlink, "mono");
    kv(dl, "read-only", n.readonly === null ? null : n.readonly ? "yes" : "no");
    if (n.openforwrite) kv(dl, "open for write", "yes", "bad");
    ["creationtime", "changetime", "modifytime", "accesstime", "backuptime"].forEach(function (t) {
      kv(dl, t.replace("time", " time"), n.times[t], "mono");
    });
    box.appendChild(dl);

    var mine = entry.result.findings.filter(function (f) { return f.path === n.path; });
    if (mine.length) {
      box.appendChild(el("h3", null, "Findings for this entry"));
      var fu = el("ul", "findinglist");
      mine.forEach(function (f) {
        var li = el("li", "finding sev-" + f.severity);
        li.appendChild(el("span", "sev", f.severity));
        li.appendChild(el("code", "code", f.code));
        li.appendChild(el("span", "msg", f.message));
        fu.appendChild(li);
      });
      box.appendChild(fu);
    }

    if (n.xattrs.length) {
      box.appendChild(el("h3", null, "Extended attributes"));
      var xt = table(["key", "type", "value"]);
      n.xattrs.forEach(function (x) { row(xt, [x.key, x.type, x.value.length > 200 ? x.value.slice(0, 197) + "…" : x.value]); });
      box.appendChild(xt);
    }

    if (n.kind === "file" && n.symlink === null) {
      box.appendChild(el("h3", null, "Extents (" + n.extents.length + ")"));
      if (n.length !== null && n.length > 0n) box.appendChild(extentMap(n));
      if (n.extents.length) {
        var et = table(["#", "partition", "start block", "byte offset", "byte count", "file offset", "file end"]);
        n.extents.forEach(function (e, i) {
          var end = e.fileoffset !== null && e.bytecount !== null ? e.fileoffset + e.bytecount : null;
          row(et, [i + 1, e.partition, e.startblock, e.byteoffset, e.bytecount, e.fileoffset, end], "num");
        });
        box.appendChild(et);
      } else box.appendChild(el("p", "dim", n.length > 0n ? "No extents: the whole file is sparse." : "Empty file."));
    }
  }

  function table(heads) {
    var t = el("table", "grid");
    var tr = el("tr");
    heads.forEach(function (h) { tr.appendChild(el("th", null, h)); });
    var th = el("thead"); th.appendChild(tr); t.appendChild(th);
    t.appendChild(el("tbody"));
    return t;
  }

  function row(t, cells, cls) {
    var tr = el("tr");
    cells.forEach(function (c) { tr.appendChild(el("td", cls, c === null || c === undefined ? "—" : c)); });
    t.tBodies[0].appendChild(tr);
  }

  // File-offset coverage bar: one segment per extent, positioned by CSSOM
  // (inline style attributes are blocked by the page's CSP).
  function extentMap(n) {
    var wrap = el("div", "emap");
    wrap.setAttribute("role", "img");
    var len = Number(n.length);
    var cov = n.covered !== undefined ? n.covered : 0n;
    wrap.setAttribute("aria-label", "Extents cover " + cov + " of " + n.length + " bytes");
    n.extents.forEach(function (e, i) {
      if (e.fileoffset === null || e.bytecount === null) return;
      var seg = el("div", "eseg" + (i % 2 ? " alt" : ""));
      var start = Number(e.fileoffset), size = Number(e.bytecount);
      seg.style.left = Math.min(100, start / len * 100) + "%";
      seg.style.width = Math.max(0.4, Math.min(100 - Math.min(100, start / len * 100), size / len * 100)) + "%";
      if (start + size > len) seg.classList.add("over");
      seg.title = "extent " + (i + 1) + ": bytes " + e.fileoffset + "–" + (e.fileoffset + e.bytecount);
      wrap.appendChild(seg);
    });
    return wrap;
  }

  // ---- generation diff ---------------------------------------------------

  function renderDiff(entry) {
    var panel = $("diffpanel"), box = $("diff");
    box.textContent = "";
    var idx = entry.result.index;
    var order = state.gen.order;
    var pos = idx ? order.indexOf(idx) : -1;
    var prev = null;
    for (var i = pos - 1; i >= 0; i--) {
      if (order[i].volumeuuid === idx.volumeuuid && order[i].generation < idx.generation) { prev = order[i]; break; }
    }
    panel.hidden = !prev;
    if (!prev) return;
    var d = L.diff(prev, idx);
    box.appendChild(el("p", "dim", "gen " + prev.generation + " (" + prev.name + ") → gen " + idx.generation + " (" + idx.name + ")"));
    [["added", d.added, "+"], ["removed", d.removed, "−"], ["changed", d.changed, "~"]].forEach(function (g) {
      var sec = el("div", "dgroup d-" + g[0]);
      sec.appendChild(el("h3", null, g[0] + " (" + g[1].length + ")"));
      var ul = el("ul", "dlist");
      g[1].slice(0, LIST_CAP).forEach(function (x) {
        var li = el("li");
        li.appendChild(el("span", "dmark", g[2]));
        li.appendChild(el("span", "mono", x.path + (x.kind === "dir" && x.path !== "/" ? "/" : "")));
        if (x.what) li.appendChild(el("span", "dim", " " + x.what));
        ul.appendChild(li);
      });
      if (g[1].length > LIST_CAP) ul.appendChild(el("li", "dim", (g[1].length - LIST_CAP) + " more not shown."));
      sec.appendChild(ul);
      box.appendChild(sec);
    });
  }

  // ---- wiring ------------------------------------------------------------

  function isFileDrag(ev) {
    var t = ev.dataTransfer && ev.dataTransfer.types;
    return t && Array.prototype.indexOf.call(t, "Files") >= 0;
  }

  window.addEventListener("dragover", function (ev) {
    if (!isFileDrag(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    document.body.classList.add("dragging");
  });
  window.addEventListener("dragleave", function (ev) {
    if (ev.relatedTarget === null) document.body.classList.remove("dragging");
  });
  window.addEventListener("drop", function (ev) {
    if (!ev.dataTransfer) return;
    ev.preventDefault();
    document.body.classList.remove("dragging");
    addFiles(ev.dataTransfer.files);
  });
  $("file").addEventListener("change", function (ev) {
    addFiles(ev.target.files).then(function () { ev.target.value = ""; });
  });
  $("drop").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); $("file").click(); }
  });
  $("sample").addEventListener("click", function () { addTexts(L.sample()); });
  $("clear").addEventListener("click", function () {
    state.entries = []; state.current = -1; state.selected = null; state.gen = null;
    $("filter").value = "";
    render();
  });
  $("blocksize").addEventListener("change", function () { if (state.entries.length) analyse(false); });
  $("filter").addEventListener("input", function () { if (state.current >= 0) renderTree(state.entries[state.current]); });

  // Test and console hook: read-only view of the current state.
  window.ltfsViewer = { state: state, addTexts: addTexts };
})();
