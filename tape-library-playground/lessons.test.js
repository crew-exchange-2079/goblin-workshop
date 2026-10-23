/*
 * lessons.test.js — run with:  node --test
 * Checks the guided lessons: every lesson is solvable, the step checker only
 * advances when a goal really holds, and the library refuses the moves an
 * operator must not make.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { LESSONS, LESSON_IDS, SOLUTIONS, TAPEALERT, createLesson, createLibrary, lessonById } from './lessons.js';

const here = dirname(fileURLToPath(import.meta.url));
const play = (id, cmds) => {
  const L = createLesson(id);
  const results = cmds.map((c) => L.run(c));
  return { L, results };
};
const plainLib = () => createLibrary({ carts: Array.from({ length: 12 }, (_, i) => [i + 1, `CRW${String(i + 1).padStart(3, '0')}L9`]) });

/* ---------- the catalogue ---------- */

test('lessons: exactly the five promised lessons, each with steps, hints and a debrief', () => {
  assert.deepEqual(LESSON_IDS, ['mount', 'cleaning', 'import-export', 'failing-drive', 'robot-jam']);
  for (const l of LESSONS) {
    assert.ok(l.title && l.summary && l.brief && l.debrief, `${l.id} is missing text`);
    assert.ok(l.steps.length >= 4, `${l.id} has too few steps`);
    for (const s of l.steps) assert.ok(s.goal && s.hint && typeof s.done === 'function', `${l.id} has a malformed step`);
    assert.ok(Array.isArray(SOLUTIONS[l.id]), `${l.id} has no scripted solution`);
  }
});

test('lessons: an unknown lesson id throws instead of opening something else', () => {
  assert.equal(lessonById('nope'), null);
  assert.throws(() => createLesson('nope'), /unknown lesson/);
});

/* ---------- solvable, and only by doing the work ---------- */

for (const id of LESSON_IDS) {
  test(`${id}: the scripted solution finishes every step`, () => {
    const { L } = play(id, SOLUTIONS[id]);
    assert.equal(L.done, true);
    assert.equal(L.stepIndex, L.lesson.steps.length);
  });

  test(`${id}: following the hint button alone finishes the lesson`, () => {
    const L = createLesson(id);
    for (let n = 0; n < 30 && !L.done; n++) L.run(L.hint());
    assert.equal(L.done, true, `stuck on step ${L.stepIndex + 1}: ${L.step?.goal}`);
    assert.equal(L.hint(), null);
  });

  test(`${id}: no step is already satisfied at the start, and help/status do not finish it`, () => {
    const L = createLesson(id);
    for (const cmd of ['help', 'status', 'help', 'nonsense command']) L.run(cmd);
    assert.equal(L.done, false);
    assert.ok(L.stepIndex <= 1, `status alone advanced ${id} to step ${L.stepIndex + 1}`);
  });

  test(`${id}: the same commands always give the same transcript`, () => {
    const a = play(id, SOLUTIONS[id]).L.lib.log.map((e) => `${e.t}|${e.ok}|${e.text}`);
    const b = play(id, SOLUTIONS[id]).L.lib.log.map((e) => `${e.t}|${e.ok}|${e.text}`);
    assert.deepEqual(a, b);
  });
}

test('checker: goals are checked against state, so a different correct path also passes', () => {
  // mount by barcode instead of slot number, unload to a spare slot, then walk it home
  const { L } = play('mount', ['status', 'load CRW003L9 D1', 'verify D1', 'unload D1 14']);
  assert.equal(L.stepIndex, 3, 'unloading to the wrong slot must not pass the "home slot" step');
  L.run('transfer 14 3');
  assert.equal(L.done, true);
});

test('checker: steps do not advance out of order', () => {
  // verifying before mounting is refused and does not count for step 3 later
  const L = createLesson('mount');
  L.run('verify D1');
  L.run('status');
  assert.equal(L.stepIndex, 1);
  L.run('load 3 D1');
  assert.equal(L.stepIndex, 2, 'the earlier failed verify must not satisfy the verify step');
});

test('checker: commands after a lesson is finished still run but change nothing about completion', () => {
  const { L } = play('mount', SOLUTIONS.mount);
  const r = L.run('load 3 D1');
  assert.equal(r.done, true);
  assert.equal(r.entry.ok, true);
  assert.deepEqual(r.passed, []);
});

/* ---------- mount / unmount ---------- */

test('mount: load, verify and unload cost the documented times', () => {
  const lib = plainLib();
  const t0 = lib.time;
  assert.ok(lib.exec('load 3 D1').ok);
  assert.ok(lib.time - t0 >= 40, 'a mount must include the 40 s thread time');
  const t1 = lib.time;
  assert.ok(lib.exec('unload D1').ok);
  assert.ok(lib.time - t1 >= 25, 'an unmount must include the 25 s rewind');
  assert.equal(lib.locate('CRW003L9').slot, 3);
});

test('mount: the library refuses a full drive, an empty slot, a cleaner and a bad drive name', () => {
  const lib = createLibrary({ carts: [[1, 'CRW001L9'], [2, 'CRW002L9'], [3, 'CLN001CU', { usesLeft: 5 }]] });
  assert.ok(lib.exec('load 1 D1').ok);
  assert.match(lib.exec('load 2 D1').text, /already holds CRW001L9/);
  assert.match(lib.exec('load 9 D2').text, /slot 9 is empty/);
  assert.match(lib.exec('load 3 D2').text, /cleaning cartridge/);
  assert.match(lib.exec('load 2 D7').text, /no drive D7/);
  assert.match(lib.exec('load 2 2').text, /expected a drive/);
  assert.match(lib.exec('load CRW001L9 D2').text, /is in D1; unload it first/);
});

/* ---------- cleaning ---------- */

test('cleaning: cannot clean a drive with a data tape in it', () => {
  const L = createLesson('cleaning');
  const r = L.run('clean D2 16');
  assert.equal(r.entry.ok, false);
  assert.match(r.entry.text, /unload it before cleaning/);
});

test('cleaning: an expired cleaner raises TapeAlert 22 and leaves flag 20 set', () => {
  const { L, results } = play('cleaning', ['unload D2', 'clean D2 15']);
  assert.equal(results[1].entry.ok, false);
  assert.deepEqual(L.lib.drive('D2').flags.has(20), true);
  assert.deepEqual(L.lib.drive('D2').flags.has(22), true);
  assert.equal(L.lib.locate('CLN001CU').slot, 15, 'the rejected cleaner goes back to its slot');
});

test('cleaning: a good cleaner clears the flags and spends one pass', () => {
  const { L } = play('cleaning', ['unload D2', 'clean D2 16']);
  assert.equal(L.lib.drive('D2').flags.size, 0);
  assert.equal(L.lib.cart('CLN002CU').usesLeft, 49);
});

test('cleaning: using the good cleaner first skips the expired-cleaner step rather than trapping the learner', () => {
  const { L } = play('cleaning', ['tapealert D2', 'unload D2', 'clean D2 16']);
  assert.equal(L.stepIndex, 4, 'flags are already clear, so steps 3 and 4 are both satisfied');
});

test('cleaning: a data cartridge offered as a cleaner is rejected with TapeAlert 23', () => {
  const lib = plainLib();
  const r = lib.exec('clean D1 4');
  assert.equal(r.ok, false);
  assert.ok(lib.drive('D1').flags.has(23));
  assert.equal(lib.locate('CRW004L9').slot, 4);
});

test('cleaning: a dirty drive fails a read with TapeAlert 5, and loading into it draws a warning', () => {
  const L = createLesson('cleaning');
  const v = L.run('verify D2');
  assert.equal(v.entry.ok, false);
  assert.ok(L.lib.drive('D2').flags.has(5));
  L.run('unload D2');
  const r = L.run('load 2 D2');
  assert.match(r.warning || '', /asking to be cleaned/);
});

/* ---------- import / export ---------- */

test('import/export: the robot will not touch the I/E station while its door is open', () => {
  const { L, results } = play('import-export', ['export CRW007L9 IE1', 'door open', 'import IE1 7', 'export 8 IE2']);
  assert.equal(results[2].entry.ok, false);
  assert.match(results[2].entry.text, /door is open/);
  assert.equal(results[3].entry.ok, false);
  // storage-to-storage moves are fine with the I/E door open
  assert.ok(L.run('transfer 8 14').entry.ok);
});

test('import/export: opening the door hands exported tapes to the operator and takes in the new ones', () => {
  const { L } = play('import-export', ['export CRW007L9 IE1', 'export CRW011L9 IE2', 'door open']);
  assert.deepEqual(L.lib.view().offsite, ['CRW007L9', 'CRW011L9']);
  assert.deepEqual(L.lib.view().ie, ['NEW001L9', 'NEW002L9']);
});

test('import/export: an imported tape takes the slot it lands in as its home', () => {
  const { L } = play('import-export', SOLUTIONS['import-export']);
  assert.equal(L.lib.cart('NEW001L9').home, 6); // 0-based slot index for slot 7
  assert.equal(L.lib.locate('NEW002L9').slot, 11);
});

test('import/export: export and import insist on the right direction', () => {
  const lib = plainLib();
  assert.match(lib.exec('export 3 14').text, /into an I\/E slot/);
  assert.match(lib.exec('import 3 14').text, /out of an I\/E slot/);
  assert.match(lib.exec('door shut').text, /usage/);
});

/* ---------- failing drive ---------- */

test('failing drive: verify fails in the broken drive and succeeds in a good one', () => {
  const L = createLesson('failing-drive');
  assert.equal(L.run('verify D1').entry.ok, false);
  L.run('unload D1');
  L.run('load CRW005L9 D2');
  assert.equal(L.run('verify D2').entry.ok, true);
});

test('failing drive: cleaning does not clear Hardware A, and the lesson says so', () => {
  const lib = createLibrary({ carts: [[1, 'CLN009CU', { usesLeft: 3 }]], drives: [{ flags: [3, 30], fault: 'hardware' }, {}] });
  const r = lib.exec('clean D1 1');
  assert.equal(r.ok, true);
  assert.ok(lib.drive('D1').flags.has(30));
  assert.match(r.text, /cleaning does not fix this/);
  const L = createLesson('failing-drive');
  L.run('unload D1');
  assert.match(L.run('clean D1 16').warning || '', /Hardware A/);
  assert.ok(L.lib.drive('D1').flags.has(30), 'still flagged after a successful clean');
});

test('failing drive: a drive cannot go offline with a tape inside, and an offline drive takes no mounts', () => {
  const L = createLesson('failing-drive');
  assert.match(L.run('drive offline D1').entry.text, /stranded/);
  L.run('unload D1');
  assert.ok(L.run('drive offline D1').entry.ok);
  assert.match(L.run('load 5 D1').entry.text, /offline/);
  assert.ok(L.run('drive online D1').entry.ok);
});

test('tapealert: flag numbers and names come from the table', () => {
  const L = createLesson('failing-drive');
  const r = L.run('tapealert D1');
  assert.deepEqual(r.entry.data.flags, [3, 30]);
  assert.match(r.entry.text, new RegExp(`flag 30: ${TAPEALERT[30]}`));
  assert.match(L.run('tapealert D2').entry.text, /no TapeAlert flags/);
});

/* ---------- robot jam / inventory ---------- */

test('robot jam: nothing moves while the robot is jammed', () => {
  const L = createLesson('robot-jam');
  for (const cmd of ['load 1 D1', 'transfer 1 15', 'inventory', 'clean D1 1']) {
    const r = L.run(cmd);
    assert.equal(r.entry.ok, false, `${cmd} should be refused`);
    assert.match(r.entry.text, /jammed/);
  }
});

test('robot jam: after a reset the library refuses moves until it has re-inventoried', () => {
  const L = createLesson('robot-jam');
  L.run('robot reset');
  assert.equal(L.lib.inventoryValid, false);
  const r = L.run('transfer 10 15');
  assert.equal(r.entry.ok, false);
  assert.match(r.entry.text, /run `inventory`/);
  assert.match(r.warning || '', /unverified inventory/);
});

test('robot jam: inventory reports every difference and makes belief match the truth', () => {
  const L = createLesson('robot-jam');
  L.run('robot reset');
  const r = L.run('inventory');
  assert.equal(r.entry.data.diffs, 4);
  const v = L.lib.view();
  assert.deepEqual(v.believed, v.slots);
  assert.equal(L.lib.locate('CRW004L9').slot, 14, 'the reset drops the held tape in the nearest free slot');
});

test('robot jam: a stale inventory sends the robot to the wrong place, and it notices', () => {
  // a library whose belief is wrong but not flagged: the first bad fetch invalidates it
  const lib = createLibrary({ carts: [[1, 'CRW002L9'], [2, 'CRW001L9']], believed: ['CRW001L9', 'CRW002L9'] });
  const r = lib.exec('load CRW001L9 D1');
  assert.equal(r.ok, false);
  assert.match(r.text, /inventory mismatch/);
  assert.equal(lib.inventoryValid, false);
  assert.equal(lib.drive('D1').cart, null, 'the wrong tape must not be mounted');
});

/* ---------- conservation ---------- */

test('conservation: no lesson ever loses or duplicates a cartridge', () => {
  for (const id of LESSON_IDS) {
    const L = createLesson(id);
    const ids = L.lib.view().carts.map((c) => c.id);
    for (const cmd of SOLUTIONS[id]) {
      L.run(cmd);
      for (const c of ids) assert.ok(L.lib.locate(c), `${id}: ${c} vanished after "${cmd}"`);
      const v = L.lib.view();
      const placed = [...v.slots, ...v.ie, ...v.drives.map((d) => d.cart), v.robot.holding].filter(Boolean);
      assert.equal(new Set(placed).size, placed.length, `${id}: a cartridge is in two places after "${cmd}"`);
    }
  }
});

/* ---------- module hygiene ---------- */

test('module: lessons.js stays loadable in a browser (no Node-only globals, no randomness)', () => {
  const raw = readFileSync(join(here, 'lessons.js'), 'utf8');
  const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert.ok(!/\bprocess\./.test(src), 'lessons.js touches process');
  assert.ok(!/\brequire\(/.test(src), 'lessons.js uses require');
  assert.ok(!/Math\.random/.test(src), 'lessons.js uses Math.random');
  assert.ok(!/Date\.now|new Date/.test(src), 'lessons.js reads the wall clock');
  assert.ok(!/\bdocument\./.test(src), 'lessons.js touches the DOM');
  assert.ok(!/\bwindow\.|localStorage/.test(src), 'lessons.js touches window or storage');
});
