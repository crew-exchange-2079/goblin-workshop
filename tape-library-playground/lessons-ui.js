/*
 * lessons-ui.js — the browser side of the guided lessons. The library model and
 * the step checker live in lessons.js; this file draws them, runs the console
 * and remembers which lessons you have finished. No libraries, no network.
 *
 * URL parameters:
 *   ?lesson=cleaning   open that lesson (mount, cleaning, import-export,
 *                      failing-drive, robot-jam)
 */
import { LESSONS, LESSON_IDS, TAPEALERT, createLesson } from './lessons.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const STORE_KEY = 'tape-library-playground.lessons.done';

let current = null;
let lessonId = LESSON_IDS.includes(params.get('lesson')) ? params.get('lesson') : LESSON_IDS[0];
const recall = [];
let recallAt = 0;

/* progress lives in this browser only; a private window just starts fresh */
function loadDone() {
  try {
    const v = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
    return new Set(Array.isArray(v) ? v.filter((x) => LESSON_IDS.includes(x)) : []);
  } catch {
    return new Set();
  }
}
function saveDone(set) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify([...set]));
  } catch {
    /* storage blocked: progress is simply not remembered */
  }
}
const done = loadDone();

/* text with `code` spans, built without innerHTML so lesson text cannot inject markup */
function richText(el, text) {
  el.textContent = '';
  String(text)
    .split(/(`[^`]+`)/)
    .forEach((part) => {
      if (part.startsWith('`') && part.endsWith('`') && part.length > 1) {
        const c = document.createElement('code');
        c.textContent = part.slice(1, -1);
        el.appendChild(c);
      } else if (part) el.appendChild(document.createTextNode(part));
    });
}

/* ---------------- picker ---------------- */
function renderPicker() {
  const list = $('lessonlist');
  list.textContent = '';
  LESSONS.forEach((l, i) => {
    const li = document.createElement('li');
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'lessonbtn' + (l.id === lessonId ? ' current' : '') + (done.has(l.id) ? ' done' : '');
    b.dataset.lesson = l.id;
    if (l.id === lessonId) b.setAttribute('aria-current', 'true');
    const t = document.createElement('span');
    t.className = 'ltitle';
    t.textContent = `${i + 1}. ${l.title}`;
    const s = document.createElement('span');
    s.className = 'lsum';
    s.textContent = l.summary;
    const tick = document.createElement('span');
    tick.className = 'tick';
    tick.textContent = done.has(l.id) ? '✓ done' : '';
    b.append(t, tick, s);
    b.addEventListener('click', () => openLesson(l.id));
    li.appendChild(b);
    list.appendChild(li);
  });
  $('progress').textContent = `${done.size}/${LESSONS.length} done`;
}

/* ---------------- lesson ---------------- */
function openLesson(id) {
  lessonId = id;
  current = createLesson(id);
  const u = new URL(location.href);
  u.searchParams.set('lesson', id);
  history.replaceState(null, '', u);
  $('lessontitle').textContent = current.lesson.title;
  richText($('brief'), current.lesson.brief);
  $('output').textContent = '';
  for (const e of current.lib.log) printLine(e.text, 'past');
  if (current.lib.log.length) printLine('', 'past');
  printLine(`— ${current.lesson.title} — type \`help\` for commands`, 'sys');
  $('feedback').textContent = '';
  $('feedback').className = 'feedback';
  $('debrief').hidden = true;
  renderPicker();
  render();
}

function printLine(text, cls) {
  const out = $('output');
  const span = document.createElement('span');
  span.className = 'ln ' + (cls || '');
  span.textContent = text + '\n';
  out.appendChild(span);
  while (out.childNodes.length > 400) out.removeChild(out.firstChild);
  out.scrollTop = out.scrollHeight;
}

function render() {
  const v = current.lib.view();
  const mm = Math.floor(v.time / 60);
  const ss = Math.round(v.time % 60);
  $('clock').textContent = `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;

  // slots: what the inventory *says*; unverified inventory is drawn as such
  const grid = $('lgrid');
  grid.style.gridTemplateColumns = `repeat(${v.cols}, minmax(0, 1fr))`;
  grid.textContent = '';
  v.believed.forEach((id, i) => {
    const cell = document.createElement('div');
    cell.className = 'lslot' + (id ? ' filled' : '') + (id && /^CLN/.test(id) ? ' cleaner' : '') + (v.inventoryValid ? '' : ' unverified');
    cell.dataset.slot = String(i + 1);
    const n = document.createElement('span');
    n.className = 'sn';
    n.textContent = String(i + 1);
    const c = document.createElement('span');
    c.className = 'sc';
    c.textContent = id || '';
    // phones show the label's number only (CRW004L9 → 004, CLN001CU → CLN1)
    const sh = document.createElement('span');
    sh.className = 'sc short';
    sh.textContent = id ? (/^CLN/.test(id) ? 'CLN' + Number(id.slice(3, 6)) : id.slice(3, 6)) : '';
    cell.title = `slot ${i + 1}: ${id || 'empty'}${v.inventoryValid ? '' : ' (unverified)'}`;
    cell.append(n, c, sh);
    grid.appendChild(cell);
  });

  const ie = $('ieslots');
  ie.textContent = '';
  v.ie.forEach((id, i) => {
    const cell = document.createElement('div');
    cell.className = 'lslot ie' + (id ? ' filled' : '');
    cell.dataset.ie = String(i + 1);
    const n = document.createElement('span');
    n.className = 'sn';
    n.textContent = `IE${i + 1}`;
    const c = document.createElement('span');
    c.className = 'sc';
    c.textContent = id || '';
    cell.append(n, c);
    ie.appendChild(cell);
  });
  $('door').textContent = `door ${v.door}`;
  $('door').classList.toggle('open', v.door === 'open');

  const rb = $('robotbox');
  rb.textContent = v.robot.state === 'jammed' ? `robot JAMMED — holding ${v.robot.holding || 'nothing'}` : 'robot ok';
  rb.classList.toggle('jammed', v.robot.state === 'jammed');

  const dr = $('ldrives');
  dr.textContent = '';
  for (const d of v.drives) {
    const el = document.createElement('div');
    el.className = 'ldrive' + (d.online ? '' : ' offline') + (d.flags.length ? ' alert' : '') + (d.cart ? ' loaded' : '');
    el.dataset.drive = d.name;
    const name = document.createElement('div');
    name.className = 'dname';
    name.textContent = d.name + (d.online ? '' : ' — offline');
    const tape = document.createElement('div');
    tape.className = 'dtape';
    tape.textContent = d.cart || '— empty —';
    const flags = document.createElement('div');
    flags.className = 'dflags';
    flags.textContent = d.flags.length ? d.flags.map((f) => `TA ${f}`).join(' · ') : '';
    flags.title = d.flags.map((f) => `${f}: ${TAPEALERT[f]}`).join('\n');
    el.append(name, tape, flags);
    dr.appendChild(el);
  }

  $('libstate').textContent = v.inventoryValid ? 'inventory verified' : 'inventory UNVERIFIED';
  $('libstate').classList.toggle('bad', !v.inventoryValid);

  // steps
  const sl = $('steplist');
  sl.textContent = '';
  current.lesson.steps.forEach((s, i) => {
    const li = document.createElement('li');
    const state = i < current.stepIndex ? 'passed' : i === current.stepIndex ? 'active' : 'todo';
    li.className = state;
    if (state === 'active') li.setAttribute('aria-current', 'step');
    if (state === 'todo') li.textContent = 'locked until the step above is done';
    else richText(li, s.goal);
    sl.appendChild(li);
  });
  $('stepcount').textContent = `step ${Math.min(current.stepIndex + 1, current.lesson.steps.length)} of ${current.lesson.steps.length}`;
  $('hintbtn').disabled = current.done;
}

function runCommand(cmd) {
  if (!cmd.trim()) return;
  recall.push(cmd);
  recallAt = recall.length;
  printLine(`library> ${cmd}`, 'cmd');
  const r = current.run(cmd);
  printLine(r.entry.text, r.entry.ok ? 'ok' : 'err');
  const fb = $('feedback');
  if (r.passed.length) {
    fb.className = 'feedback good';
    fb.textContent = r.passed.length > 1 ? `✓ steps ${r.passed.map((i) => i + 1).join(', ')} done` : `✓ step ${r.passed[0] + 1} done`;
  } else if (r.warning) {
    fb.className = 'feedback warn';
    fb.textContent = r.warning;
  } else if (!r.entry.ok) {
    fb.className = 'feedback warn';
    fb.textContent = 'The library refused that — read the console line above.';
  } else if (!r.done) {
    fb.className = 'feedback';
    fb.textContent = 'Not this step yet.';
  }
  if (r.warning && r.passed.length) fb.textContent += ' — ' + r.warning;
  if (r.done && r.passed.length) finish();
  render();
}

function finish() {
  done.add(lessonId);
  saveDone(done);
  richText($('debrieftext'), current.lesson.debrief);
  const idx = LESSON_IDS.indexOf(lessonId);
  const next = LESSON_IDS.slice(idx + 1).concat(LESSON_IDS.slice(0, idx)).find((id) => !done.has(id));
  $('nextbtn').hidden = !next;
  $('nextbtn').dataset.next = next || '';
  $('debrief').hidden = false;
  renderPicker();
}

/* ---------------- controls ---------------- */
$('cmdform').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('cmd');
  const cmd = input.value;
  input.value = '';
  runCommand(cmd);
  input.focus();
});
$('cmd').addEventListener('keydown', (e) => {
  if (e.key === 'ArrowUp' && recall.length) {
    e.preventDefault();
    recallAt = Math.max(0, recallAt - 1);
    e.target.value = recall[recallAt];
  } else if (e.key === 'ArrowDown' && recall.length) {
    e.preventDefault();
    recallAt = Math.min(recall.length, recallAt + 1);
    e.target.value = recall[recallAt] || '';
  }
});
$('hintbtn').addEventListener('click', () => {
  const h = current.hint();
  if (!h) return;
  $('cmd').value = h;
  $('feedback').className = 'feedback';
  richText($('feedback'), `Hint: \`${h}\` is in the console — press Run.`);
  $('cmd').focus();
});
$('resetbtn').addEventListener('click', () => openLesson(lessonId));
$('nextbtn').addEventListener('click', (e) => {
  if (e.target.dataset.next) openLesson(e.target.dataset.next);
});

openLesson(lessonId);
