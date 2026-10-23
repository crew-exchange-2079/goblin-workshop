/*
 * ui.js — the browser side. All the logic lives in sim.js; this file only draws
 * it and wires up the controls. Plain ES modules, no libraries, no network.
 *
 * URL parameters (handy for screenshots and for sharing a situation):
 *   ?scenario=big-restore   which scenario to load
 *   ?policy=batch           which policy to start on
 *   ?speed=10               simulated seconds per real second
 *   ?autostart=1            start running instead of waiting for a click
 *   ?t=1500                 fast-forward to this simulated second first (deterministic)
 *   ?compare=1              open the comparison table straight away
 */
import { createSim, comparePolicies, SCENARIOS, SCENARIO_IDS, POLICIES, POLICY_IDS, fmt, secondsToClock } from './sim.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const ROW_PITCH = 4; // must match the geometry in sim.js
const SIM_DT = 0.25; // fixed simulation step, seconds
const paramsValid = (list, v) => list.includes(v) ? v : null;

let scenarioId = paramsValid(SCENARIO_IDS, params.get('scenario')) || 'quiet-morning';
let policyId = paramsValid(POLICY_IDS, params.get('policy')) || 'batch';
let speed = Math.min(60, Math.max(1, Number(params.get('speed')) || 20));
let running = params.get('autostart') === '1';
let sim = null;
let slotEls = [];
let driveEls = [];
let lastEventCount = 0;
let accumulator = 0;
let lastFrame = 0;

/* ---------------- build the controls ---------------- */
for (const id of SCENARIO_IDS) {
  const opt = document.createElement('option');
  opt.value = id;
  opt.textContent = SCENARIOS[id].title;
  $('scenario').appendChild(opt);
}
for (const p of POLICIES) {
  const opt = document.createElement('option');
  opt.value = p.id;
  opt.textContent = p.name;
  $('policy').appendChild(opt);
}
$('scenario').value = scenarioId;
$('policy').value = policyId;
$('speed').value = speed;
$('speedout').textContent = speed + '×';
$('drive-count').textContent = SCENARIOS[scenarioId].drives;

function policyBlurb(id) {
  return POLICIES.find((p) => p.id === id).blurb;
}
$('policyblurb').textContent = policyBlurb(policyId);

/* ---------------- scenario / policy / DOM ---------------- */
function buildLibrary() {
  const grid = $('grid');
  grid.innerHTML = '';
  slotEls = [];
  const s = SCENARIOS[scenarioId];
  grid.style.gridTemplateColumns = `repeat(${s.grid.cols}, minmax(0, 1fr))`;
  for (let i = 0; i < s.grid.cols * s.grid.rows; i++) {
    const cell = document.createElement('div');
    cell.className = 'slot';
    cell.innerHTML = '<span class="cartname"></span><span class="passes"></span>';
    grid.appendChild(cell);
    slotEls.push(cell);
  }
  const drives = $('drives');
  drives.innerHTML = '';
  driveEls = [];
  for (let i = 0; i < s.drives; i++) {
    const d = document.createElement('div');
    d.className = 'drive';
    d.innerHTML =
      '<div class="dname"></div><div class="dtape"></div><div class="dstate"></div><div class="bar"><i></i></div>';
    drives.appendChild(d);
    driveEls.push({
      root: d,
      name: d.querySelector('.dname'),
      tape: d.querySelector('.dtape'),
      state: d.querySelector('.dstate'),
      bar: d.querySelector('.bar > i'),
    });
  }
  $('drive-count').textContent = s.drives;
}

function newSim() {
  sim = createSim({ scenario: scenarioId, policy: policyId });
  lastEventCount = 0;
  accumulator = 0;
  buildLibrary();
  $('scenehint').textContent = SCENARIOS[scenarioId].blurb;
  hideCompare();
  const pre = Number(params.get('t')) || 0;
  if (pre > 0) for (let t = 0; t < pre; t += SIM_DT) sim.step(SIM_DT);
  render(true);
  updatePlayButton();
}

function updatePlayButton() {
  $('playpause').textContent = running ? 'Pause' : 'Play';
  $('runstate').textContent = running ? 'running' : 'paused';
}

/* ---------------- rendering ---------------- */
function robotPixels(v) {
  const stage = $('stage').getBoundingClientRect();
  const grid = $('grid').getBoundingClientRect();
  const driveRow = $('drives').getBoundingClientRect();
  const pad = 10;
  const cols = SCENARIOS[scenarioId].grid.cols;
  const rows = SCENARIOS[scenarioId].grid.rows;
  const innerW = grid.width - pad * 2;
  const cellH = (grid.height - pad * 2) / rows;
  const gridTop = grid.top - stage.top + pad;
  const rowY = (rows - 1) * ROW_PITCH; // sim y of the bottom slot row
  const x = grid.left - stage.left + pad + (v.robot.x / Math.max(1, cols - 1)) * innerW;
  let y;
  if (rows === 1 || v.robot.y <= rowY) {
    // somewhere in the slot grid: line up with the row centres
    y = gridTop + cellH / 2 + (v.robot.y / Math.max(1, rowY)) * (grid.height - pad * 2 - cellH);
  } else {
    // below the grid: stretch the gap over the drive row
    const gridBottom = grid.top - stage.top + grid.height;
    const driveMid = driveRow.top - stage.top + driveRow.height / 2;
    const gapUnits = 8; // matches sim.js: robotHomeRow + DRIVE_Y_OFFSET
    y = gridBottom + ((v.robot.y - rowY) / gapUnits) * (driveMid - gridBottom);
  }
  return { x, y };
}

function render(force) {
  const v = sim.view();
  $('clock').textContent = v.mmss(v.time);
  const st = v.stats;

  $('s-served').textContent = `${st.completed}/${st.generated}`;
  $('s-queue').textContent = st.backlog;
  $('s-mean').textContent = st.meanWait === null ? '—' : fmt(st.meanWait, 0) + 's';
  $('s-p95').textContent = st.p95Wait === null ? '—' : fmt(st.p95Wait, 0) + 's';
  $('s-mounts').textContent = st.mounts;
  $('s-passes').textContent = st.tapePasses;
  $('s-rate').textContent = fmt(st.filesPerHour, 0);
  $('s-robot').textContent = fmt(st.robotBusyFraction * 100, 0) + '%';

  // slots
  const narrow = window.innerWidth < 760 && SCENARIOS[scenarioId].grid.cols >= 7;
  for (let i = 0; i < slotEls.length; i++) {
    const el = slotEls[i];
    const id = v.slots[i];
    const cart = id ? v.carts.find((c) => c.id === id) : null;
    el.className = 'slot' + (id ? ' filled' : '');
    const name = el.firstChild;
    const passes = el.lastChild;
    const label = id ? (narrow ? id.slice(3, 6) : id) : '';
    if (name.textContent !== label) name.textContent = label;
    if (el.title !== (id || '')) el.title = id || '';
    const p = cart ? `×${cart.mounts}` : '';
    if (passes.textContent !== p) passes.textContent = p;
    const busy = cart && cart.loc.kind === 'drive';
    el.classList.toggle('busy', !!busy);
  }

  // drives
  v.drives.forEach((d, i) => {
    const el = driveEls[i];
    if (!el) return;
    el.root.className = 'drive ' + d.state + (d.down ? ' down' : '');
    el.name.textContent = d.down ? `${d.name} — OFFLINE` : `${d.name} · mounts ${d.mounts} · reads ${d.reads}`;
    el.tape.textContent = d.cart ? d.cart + '  @' + Math.round(d.headPos * 100) + '% of tape' : '— empty —';
    el.state.textContent = d.down ? 'failed' : d.state;
    el.bar.style.width = Math.round(d.progress * 100) + '%';
  });

  // robot: with reduced motion we still show where it is, just without the glide
  const p = robotPixels(v);
  const robot = $('robot');
  robot.style.transform = `translate(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px)`;
  robot.classList.toggle('carrying', v.robot.state !== 'idle');
  const label = robot.querySelector('.robotlabel');
  const rl = v.robot.state === 'idle' ? '' : `${v.robot.state} ${v.robot.label}`;
  if (label.textContent !== rl) label.textContent = rl;

  // queue
  const q = $('queue');
  const rows = v.pending.slice(0, 8);
  const html =
    rows
      .map(
        (r) =>
          `<li class="${r.priority === 'urgent' ? 'urgent' : ''}"><span class="qpos">${r.id}</span>` +
          `<span class="qfile">${r.cart} ${r.file}</span>` +
          `<span class="qpos">${r.priority === 'urgent' ? 'URGENT' : r.label}</span>` +
          `<span class="qpos">${Math.round(v.time - r.arrival)}s</span></li>`,
      )
      .join('') || '<li class="empty">queue is empty</li>';
  if (q.dataset.sig !== html) {
    q.innerHTML = html;
    q.dataset.sig = html;
  }

  // events
  const el = $('events');
  if (v.events.length !== lastEventCount || force) {
    lastEventCount = v.events.length;
    el.innerHTML = v.events
      .slice(-14)
      .reverse()
      .map((e) => `<li><span class="t">${e.t}s</span><span class="m ${e.kind}">${e.text}</span></li>`)
      .join('');
  }

  if (sim.time >= SCENARIOS[scenarioId].horizonMinutes * 60) {
    running = false;
    $('runstate').textContent = 'horizon reached';
    $('playpause').textContent = 'Play';
  }
}

/* ---------------- the loop ---------------- */
function frame(now) {
  const dtReal = lastFrame ? Math.min(0.25, (now - lastFrame) / 1000) : 0;
  lastFrame = now;
  if (running) {
    accumulator += dtReal * speed;
    let steps = 0;
    while (accumulator >= SIM_DT && steps < 400) {
      sim.step(SIM_DT);
      accumulator -= SIM_DT;
      steps++;
    }
  }
  render(false);
  requestAnimationFrame(frame);
}

/* ---------------- controls ---------------- */
$('scenario').addEventListener('change', (e) => {
  scenarioId = e.target.value;
  newSim();
});
$('policy').addEventListener('change', (e) => {
  policyId = e.target.value;
  $('policyblurb').textContent = policyBlurb(policyId);
  newSim();
});
$('playpause').addEventListener('click', togglePlay);
$('reset').addEventListener('click', () => newSim());
$('step').addEventListener('click', () => {
  running = false;
  updatePlayButton();
  for (let t = 0; t < 60; t += SIM_DT) sim.step(SIM_DT);
  render(true);
});
$('speed').addEventListener('input', (e) => setSpeed(Number(e.target.value), false));
$('compare').addEventListener('click', () => showCompare(true));
$('compareclose').addEventListener('click', () => hideCompare(true));

// Screen readers get no visual cue when a shortcut changes something, so say it.
function announce(text) {
  $('announce').textContent = text;
}
function togglePlay() {
  running = !running;
  updatePlayButton();
}
function setSpeed(v, fromKey) {
  speed = Math.min(60, Math.max(1, v));
  $('speed').value = speed;
  $('speedout').textContent = speed + '×';
  if (fromKey) announce(`speed ${speed}×`);
}

document.addEventListener('keydown', (e) => {
  // leave browser and OS shortcuts (ctrl+F, cmd+1, ...) alone
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === 'Escape') {
    if (!$('comparepanel').hidden) {
      e.preventDefault();
      hideCompare(true);
    }
    return;
  }
  // form controls own their keys: arrows in a select, space on a focused button
  const tag = e.target.tagName;
  if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (e.key === ' ') {
    if (tag === 'BUTTON') return;
    e.preventDefault();
    togglePlay();
    announce(running ? 'running' : 'paused');
  } else if (e.key === ']') {
    setSpeed(speed + 5, true);
  } else if (e.key === '[') {
    setSpeed(speed - 5, true);
  } else if ('1234'.includes(e.key)) {
    scenarioId = SCENARIO_IDS[Number(e.key) - 1];
    $('scenario').value = scenarioId;
    newSim();
    announce(`scenario: ${SCENARIOS[scenarioId].title}`);
  } else if ('fbpu'.includes(e.key.toLowerCase())) {
    const map = { f: 'fifo', b: 'batch', p: 'position', u: 'priority' };
    policyId = map[e.key.toLowerCase()];
    $('policy').value = policyId;
    $('policyblurb').textContent = policyBlurb(policyId);
    newSim();
    announce(`policy: ${POLICIES.find((p) => p.id === policyId).name}`);
  } else if (e.key.toLowerCase() === 'c') {
    showCompare(true);
  }
});

/* ---------------- compare table ---------------- */
// Hiding the panel while focus is inside it would drop focus to <body>; hand it
// back to the button that opened the panel instead.
function hideCompare(restoreFocus) {
  const panel = $('comparepanel');
  if (panel.hidden) return;
  const hadFocus = panel.contains(document.activeElement);
  panel.hidden = true;
  if (restoreFocus || hadFocus) $('compare').focus();
}

function showCompare(moveFocus) {
  const rows = comparePolicies(scenarioId);
  const best = rows.reduce((a, b) =>
    b.completed > a.completed || (b.completed === a.completed && b.meanWait < a.meanWait) ? b : a,
  );
  const head = ['policy', 'done', 'left', 'mean wait', 'p95', 'urgent p95', 'mounts', 'files/h'];
  const cells = (r) => [
    r.name,
    `${r.completed}/${r.generated}`,
    r.backlog,
    fmt(r.meanWait, 0) + 's',
    fmt(r.p95Wait, 0) + 's',
    fmt(r.urgentP95Wait, 0) + 's',
    r.mounts,
    fmt(r.filesPerHour, 0),
  ];
  $('comparetable').innerHTML =
    '<thead><tr>' + head.map((h) => `<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
    rows
      .map(
        (r) =>
          `<tr class="${r === best ? 'best' : ''}">` +
          cells(r)
            .map((c, i) => `<td>${i === 0 && r === best ? c + ' ★' : c}</td>`)
            .join('') +
          '</tr>',
      )
      .join('') +
    '</tbody>';
  $('comparecaption').textContent =
    `${SCENARIOS[scenarioId].title}, seed ${SCENARIOS[scenarioId].seed}, run headless to the end of the horizon for each policy. ` +
    `Same requests, same library, same costs — only the scheduling rule changes. Worst urgent tail: ` +
    secondsToClock(rows.reduce((a, b) => (b.urgentP95Wait ?? 0) > (a.urgentP95Wait ?? 0) ? b : a).urgentP95Wait) + '.';
  $('comparepanel').hidden = false;
  $('comparepanel').scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'nearest' });
  if (moveFocus) $('comparetitle').focus({ preventScroll: true });
}

newSim();
updatePlayButton();
if (params.get('compare') === '1') showCompare();
// prefers-reduced-motion: the play button and +1 min still drive the simulation,
// but the robot is not animated across the library — you get the numbers instead.
if (reducedMotion) document.body.classList.add('reduced-motion');
requestAnimationFrame(frame);
