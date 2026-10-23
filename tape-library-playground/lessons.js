/*
 * lessons.js — guided operator lessons for the tape library playground.
 *
 * sim.js models *scheduling*: who gets the next free drive. This file models
 * the other half of running a library: the operator at the console mounting
 * tapes, cleaning drives, swapping cartridges through the import/export
 * (I/E) slots, reading TapeAlert flags and recovering from a robot jam.
 *
 * Pure, like sim.js: no DOM, no Node-only globals, no Math.random, no clock.
 * The same file runs in the browser and under `node --test`.
 *
 * Everything is INVENTED. The console syntax is mtx-flavoured but belongs to
 * no real product. The TapeAlert flag numbers and names follow the publicly
 * listed TapeAlert table; which flags a drive raises, and when, is a teaching
 * simplification, not the behaviour of any particular drive. Times reuse the
 * illustrative costs from sim.js.
 *
 * Two pieces:
 *   createLibrary(setup)  a small state machine that accepts console commands
 *   createLesson(id)      a library plus a step checker: after every command
 *                         it asks "is the current step's goal now true?" and
 *                         advances. Goals are checked against library state
 *                         (or what a command reported), never against the
 *                         exact text typed, so any correct path counts.
 */
import { DEFAULT_COSTS } from './sim.js';

/* TapeAlert flags used here, numbered as in the public TapeAlert table. */
export const TAPEALERT = {
  3: 'Hard error',
  5: 'Read failure',
  20: 'Clean now',
  21: 'Clean periodic',
  22: 'Expired cleaning media',
  23: 'Invalid cleaning tape',
  30: 'Hardware A',
  39: 'Diagnostics required',
};

const C = {
  ...DEFAULT_COSTS,
  cleanSeconds: 95, // invented: one cleaning pass, start to eject
  scanSecondsPerSlot: 1.5, // invented: barcode scan per element during inventory
  verifySeconds: 30, // invented: read back a tape's label and first block
};

export const HELP = [
  'status                    what the library believes is where',
  'load <slot|barcode> <Dn>  mount a cartridge in drive n',
  'unload <Dn> [slot]        rewind and return the tape (home slot by default)',
  'transfer <from> <to>      move a cartridge between slots / I/E slots',
  'export <slot|barcode> <IEn>   same as transfer, into an I/E slot',
  'import <IEn> <slot>           same as transfer, out of an I/E slot',
  'door open | door close    the I/E station door',
  'clean <Dn> <slot>         run the cleaning cartridge in <slot> through Dn',
  'tapealert <Dn>            read the drive\'s TapeAlert flags',
  'verify <Dn>               read back the mounted tape',
  'drive offline <Dn> | drive online <Dn>',
  'robot status | robot reset',
  'inventory                 rescan every barcode',
  'help',
];

const isCleaning = (id) => /^CLN/.test(id);

/* ------------------------------------------------------------------ *
 * The library
 * ------------------------------------------------------------------ */
export function createLibrary(setup) {
  const cols = setup.cols || 8;
  const slotCount = setup.slots || 16;
  const ieCount = setup.ie || 2;

  const slots = new Array(slotCount).fill(null); // physical truth
  const carts = new Map(); // id -> { id, home, usesLeft }
  for (const [slot1, id, extra] of setup.carts || []) {
    slots[slot1 - 1] = id;
    carts.set(id, { id, home: slot1 - 1, usesLeft: null, ...(extra || {}) });
  }
  const ie = new Array(ieCount).fill(null);
  for (const [ie1, id] of setup.ieCarts || []) {
    ie[ie1 - 1] = id;
    carts.set(id, { id, home: null, usesLeft: null });
  }

  const drives = (setup.drives || [{}, {}]).map((d, i) => ({
    name: `D${i + 1}`,
    cart: d.cart || null,
    online: d.online !== false,
    flags: new Set(d.flags || []),
    fault: d.fault || null, // 'hardware' = the drive itself is broken
  }));
  for (const d of drives) if (d.cart && !carts.has(d.cart)) carts.set(d.cart, { id: d.cart, home: null, usesLeft: null });

  const robot = { state: setup.robot?.state || 'ok', holding: setup.robot?.holding || null, where: setup.robot?.where || null };
  if (robot.holding && !carts.has(robot.holding)) carts.set(robot.holding, { id: robot.holding, home: null, usesLeft: null });
  for (const [id, home] of setup.homes || []) carts.get(id).home = home - 1;

  // what the library *believes* is in each storage slot; only drifts from the
  // truth when something happens behind the robot's back (a jam, a hand)
  const believed = setup.believed ? setup.believed.slice() : slots.slice();
  let inventoryValid = setup.inventoryValid !== false;
  let door = 'closed';
  const offsite = []; // cartridges an operator has taken out of the I/E station
  const operatorPlaces = (setup.operatorPlaces || []).slice(); // tapes waiting to be put into the I/E slots
  let time = 0;
  const log = (setup.history || []).map((text) => ({ t: 0, text }));

  /* ---- parsing -------------------------------------------------- */
  const err = (text) => ({ ok: false, text });
  const okr = (text, data) => ({ ok: true, text, data });

  function parseDrive(tok) {
    const m = /^d(\d+)$/i.exec(tok || '');
    if (!m) return { error: `expected a drive like D1, got "${tok ?? ''}"` };
    const i = Number(m[1]) - 1;
    if (i < 0 || i >= drives.length) return { error: `no drive ${tok.toUpperCase()} (this library has D1–D${drives.length})` };
    return { drive: drives[i] };
  }

  // a storage slot (4, s4), an I/E slot (IE1) or a barcode, resolved through
  // what the library believes — which is exactly why a stale inventory hurts
  function parseElement(tok) {
    if (!tok) return { error: 'missing a slot, I/E slot or barcode' };
    let m = /^s?(\d+)$/i.exec(tok);
    if (m) {
      const i = Number(m[1]) - 1;
      if (i < 0 || i >= slotCount) return { error: `no slot ${m[1]} (slots are 1–${slotCount})` };
      return { el: { kind: 'slot', i } };
    }
    m = /^ie(\d+)$/i.exec(tok);
    if (m) {
      const i = Number(m[1]) - 1;
      if (i < 0 || i >= ieCount) return { error: `no ${tok.toUpperCase()} (I/E slots are IE1–IE${ieCount})` };
      return { el: { kind: 'ie', i } };
    }
    const id = tok.toUpperCase();
    const si = believed.indexOf(id);
    if (si >= 0) return { el: { kind: 'slot', i: si } };
    const ii = ie.indexOf(id);
    if (ii >= 0) return { el: { kind: 'ie', i: ii } };
    const d = drives.find((x) => x.cart === id);
    if (d) return { error: `${id} is in ${d.name}; unload it first` };
    return { error: `no slot holds ${id} as far as the library knows` };
  }

  const elName = (el) => (el.kind === 'slot' ? `slot ${el.i + 1}` : `IE${el.i + 1}`);
  const truthAt = (el) => (el.kind === 'slot' ? slots[el.i] : ie[el.i]);
  const beliefAt = (el) => (el.kind === 'slot' ? believed[el.i] : ie[el.i]);
  function setAt(el, id) {
    if (el.kind === 'slot') {
      slots[el.i] = id;
      believed[el.i] = id;
    } else ie[el.i] = id;
  }

  /* ---- shared preconditions ------------------------------------- */
  function robotBlocked() {
    if (robot.state === 'jammed') return `robot is jammed${robot.holding ? ` holding ${robot.holding}` : ''}; run \`robot reset\``;
    if (!inventoryValid) return 'inventory is unverified since the last recovery; run `inventory` before moving anything';
    return null;
  }
  function ieBlocked(...els) {
    if (door === 'open' && els.some((e) => e.kind === 'ie')) return 'the I/E door is open: the robot will not reach into the I/E station; `door close` first';
    return null;
  }

  // coarse robot cost: pick + travel + place, travel scaled by how far apart
  function pos(el) {
    if (el.kind === 'slot') return { x: el.i % cols, y: Math.floor(el.i / cols) * 4 };
    if (el.kind === 'ie') return { x: -2, y: el.i * 4 };
    return { x: 2 + el.i * 4, y: 12 };
  }
  function moveCost(a, b) {
    const pa = pos(a);
    const pb = pos(b);
    return C.robotPickSeconds + C.robotPlaceSeconds + Math.hypot(pa.x - pb.x, pa.y - pb.y) * C.robotSecondsPerSlot;
  }
  const driveEl = (d) => ({ kind: 'drive', i: drives.indexOf(d) });

  function freeSlotNear(i) {
    for (let k = 0; k < slotCount; k++) {
      for (const j of [i + k, i - k]) if (j >= 0 && j < slotCount && slots[j] === null && believed[j] === null) return j;
    }
    return null;
  }

  // the robot reaches for a slot the library thinks is full; physics decides
  function fetchFrom(el) {
    const believedId = beliefAt(el);
    const actual = truthAt(el);
    if (!believedId) return { error: `${elName(el)} is empty` };
    if (actual !== believedId) {
      inventoryValid = false;
      return { error: `robot found ${elName(el)} ${actual ? `holding ${actual}, not ${believedId}` : 'empty'} — inventory mismatch; run \`inventory\`` };
    }
    return { id: actual };
  }

  /* ---- commands ------------------------------------------------- */
  function status() {
    const lines = [];
    lines.push(`robot: ${robot.state}${robot.holding ? ` (holding ${robot.holding})` : ''}  ·  I/E door: ${door}  ·  inventory: ${inventoryValid ? 'verified' : 'UNVERIFIED'}`);
    for (const d of drives) {
      const fl = [...d.flags].sort((a, b) => a - b);
      lines.push(`${d.name}: ${d.cart || 'empty'}${d.online ? '' : '  [offline]'}${fl.length ? `  TapeAlert ${fl.join(',')}` : ''}`);
    }
    const cells = believed.map((id, i) => `${String(i + 1).padStart(2)}:${id ? id + (isCleaning(id) ? `(${carts.get(id)?.usesLeft ?? '?'} uses)` : '') : '—'}`);
    for (let r = 0; r < cells.length; r += 4) lines.push(cells.slice(r, r + 4).join('  '));
    lines.push(ie.map((id, i) => `IE${i + 1}:${id || '—'}`).join('  '));
    return okr(lines.join('\n'), { view: 'status' });
  }

  function load(args) {
    const b = robotBlocked();
    if (b) return err(b);
    const src = parseElement(args[0]);
    if (src.error) return err(src.error);
    const dst = parseDrive(args[1]);
    if (dst.error) return err(dst.error);
    const d = dst.drive;
    const ib = ieBlocked(src.el);
    if (ib) return err(ib);
    if (!d.online) return err(`${d.name} is offline; the library will not mount into it`);
    if (d.cart) return err(`${d.name} already holds ${d.cart}; unload it first`);
    const got = fetchFrom(src.el);
    if (got.error) return err(got.error);
    if (isCleaning(got.id)) return err(`${got.id} is a cleaning cartridge; use \`clean ${d.name} <slot>\``);
    setAt(src.el, null);
    d.cart = got.id;
    time += moveCost(src.el, driveEl(d)) + C.loadSeconds;
    return okr(`${got.id}: ${elName(src.el)} → ${d.name}, threaded (${C.loadSeconds}s load)`, { drive: d.name, cart: got.id });
  }

  function unload(args) {
    const b = robotBlocked();
    if (b) return err(b);
    const dd = parseDrive(args[0]);
    if (dd.error) return err(dd.error);
    const d = dd.drive;
    if (!d.cart) return err(`${d.name} is empty`);
    let target;
    if (args[1]) {
      const t = parseElement(args[1]);
      if (t.error) return err(t.error);
      target = t.el;
      const ib = ieBlocked(target);
      if (ib) return err(ib);
      if (truthAt(target) || beliefAt(target)) return err(`${elName(target)} is not empty`);
    } else {
      const home = carts.get(d.cart)?.home;
      const i = home != null && slots[home] === null && believed[home] === null ? home : freeSlotNear(home ?? 0);
      if (i == null) return err('no free slot to put it in');
      target = { kind: 'slot', i };
    }
    const id = d.cart;
    d.cart = null;
    setAt(target, id);
    time += C.unloadSeconds + C.robotEjectSeconds + moveCost(driveEl(d), target);
    return okr(`${id}: rewound (${C.unloadSeconds}s), ${d.name} → ${elName(target)}`, { drive: d.name, cart: id, to: elName(target) });
  }

  function transfer(args, verb = 'transfer') {
    const b = robotBlocked();
    if (b) return err(b);
    const s = parseElement(args[0]);
    if (s.error) return err(s.error);
    const t = parseElement(args[1]);
    if (t.error) return err(t.error);
    if (verb === 'export' && t.el.kind !== 'ie') return err('export goes into an I/E slot, e.g. `export 7 IE1`');
    if (verb === 'import' && s.el.kind !== 'ie') return err('import comes out of an I/E slot, e.g. `import IE1 9`');
    const ib = ieBlocked(s.el, t.el);
    if (ib) return err(ib);
    if (truthAt(t.el) || beliefAt(t.el)) return err(`${elName(t.el)} is not empty`);
    const got = fetchFrom(s.el);
    if (got.error) return err(got.error);
    setAt(s.el, null);
    setAt(t.el, got.id);
    const c = carts.get(got.id);
    if (c && c.home == null && t.el.kind === 'slot') c.home = t.el.i; // a new tape's home is where it first lands
    time += moveCost(s.el, t.el);
    return okr(`${got.id}: ${elName(s.el)} → ${elName(t.el)}`, { cart: got.id, from: elName(s.el), to: elName(t.el) });
  }

  function doorCmd(args) {
    const want = (args[0] || '').toLowerCase();
    if (want === 'open') {
      if (door === 'open') return err('the I/E door is already open');
      door = 'open';
      const lines = ['I/E door open — robot locked out of the I/E station'];
      const taken = [];
      for (let i = 0; i < ieCount; i++) {
        if (ie[i]) {
          taken.push(ie[i]);
          offsite.push(ie[i]);
          ie[i] = null;
        }
      }
      if (taken.length) lines.push(`operator takes out ${taken.join(', ')}`);
      const placed = [];
      for (let i = 0; i < ieCount && operatorPlaces.length; i++) {
        if (!ie[i]) {
          const id = operatorPlaces.shift();
          ie[i] = id;
          if (!carts.has(id)) carts.set(id, { id, home: null, usesLeft: null });
          placed.push(`${id} in IE${i + 1}`);
        }
      }
      if (placed.length) lines.push(`operator puts in ${placed.join(', ')}`);
      return okr(lines.join('\n'), { door: 'open', taken, placed });
    }
    if (want === 'close') {
      if (door === 'closed') return err('the I/E door is already closed');
      door = 'closed';
      time += ieCount * C.scanSecondsPerSlot;
      return okr(`I/E door closed — scanned ${ie.map((id, i) => `IE${i + 1}:${id || '—'}`).join(' ')}`, { door: 'closed' });
    }
    return err('usage: door open | door close');
  }

  function clean(args) {
    const b = robotBlocked();
    if (b) return err(b);
    const dd = parseDrive(args[0]);
    if (dd.error) return err(dd.error);
    const d = dd.drive;
    if (!args[1]) return err(`usage: clean ${d.name} <slot holding a cleaning cartridge>`);
    const s = parseElement(args[1]);
    if (s.error) return err(s.error);
    const ib = ieBlocked(s.el);
    if (ib) return err(ib);
    if (!d.online) return err(`${d.name} is offline`);
    if (d.cart) return err(`${d.name} holds ${d.cart}; unload it before cleaning`);
    const got = fetchFrom(s.el);
    if (got.error) return err(got.error);
    if (!isCleaning(got.id)) {
      d.flags.add(23);
      time += 2 * moveCost(s.el, driveEl(d)) + C.loadSeconds;
      return { ok: false, text: `${got.id} is a data cartridge, not a cleaner — ${d.name} rejected it (TapeAlert 23)`, data: { drive: d.name, cart: got.id, flag: 23 } };
    }
    const c = carts.get(got.id);
    time += 2 * moveCost(s.el, driveEl(d));
    if (!(c.usesLeft > 0)) {
      d.flags.add(22);
      time += 20;
      return {
        ok: false,
        text: `${got.id} has no cleaning passes left — ${d.name} ejected it (TapeAlert 22: ${TAPEALERT[22]}); it is back in ${elName(s.el)}`,
        data: { drive: d.name, cart: got.id, flag: 22 },
      };
    }
    c.usesLeft--;
    time += C.cleanSeconds;
    d.flags.delete(20);
    d.flags.delete(21);
    d.flags.delete(22);
    d.flags.delete(23);
    const still = [...d.flags].sort((a, b) => a - b);
    return okr(
      `${d.name} cleaned with ${got.id} (${c.usesLeft} uses left), returned to ${elName(s.el)}` +
        (still.length ? `\nstill flagged: ${still.map((f) => `${f} ${TAPEALERT[f]}`).join(', ')} — cleaning does not fix this` : ''),
      { drive: d.name, cart: got.id },
    );
  }

  function tapealert(args) {
    const dd = parseDrive(args[0]);
    if (dd.error) return err(dd.error);
    const d = dd.drive;
    const fl = [...d.flags].sort((a, b) => a - b);
    return okr(
      fl.length ? `${d.name} TapeAlert:\n` + fl.map((f) => `  flag ${f}: ${TAPEALERT[f] || 'unknown'}`).join('\n') : `${d.name}: no TapeAlert flags set`,
      { drive: d.name, flags: fl },
    );
  }

  function verify(args) {
    const dd = parseDrive(args[0]);
    if (dd.error) return err(dd.error);
    const d = dd.drive;
    if (!d.cart) return err(`${d.name} is empty`);
    time += C.verifySeconds;
    if (d.fault === 'hardware') {
      d.flags.add(3);
      d.flags.add(30);
      return { ok: false, text: `${d.name}: read of ${d.cart} FAILED (TapeAlert 3 ${TAPEALERT[3]}, 30 ${TAPEALERT[30]})`, data: { drive: d.name, cart: d.cart } };
    }
    if (d.flags.has(20)) {
      d.flags.add(5);
      return { ok: false, text: `${d.name}: read of ${d.cart} FAILED after retries (TapeAlert 5 ${TAPEALERT[5]}); the drive is asking to be cleaned`, data: { drive: d.name, cart: d.cart } };
    }
    return okr(`${d.name}: ${d.cart} label and first block read back OK`, { drive: d.name, cart: d.cart });
  }

  function driveCmd(args) {
    const want = (args[0] || '').toLowerCase();
    const dd = parseDrive(args[1]);
    if (want !== 'offline' && want !== 'online') return err('usage: drive offline <Dn> | drive online <Dn>');
    if (dd.error) return err(dd.error);
    const d = dd.drive;
    if (want === 'offline') {
      if (!d.online) return err(`${d.name} is already offline`);
      if (d.cart) return err(`${d.name} still holds ${d.cart}; unload it first or the tape is stranded inside an offline drive`);
      d.online = false;
      return okr(`${d.name} offline — no new mounts will be scheduled to it`, { drive: d.name });
    }
    if (d.online) return err(`${d.name} is already online`);
    d.online = true;
    return okr(`${d.name} online`, { drive: d.name });
  }

  function robotCmd(args) {
    const want = (args[0] || '').toLowerCase();
    if (want === 'status') {
      if (robot.state === 'jammed') return okr(`robot JAMMED${robot.where ? ` ${robot.where}` : ''}${robot.holding ? `, gripper holding ${robot.holding}` : ''}`, { view: 'robot' });
      return okr(`robot ok, parked${inventoryValid ? '' : ' — inventory unverified'}`, { view: 'robot' });
    }
    if (want === 'reset') {
      if (robot.state !== 'jammed') return err('robot is not jammed');
      robot.state = 'ok';
      robot.where = null;
      const lines = ['robot reset: re-homed all axes'];
      if (robot.holding) {
        const i = freeSlotNear(setup.robot?.dropNear ?? 0);
        if (i == null) {
          robot.state = 'jammed';
          return err('robot reset: no free slot to release the held cartridge into');
        }
        slots[i] = robot.holding;
        lines.push(`gripper was holding ${robot.holding}; released it into the nearest free slot`);
        robot.holding = null;
      }
      inventoryValid = false;
      lines.push('inventory is now UNVERIFIED; run `inventory` before any move');
      time += 45;
      return okr(lines.join('\n'), { view: 'robot' });
    }
    return err('usage: robot status | robot reset');
  }

  function inventory() {
    if (robot.state === 'jammed') return err('robot is jammed; run `robot reset` first');
    if (door === 'open') return err('close the I/E door first');
    const diffs = [];
    for (let i = 0; i < slotCount; i++) {
      if (believed[i] !== slots[i]) diffs.push(`slot ${i + 1}: expected ${believed[i] || 'empty'}, found ${slots[i] || 'empty'}`);
    }
    for (let i = 0; i < slotCount; i++) believed[i] = slots[i];
    inventoryValid = true;
    time += (slotCount + ieCount) * C.scanSecondsPerSlot;
    return okr(
      `inventory: scanned ${slotCount} slots, ${ieCount} I/E slots, ${drives.length} drives — ` +
        (diffs.length ? `${diffs.length} difference${diffs.length > 1 ? 's' : ''}:\n  ${diffs.join('\n  ')}` : 'no differences'),
      { diffs: diffs.length },
    );
  }

  function exec(line) {
    const raw = String(line || '').trim();
    const toks = raw.split(/\s+/).filter(Boolean);
    const verb = (toks[0] || '').toLowerCase();
    const args = toks.slice(1);
    let res;
    switch (verb) {
      case '':
        res = err('type a command (try `help`)');
        break;
      case 'help':
        res = okr(HELP.join('\n'), { view: 'help' });
        break;
      case 'status':
        res = status();
        break;
      case 'load':
      case 'mount':
        res = load(args);
        break;
      case 'unload':
      case 'unmount':
        res = unload(args);
        break;
      case 'transfer':
      case 'move':
        res = transfer(args);
        break;
      case 'export':
        res = transfer(args, 'export');
        break;
      case 'import':
        res = transfer(args, 'import');
        break;
      case 'door':
        res = doorCmd(args);
        break;
      case 'clean':
        res = clean(args);
        break;
      case 'tapealert':
        res = tapealert(args);
        break;
      case 'verify':
        res = verify(args);
        break;
      case 'drive':
        res = driveCmd(args);
        break;
      case 'robot':
        res = robotCmd(args);
        break;
      case 'inventory':
        res = inventory();
        break;
      default:
        res = err(`unknown command "${verb}" (try \`help\`)`);
    }
    const entry = { t: Math.round(time), cmd: raw, verb, args: args.map((a) => a.toUpperCase()), ok: res.ok, text: res.text, data: res.data || {} };
    log.push(entry);
    if (log.length > 300) log.splice(0, log.length - 300);
    return entry;
  }

  /* ---- read-only views ------------------------------------------ */
  // where a cartridge physically is right now
  function locate(id) {
    let i = slots.indexOf(id);
    if (i >= 0) return { kind: 'slot', slot: i + 1 };
    i = ie.indexOf(id);
    if (i >= 0) return { kind: 'ie', slot: i + 1 };
    const d = drives.find((x) => x.cart === id);
    if (d) return { kind: 'drive', drive: d.name };
    if (robot.holding === id) return { kind: 'robot' };
    if (offsite.includes(id)) return { kind: 'offsite' };
    return null;
  }

  function view() {
    return {
      time,
      cols,
      slots: slots.slice(),
      believed: believed.slice(),
      ie: ie.slice(),
      door,
      inventoryValid,
      robot: { ...robot },
      offsite: offsite.slice(),
      drives: drives.map((d) => ({ name: d.name, cart: d.cart, online: d.online, flags: [...d.flags].sort((a, b) => a - b), fault: d.fault })),
      carts: [...carts.values()].map((c) => ({ ...c, home: c.home == null ? null : c.home + 1 })),
    };
  }

  return {
    exec,
    view,
    locate,
    drive: (name) => drives.find((d) => d.name === name.toUpperCase()),
    cart: (id) => carts.get(id),
    get log() {
      return log;
    },
    get time() {
      return time;
    },
    get door() {
      return door;
    },
    get inventoryValid() {
      return inventoryValid;
    },
    get robot() {
      return robot;
    },
  };
}

/* ------------------------------------------------------------------ *
 * The five lessons
 *
 * Each step has a goal shown to the learner, a hint (one command that makes
 * progress), and done(lib, hist): true once the goal holds. `hist` is every
 * command run since that step became current. Optional warn(lib, entry) lets a
 * lesson point out a legal-but-bad move without blocking it.
 * ------------------------------------------------------------------ */
const ran = (hist, verb, pred = () => true) => hist.some((e) => e.verb === verb && pred(e));
const at = (lib, id, kind, where) => {
  const l = lib.locate(id);
  return !!l && l.kind === kind && (where === undefined || l.slot === where || l.drive === where);
};
const home = (lib, id) => at(lib, id, 'slot', lib.cart(id).home + 1);
const dataCarts = (n, skip = []) =>
  Array.from({ length: n }, (_, i) => i + 1)
    .filter((i) => !skip.includes(i))
    .map((i) => [i, `CRW${String(i).padStart(3, '0')}L9`]);

export const LESSONS = [
  {
    id: 'mount',
    title: 'Mount and unmount a tape',
    summary: 'Find a cartridge, thread it into a drive, check it reads, and put it back where it lives.',
    setup: { carts: dataCarts(12) },
    brief:
      'A restore job needs CRW003L9. Both drives are empty. Mount it in D1, prove the drive can read it, then return it to its home slot so the next person finds it where the inventory says.',
    steps: [
      {
        goal: 'Look at the library: run `status` and find which slot holds CRW003L9.',
        hint: 'status',
        done: (lib, h) => ran(h, 'status', (e) => e.ok),
      },
      {
        goal: 'Mount CRW003L9 in drive D1.',
        hint: 'load 3 D1',
        done: (lib) => at(lib, 'CRW003L9', 'drive', 'D1'),
      },
      {
        goal: 'Confirm the tape is readable in D1.',
        hint: 'verify D1',
        done: (lib, h) => ran(h, 'verify', (e) => e.ok && e.data.drive === 'D1' && e.data.cart === 'CRW003L9'),
      },
      {
        goal: 'Unmount it: rewind and return CRW003L9 to its home slot (3), leaving D1 empty.',
        hint: 'unload D1',
        done: (lib) => home(lib, 'CRW003L9') && !lib.drive('D1').cart,
      },
    ],
    debrief:
      'Mounting is never "open the file": the robot travels, the drive threads for ~40 s, and unmounting rewinds the whole tape before the robot can take it. Returning to the home slot matters because everyone else trusts the inventory.',
  },
  {
    id: 'cleaning',
    title: 'Run a cleaning cycle',
    summary: 'A drive raises TapeAlert 20. Clear the drive, discover an expired cleaner, clean properly, and get the dead cleaner out.',
    setup: {
      carts: dataCarts(12, [2]).concat([[15, 'CLN001CU', { usesLeft: 0 }], [16, 'CLN002CU', { usesLeft: 50 }]]),
      drives: [{}, { cart: 'CRW002L9', flags: [20] }],
      homes: [['CRW002L9', 2]],
    },
    brief:
      'Overnight jobs on D2 slowed down and the backup software logged a drive alert. Two cleaning cartridges live in slots 15 and 16. Find out what D2 wants and give it that.',
    steps: [
      {
        goal: 'Read D2\'s TapeAlert flags.',
        hint: 'tapealert D2',
        done: (lib, h) => ran(h, 'tapealert', (e) => e.data.drive === 'D2'),
      },
      {
        goal: 'Flag 20 is "Clean now". A drive cannot be cleaned with a data tape in it: unload CRW002L9.',
        hint: 'unload D2',
        done: (lib) => !lib.drive('D2').cart && at(lib, 'CRW002L9', 'slot'),
      },
      {
        goal: 'Clean D2 with the cleaning cartridge in slot 15 (CLN001CU).',
        hint: 'clean D2 15',
        done: (lib, h) => ran(h, 'clean', (e) => e.data.drive === 'D2' && e.data.cart === 'CLN001CU') || lib.drive('D2').flags.size === 0,
      },
      {
        goal: 'That cleaner is spent (flag 22). Clean D2 with the other one, then check its flags are clear.',
        hint: 'clean D2 16',
        done: (lib) => lib.drive('D2').flags.size === 0,
      },
      {
        goal: 'Get the expired CLN001CU out of the library: export it to an I/E slot.',
        hint: 'export 15 IE1',
        done: (lib) => {
          const l = lib.locate('CLN001CU');
          return !!l && (l.kind === 'ie' || l.kind === 'offsite');
        },
      },
    ],
    warn: (lib, e) =>
      e.ok && e.verb === 'load' && lib.drive(e.data.drive)?.flags.has(20)
        ? `${e.data.drive} is still asking to be cleaned (flag 20) — loading data into it risks read errors.`
        : null,
    debrief:
      'TapeAlert turns "the drive feels slow" into a flag number. Clean-now (20) is fixed by a cleaner; an expired cleaner (22) silently does nothing useful, which is why cleaners carry a pass count and why spent ones leave the library instead of sitting in a slot looking helpful.',
  },
  {
    id: 'import-export',
    title: 'Swap tapes through the I/E slots',
    summary: 'Send two cartridges offsite and bring two new ones in — without ever opening the main door.',
    setup: { carts: dataCarts(12), operatorPlaces: ['NEW001L9', 'NEW002L9'] },
    brief:
      'The weekly vault run is here. CRW007L9 and CRW011L9 go offsite, and the courier brought two blank tapes, NEW001L9 and NEW002L9, to load. The library has two I/E (mailslot) slots; the main door stays shut so the robot keeps working.',
    steps: [
      {
        goal: 'Export CRW007L9 to an I/E slot.',
        hint: 'export CRW007L9 IE1',
        done: (lib) => ['ie', 'offsite'].includes(lib.locate('CRW007L9')?.kind),
      },
      {
        goal: 'Export CRW011L9 to the other I/E slot.',
        hint: 'export CRW011L9 IE2',
        done: (lib) => ['ie', 'offsite'].includes(lib.locate('CRW011L9')?.kind),
      },
      {
        goal: 'Open the I/E door so the courier can take both tapes and put the new ones in.',
        hint: 'door open',
        done: (lib) => at(lib, 'CRW007L9', 'offsite') && at(lib, 'CRW011L9', 'offsite'),
      },
      {
        goal: 'Close the I/E door so the library scans what was put in.',
        hint: 'door close',
        done: (lib) => lib.door === 'closed',
      },
      {
        goal: 'Import NEW001L9 and NEW002L9 into empty storage slots (7 and 11 just freed up).',
        hint: (lib) => (at(lib, 'NEW001L9', 'ie') ? `import IE${lib.locate('NEW001L9').slot} 7` : `import IE${lib.locate('NEW002L9')?.slot ?? 2} 11`),
        done: (lib) => at(lib, 'NEW001L9', 'slot') && at(lib, 'NEW002L9', 'slot'),
      },
    ],
    debrief:
      'The I/E station is an airlock: the robot only works one side of it, the operator only the other, and the door decides which. Closing it triggers a scan, so the library learns about new tapes without anyone opening the main door and invalidating the whole inventory.',
  },
  {
    id: 'failing-drive',
    title: 'A failing drive and its TapeAlert flags',
    summary: 'Two different tapes failed in the same drive. Decide whether the drive or the media is at fault, and act on it.',
    setup: {
      carts: dataCarts(12, [5]).concat([[16, 'CLN003CU', { usesLeft: 31 }]]),
      drives: [{ cart: 'CRW005L9', flags: [3, 30], fault: 'hardware' }, {}],
      homes: [['CRW005L9', 5]],
      history: ['02:14 D1 read error on CRW009L9 (job 4411)', '02:31 D1 read error on CRW005L9 (job 4415)'],
    },
    brief:
      'The backup log shows read errors on CRW009L9 and CRW005L9 — both in D1. CRW005L9 is still in D1. Is it the tapes or the drive?',
    steps: [
      {
        goal: 'Read D1\'s TapeAlert flags.',
        hint: 'tapealert D1',
        done: (lib, h) => ran(h, 'tapealert', (e) => e.data.drive === 'D1'),
      },
      {
        goal: 'Flags 3 + 30 (Hardware A) point at the drive. Get CRW005L9 out of D1 before you take D1 down.',
        hint: 'unload D1',
        done: (lib) => !lib.drive('D1').cart,
      },
      {
        goal: 'Take D1 offline so nothing else gets mounted into it.',
        hint: 'drive offline D1',
        done: (lib) => !lib.drive('D1').online,
      },
      {
        goal: 'Prove the media is fine: mount CRW005L9 in D2 and verify it.',
        hint: (lib) => (at(lib, 'CRW005L9', 'drive', 'D2') ? 'verify D2' : 'load CRW005L9 D2'),
        done: (lib, h) => ran(h, 'verify', (e) => e.ok && e.data.drive === 'D2' && e.data.cart === 'CRW005L9'),
      },
      {
        goal: 'Return CRW005L9 to its home slot (5) and leave D2 empty for real work.',
        hint: 'unload D2',
        done: (lib) => home(lib, 'CRW005L9') && !lib.drive('D2').cart,
      },
    ],
    warn: (lib, e) => {
      if (e.verb === 'clean' && e.args[0] === 'D1') return 'Cleaning cannot clear Hardware A (30): the fault is in the drive, not on the heads.';
      if (e.verb === 'verify' && e.args[0] === 'D1') return 'Every read in a faulty drive is another chance to damage the tape in it.';
      return null;
    },
    debrief:
      'One tape failing is a media question; two tapes failing in the same drive is a drive question. TapeAlert separates them — media flags versus hardware flags — and a read test in a known-good drive settles it. Take the drive offline empty, or you strand a cartridge inside it.',
  },
  {
    id: 'robot-jam',
    title: 'Inventory after a robot jam',
    summary: 'The robot jammed mid-move and someone reached in by hand. Recover, re-inventory, and put everything back.',
    setup: {
      // truth: CRW004L9 is in the gripper; CRW010L9 and CRW012L9 were pushed
      // back by hand into each other's slots. The inventory knows neither.
      carts: dataCarts(12, [4, 10, 12]).concat([[10, 'CRW012L9'], [12, 'CRW010L9']]),
      homes: [['CRW004L9', 4], ['CRW010L9', 10], ['CRW012L9', 12]],
      believed: (() => {
        const b = new Array(16).fill(null);
        for (let i = 1; i <= 12; i++) b[i - 1] = `CRW${String(i).padStart(3, '0')}L9`;
        return b;
      })(),
      robot: { state: 'jammed', holding: 'CRW004L9', where: 'between slot 4 and D2', dropNear: 13 },
      history: ['03:02 robot motion error (gripper axis) moving CRW004L9 slot 4 → D2', '03:09 operator opened main door, closed main door'],
    },
    brief:
      'At 03:02 the robot jammed carrying CRW004L9 to D2. The night operator opened the main door to look, pushed two loose cartridges back into slots, and closed it. The library\'s inventory still shows the world as it was at 03:01.',
    steps: [
      {
        goal: 'Find out what state the robot is in.',
        hint: 'robot status',
        done: (lib, h) => ran(h, 'robot', (e) => e.data.view === 'robot') || ran(h, 'status', (e) => e.ok),
      },
      {
        goal: 'Reset the robot. Note where it puts the cartridge it was holding.',
        hint: 'robot reset',
        done: (lib) => lib.robot.state === 'ok',
      },
      {
        goal: 'The inventory can\'t be trusted after a jam and an open door. Rescan it.',
        hint: 'inventory',
        done: (lib) => lib.inventoryValid,
      },
      {
        goal: 'Return CRW004L9 to its home slot (4).',
        hint: 'transfer CRW004L9 4',
        done: (lib) => home(lib, 'CRW004L9'),
      },
      {
        goal: 'The scan found CRW010L9 and CRW012L9 swapped. Put both home (you\'ll need a free slot to swap through).',
        hint: (lib) => {
          // a cartridge whose home is free goes straight home; if both homes
          // are occupied by each other, park one in a free slot first
          for (const [id, slot] of [['CRW010L9', 10], ['CRW012L9', 12]]) {
            if (!home(lib, id) && !lib.view().slots[slot - 1]) return `transfer ${lib.locate(id).slot} ${slot}`;
          }
          const free = lib.view().slots.findIndex((x, i) => !x && i >= 12) + 1;
          return `transfer 10 ${free}`;
        },
        done: (lib) => home(lib, 'CRW010L9') && home(lib, 'CRW012L9') && lib.inventoryValid,
      },
    ],
    warn: (lib, e) =>
      !e.ok && /inventory/.test(e.text) && e.verb !== 'inventory'
        ? 'The library refused to move on an unverified inventory — that refusal is what keeps a tape from being "lost" in plain sight.'
        : null,
    debrief:
      'After a jam the library knows the robot moved but not where everything ended up, and any open door means a human may have touched anything. A full inventory is slow (every barcode, every slot), but it is the only thing that turns "probably" back into "known". Moving tapes before it is how a cartridge gets lost in plain sight.',
  },
];

export const LESSON_IDS = LESSONS.map((l) => l.id);
export const lessonById = (id) => LESSONS.find((l) => l.id === id) || null;

// one complete solution per lesson, as a learner would type it (the tests
// replay these; the UI's hint button derives its suggestion from live state)
export const SOLUTIONS = {
  mount: ['status', 'load 3 D1', 'verify D1', 'unload D1'],
  cleaning: ['tapealert D2', 'unload D2', 'clean D2 15', 'clean D2 16', 'export 15 IE1'],
  'import-export': ['export CRW007L9 IE1', 'export CRW011L9 IE2', 'door open', 'door close', 'import IE1 7', 'import IE2 11'],
  'failing-drive': ['tapealert D1', 'unload D1', 'drive offline D1', 'load CRW005L9 D2', 'verify D2', 'unload D2'],
  'robot-jam': ['robot status', 'robot reset', 'inventory', 'transfer CRW004L9 4', 'transfer 10 15', 'transfer 12 10', 'transfer 15 12'],
};

/* ------------------------------------------------------------------ *
 * A lesson in progress: the library plus the step checker.
 * ------------------------------------------------------------------ */
export function createLesson(id) {
  const lesson = lessonById(id);
  if (!lesson) throw new Error(`unknown lesson "${id}"`);
  const lib = createLibrary(lesson.setup);
  let stepIndex = 0;
  let hist = [];

  function advance() {
    const passed = [];
    while (stepIndex < lesson.steps.length && lesson.steps[stepIndex].done(lib, hist)) {
      passed.push(stepIndex);
      stepIndex++;
      hist = [];
    }
    return passed;
  }

  function run(cmd) {
    if (stepIndex >= lesson.steps.length) {
      const entry = lib.exec(cmd);
      return { entry, passed: [], warning: null, done: true };
    }
    const entry = lib.exec(cmd);
    hist.push(entry);
    const warning = lesson.warn ? lesson.warn(lib, entry) : null;
    const passed = advance();
    return { entry, passed, warning, done: stepIndex >= lesson.steps.length };
  }

  return {
    lesson,
    lib,
    run,
    get stepIndex() {
      return stepIndex;
    },
    get done() {
      return stepIndex >= lesson.steps.length;
    },
    get step() {
      return lesson.steps[stepIndex] || null;
    },
    hint() {
      const s = lesson.steps[stepIndex];
      if (!s) return null;
      return typeof s.hint === 'function' ? s.hint(lib) : s.hint;
    },
  };
}
