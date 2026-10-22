/* The Settings page: read the desk-wide settings, write them back.
 *
 * It reads what the ENGINE achieved beside what was asked for, because a
 * refresh interval nobody meets is a number that reads as a promise.
 */
'use strict';

const FIELDS = () => Array.from(document.querySelectorAll('[data-key]'));
let original = {};

function put(el, value) {
  if (el.type === 'checkbox') el.checked = !!value;
  else if (value === null || value === undefined) el.value = '';
  else el.value = value;
}

function take(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') {
    // Blank means "unset" and 0 does not. A loop that coerces '' to 0 turns
    // an empty guard into a guard set to zero, which is a real instruction.
    return el.value === '' ? null : Number(el.value);
  }
  return el.value;
}

function paint(settings) {
  original = settings;
  FIELDS().forEach((el) => put(el, settings[el.dataset.key]));
}

async function load() {
  paint(await getJSON('/api/settings'));
  try {
    const snap = await getJSON('/api/snapshot');
    const engine = snap.engine || {};
    const note = document.getElementById('loop-achieved');
    // What it actually does, beside what it was asked for.
    note.innerHTML = 'Achieved: <b>' +
      (engine.loop_ms === undefined ? DASH : engine.loop_ms + ' ms') +
      '</b> engine, snapshot <b>' +
      (engine.snapshot_age_sec === null || engine.snapshot_age_sec === undefined
        ? DASH : engine.snapshot_age_sec + ' s') + '</b> old.' +
      (engine.alive === false
        ? ' <b>The engine is not publishing.</b>' : '');
  } catch (e) { /* the page is still usable with the engine down */ }
}

document.getElementById('save').onclick = async () => {
  const body = {};
  FIELDS().forEach((el) => { body[el.dataset.key] = take(el); });
  const { data } = await postJSON('/api/settings', body);
  if (!data.ok) {
    toast('REJECT', 'NOT SAVED', data.error || 'the settings were refused');
    return;
  }
  original = body;
  document.getElementById('saved').textContent =
    'saved ' + new Date().toLocaleTimeString([], { hour12: false });
  toast('OK', 'SAVED', 'Settings written to config.json');

  // Only the two that need it say so, and only when they changed. Warning on
  // every save teaches the operator to ignore the line that matters.
  const note = document.getElementById('restart-note');
  const restart = data.restart_needed || [];
  note.classList.toggle('hidden', restart.length === 0);
  if (restart.length) {
    note.textContent = restart.join(', ') +
      (restart.length === 1 ? ' is read at startup and needs a restart'
        : ' are read at startup and need a restart') +
      ' to take effect. Everything else applied at once.';
  }
};

document.getElementById('revert').onclick = () => {
  paint(original);
  toast('OK', 'REVERTED', 'Back to the last saved values');
};

load();
