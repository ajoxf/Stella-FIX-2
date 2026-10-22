/* The Exchanges page: venues, the three buttons, and contracts.
 *
 * Two rules run through it. A password is typed and sent once; it is never
 * read back, not even masked, because a masked value is one that gets echoed
 * into the form and saved over the real one. And a specification the venue
 * reports is REPORTED, never applied — the operator presses the button.
 */
'use strict';

const state = { venues: [], contracts: [], editing: null, editingContract: null };

/* -- venues -------------------------------------------------------------- */

function venueRow(v) {
  const el = document.createElement('div');
  el.className = 'row' + (v.environment === 'PROD' ? ' prod' : '');
  el.innerHTML =
    '<span class="rname"></span>' +
    '<span class="tag env-tag"></span>' +
    '<span class="rdetail"></span>' +
    '<span class="rpw"></span>';
  el.querySelector('.rname').textContent = v.name;
  const tag = el.querySelector('.env-tag');
  tag.textContent = v.environment;
  tag.classList.add(v.environment === 'PROD' ? 'SELL' : 'BUY');
  el.querySelector('.rdetail').textContent =
    v.host ? v.host + (v.port ? ':' + v.port : '') : 'no endpoint — simulator';
  // Set or not set. Never the value.
  el.querySelector('.rpw').textContent =
    v.password_set ? 'password set' : 'no password';
  el.onclick = () => editVenue(v.name);
  return el;
}

async function loadVenues() {
  state.venues = await getJSON('/api/venues');
  const host = document.getElementById('venue-list');
  host.innerHTML = '';
  if (!state.venues.length) {
    const empty = document.createElement('div');
    empty.className = 'hint';
    empty.textContent = 'No venue yet. Until one exists the system runs ' +
      'against the simulator, and the screen says SIMULATED.';
    host.appendChild(empty);
  }
  state.venues.forEach((v) => host.appendChild(venueRow(v)));
  paintVenueOptions();
  paintCounts();
}

function vField(name) { return document.getElementById('v-' + name); }

const VENUE_TEXT = ['broker', 'host', 'md_host', 'recovery_host', 'sender_comp_id',
  'target_comp_id', 'sender_sub_id', 'on_behalf_of_comp_id', 'username',
  'account', 'data_dictionary', 'fix_version'];
const VENUE_NUM = ['port', 'md_port', 'recovery_port', 'heartbeat_sec'];
const VENUE_BOOL = ['reset_seq_on_logon', 'use_tls', 'enabled'];

function showVenue(v) {
  state.editing = v ? v.name : null;
  document.getElementById('venue-form-card').hidden = false;
  document.getElementById('venue-form-title').textContent =
    v ? v.name : 'New venue';
  document.getElementById('v-name').value = v ? v.name : '';
  document.getElementById('v-name').disabled = !!v;

  const env = (v && v.environment) || '';
  document.querySelectorAll('[name="env"]').forEach((r) => {
    r.checked = r.value === env;
  });
  paintEnvRadios();

  VENUE_TEXT.forEach((k) => { vField(k).value = (v && v[k]) || ''; });
  // A select whose value matches no option renders BLANK, which reads as "no
  // FIX version" rather than "the usual one".
  if (!vField('fix_version').value) vField('fix_version').value = 'FIX.4.4';
  VENUE_NUM.forEach((k) => {
    vField(k).value = (v && v[k] !== null && v[k] !== undefined) ? v[k] : '';
  });
  VENUE_BOOL.forEach((k) => { vField(k).checked = v ? !!v[k] : true; });
  if (!v) vField('heartbeat_sec').value = 30;

  const pw = document.getElementById('v-password');
  pw.value = '';
  pw.placeholder = v && v.password_set ? 'set — leave blank to keep it'
    : 'not set';
  document.getElementById('pw-note').textContent = v
    ? 'stored in .env as ' + v.password_env
    : 'stored in .env, never in config';
  document.getElementById('v-delete').hidden = !v;
  document.getElementById('diag-card').hidden = true;
}

function paintEnvRadios() {
  const chosen = document.querySelector('[name="env"]:checked');
  const value = chosen ? chosen.value : '';
  document.getElementById('lbl-uat').classList.toggle('sel-uat', value === 'UAT');
  document.getElementById('lbl-prod').classList.toggle('sel-prod', value === 'PROD');
}
document.querySelectorAll('[name="env"]').forEach((r) => {
  r.onchange = paintEnvRadios;
});

function editVenue(name) {
  showVenue(state.venues.find((v) => v.name === name));
}

document.getElementById('new-venue').onclick = () => showVenue(null);

document.getElementById('v-save').onclick = async () => {
  const name = document.getElementById('v-name').value.trim();
  if (!name) { toast('REJECT', 'NOT SAVED', 'a name is required'); return; }
  const chosen = document.querySelector('[name="env"]:checked');
  const body = { environment: chosen ? chosen.value : '' };
  VENUE_TEXT.forEach((k) => { body[k] = vField(k).value.trim(); });
  VENUE_NUM.forEach((k) => {
    body[k] = vField(k).value === '' ? null : Number(vField(k).value);
  });
  VENUE_BOOL.forEach((k) => { body[k] = vField(k).checked; });
  const pw = document.getElementById('v-password').value;
  if (pw) body.password = pw;              // blank leaves the stored one alone

  // Turning a venue live is a decision, and it is made once, out loud.
  if (body.environment === 'PROD') {
    const ok = await ask('Save ' + name + ' as PROD?',
      'PROD is a live account. Every badge on the screen turns red and the ' +
      'algo will send real orders through it. UAT and PROD are separate ' +
      'venues with separate credentials.', 'Save as PROD');
    if (!ok) return;
  }

  const { data } = await postJSON('/api/venues/' + encodeURIComponent(name), body);
  if (!data.ok) {
    toast('REJECT', 'NOT SAVED', data.error || 'the venue was refused');
    return;
  }
  document.getElementById('v-password').value = '';
  toast('OK', 'SAVED', name + ' saved');
  await loadVenues();
  editVenue(name);
};

document.getElementById('v-delete').onclick = async () => {
  if (!state.editing) return;
  const ok = await ask('Delete ' + state.editing + '?',
    'The venue is removed from config.json. Its password stays in .env ' +
    'until you remove it by hand.', 'Delete');
  if (!ok) return;
  const { data } = await postJSON(
    '/api/venues/' + encodeURIComponent(state.editing), undefined, 'DELETE');
  if (!data.ok) {
    toast('REJECT', 'NOT DELETED', data.error || 'refused');
    return;
  }
  document.getElementById('venue-form-card').hidden = true;
  await loadVenues();
};

/* -- the three buttons --------------------------------------------------- */

// A Logon takes TT up to 20 s to answer, and Connect reports back within 3.
// While the session is still CONNECTING the page reads the status again
// every two seconds until TT accepts or refuses, rather than leaving a
// handshake in progress on screen as a failure.
const FOLLOW_EVERY_MS = 2000;
const FOLLOW_MAX = 15;
let followTimer = null;
let followCount = 0;

async function runAction(action, following) {
  if (followTimer) { clearTimeout(followTimer); followTimer = null; }
  if (!following) followCount = 0;
  if (!state.editing) {
    toast('REJECT', 'NO VENUE', 'Save the venue first.');
    return;
  }
  const card = document.getElementById('diag-card');
  const rows = document.getElementById('diag-rows');
  card.hidden = false;
  rows.textContent = action + '…';
  const body = await getJSON('/api/venues/' +
    encodeURIComponent(state.editing) + '/' + action);
  const label = action === 'test' ? 'Status check' : action;
  document.getElementById('diag-when').textContent =
    label + ' · ' + new Date().toLocaleTimeString([], { hour12: false }) +
    (body.simulated ? ' · SIMULATED' : '');
  const help = document.getElementById('diag-help');
  help.hidden = action !== 'test';
  help.textContent = action === 'test'
    ? 'Status check only: this reads the engine’s existing FIX state and does not send a Logon. Use Connect to request a new session.'
    : '';
  rows.innerHTML = '';
  (body.rows || []).forEach((r) => {
    const line = document.createElement('div');
    line.className = 'dline ' + (r.ok ? 'ok' : r.pending ? 'wait' : 'bad');
    const mark = r.ok ? 'OK  ' : r.pending ? 'WAIT' : 'FAIL';
    line.innerHTML = '<b></b><span class="dcheck"></span>' +
      '<span class="ddetail"></span>';
    line.querySelector('b').textContent = mark;
    line.querySelector('.dcheck').textContent = r.check;
    line.querySelector('.ddetail').textContent = r.detail;
    rows.appendChild(line);
    // Every failure carries the step that fixes it.
    if (!r.ok && r.fix) {
      const fix = document.createElement('div');
      fix.className = 'dfix';
      fix.textContent = '→ ' + r.fix;
      rows.appendChild(fix);
    }
  });
  paintLink(body);
  const pending = body.pending || (body.rows || []).some((r) => r.pending);
  if (pending && followCount < FOLLOW_MAX) {
    followCount += 1;
    followTimer = setTimeout(() => runAction('test', true), FOLLOW_EVERY_MS);
  }
}

document.getElementById('v-connect').onclick = () => runAction('connect');
document.getElementById('v-test').onclick = () => runAction('test');
document.getElementById('v-diagnose').onclick = () => runAction('diagnose');

function paintLink(body) {
  const link = document.getElementById('link-line');
  if (body.ok) {
    link.textContent = body.simulated ? 'SIMULATED' : 'CONNECTED';
    link.className = 'link ' + (body.simulated ? 'part' : 'ok');
    return;
  }
  const waiting = (body.rows || []).find((r) => r.pending);
  if (waiting) {
    link.textContent = 'CONNECTING… ' + waiting.detail;
    link.className = 'link part';
    return;
  }
  // One line naming the single thing standing in the way.
  const first = (body.rows || []).find((r) => !r.ok);
  link.textContent = first ? first.check + ': ' + first.detail : 'not connected';
  link.className = 'link bad';
}

/* -- contracts ----------------------------------------------------------- */

function paintVenueOptions() {
  const sel = document.getElementById('c-venue');
  const current = sel.value;
  sel.innerHTML = '<option value="">— none —</option>';
  state.venues.forEach((v) => {
    const o = document.createElement('option');
    o.value = v.name;
    o.textContent = v.name + ' (' + v.environment + ')';
    sel.appendChild(o);
  });
  sel.value = current;
}

function paintCounts() {
  document.getElementById('crumb-counts').textContent =
    state.venues.length + (state.venues.length === 1 ? ' venue' : ' venues') +
    ' · ' + state.contracts.length +
    (state.contracts.length === 1 ? ' contract' : ' contracts');
}

const num = (v, d) => (v === null || v === undefined) ? DASH : Number(v).toFixed(d);

async function loadContracts() {
  state.contracts = await getJSON('/api/contracts');
  const body = document.getElementById('contract-rows');
  body.innerHTML = '';
  state.contracts.forEach((c) => {
    const tr = document.createElement('tr');
    const venueSrc = c.spec_source || {};
    const cells = [
      ['txt', c.name || c.key],
      ['', c.symbol || DASH],
      // A contract with no venue runs against the simulator. Naming a venue
      // that is not in the list above reads as a broken reference.
      ['txt', c.venue || 'simulator'],
      // A number the venue reported and one the operator typed must be
      // visually distinguishable.
      ['r' + (venueSrc.tick_size === 'operator' ? ' override' : ''), num(c.tick_size, 4)],
      ['r' + (venueSrc.tick_value === 'operator' ? ' override' : ''), num(c.tick_value, 2)],
      ['r', num(c.contract_multiplier, 0)],
      ['', c.currency || DASH],
      ['r', num((c.effective || {}).quantity, 0)],
      ['', (c.session_open && c.session_close)
        ? c.session_open + '–' + c.session_close : DASH],
      ['', c.enabled ? 'yes' : 'no'],
    ];
    cells.forEach(([cls, text]) => {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = text;
      tr.appendChild(td);
    });
    const act = document.createElement('td');
    act.className = 'r';
    const edit = document.createElement('button');
    edit.className = 'btn sm';
    edit.textContent = 'Edit';
    edit.onclick = () => showContract(c);
    const del = document.createElement('button');
    del.className = 'btn sm danger';
    del.textContent = 'Delete';
    del.onclick = () => deleteContract(c);
    act.appendChild(edit);
    act.appendChild(del);
    tr.appendChild(act);
    body.appendChild(tr);
  });
  paintCounts();
}

const C_TEXT = ['name', 'symbol', 'security_id', 'security_exchange',
  'currency', 'session_open', 'session_close'];
const C_NUM = ['tick_size', 'tick_value', 'contract_multiplier', 'min_qty',
  'qty_step', 'max_qty', 'decimals'];

function cField(k) { return document.getElementById('c-' + k); }

function showContract(c) {
  state.editingContract = c ? c.key : null;
  document.getElementById('contract-form-card').hidden = false;
  document.getElementById('contract-form-title').textContent =
    c ? (c.name || c.key) : 'New contract';
  C_TEXT.forEach((k) => { cField(k).value = (c && c[k]) || ''; });
  C_NUM.forEach((k) => {
    cField(k).value = (c && c[k] !== null && c[k] !== undefined) ? c[k] : '';
  });
  if (!c) cField('decimals').value = 4;
  cField('venue').value = (c && c.venue) || '';
  cField('enabled').checked = c ? !!c.enabled : true;
  document.getElementById('c-read').disabled = !c;
  document.getElementById('spec-compare').hidden = true;
}

document.getElementById('new-contract').onclick = () => showContract(null);
document.getElementById('c-cancel').onclick = () => {
  document.getElementById('contract-form-card').hidden = true;
};

document.getElementById('c-save').onclick = async () => {
  const body = {};
  C_TEXT.forEach((k) => { body[k] = cField(k).value.trim(); });
  C_NUM.forEach((k) => {
    body[k] = cField(k).value === '' ? '' : Number(cField(k).value);
  });
  body.venue = cField('venue').value;
  body.enabled = cField('enabled').checked;

  let res;
  if (state.editingContract) {
    res = await postJSON('/api/contracts/' +
      encodeURIComponent(state.editingContract), body);
  } else {
    res = await postJSON('/api/contracts', body);
  }
  if (!res.data.ok) {
    toast('REJECT', 'NOT SAVED', res.data.error || 'refused');
    return;
  }
  // An EDIT is adopted by the running engine; a NEW contract is not — the
  // engine subscribes and sizes its contracts when it starts.
  toast('OK', 'SAVED', (body.name || body.symbol) + (state.editingContract
    ? ' saved — in force within a few seconds'
    : ' saved — restart the engine (Ctrl+C, then run_fix.bat) and its ' +
      'window appears on the Algo desk'));
  document.getElementById('contract-form-card').hidden = true;
  await loadContracts();
};

async function deleteContract(c) {
  const ok = await ask('Delete ' + (c.name || c.key) + '?',
    'The contract is removed and its window disappears. Nothing at the venue ' +
    'is touched.', 'Delete');
  if (!ok) return;
  const { data } = await postJSON('/api/contracts/' +
    encodeURIComponent(c.key), undefined, 'DELETE');
  if (!data.ok) {
    // The refusal names the position rather than saying "cannot delete".
    toast('REJECT', 'NOT DELETED', data.error || 'refused');
    return;
  }
  await loadContracts();
}

document.getElementById('c-read').onclick = async () => {
  if (!state.editingContract) return;
  const { data } = await postJSON('/api/contracts/' +
    encodeURIComponent(state.editingContract) + '/read-from-venue', {});
  const panel = document.getElementById('spec-compare');
  panel.hidden = false;
  panel.innerHTML = '';
  if (!data.ok) {
    panel.innerHTML = '<div class="hint bad-hint"></div>';
    panel.querySelector('.hint').textContent = data.error;
    return;
  }
  const head = document.createElement('div');
  head.className = 'hint';
  head.innerHTML = 'The venue says this. <b>Nothing has been applied</b> — ' +
    'a specification changed under a running desk is every money figure on ' +
    'that window changing without anybody being told.';
  panel.appendChild(head);

  // A green comparison against the simulator is not a check, and must not
  // read as one: it was built from this very configuration.
  if (data.note) {
    const warn = document.createElement('div');
    warn.className = 'hint sim-note';
    warn.innerHTML = '<b>SIMULATED</b> — ' + data.note;
    panel.appendChild(warn);
  }

  const table = document.createElement('table');
  table.className = 'grid';
  table.innerHTML = '<thead><tr><th>Field</th><th class="r">Venue</th>' +
    '<th class="r">Config</th><th></th></tr></thead><tbody></tbody>';
  const body = table.querySelector('tbody');
  data.fields.forEach((f) => {
    const tr = document.createElement('tr');
    if (!f.agrees) tr.className = 'disagrees';
    tr.innerHTML = '<td class="txt"></td><td class="r"></td>' +
      '<td class="r"></td><td class="r"></td>';
    const tds = tr.querySelectorAll('td');
    tds[0].textContent = f.field.replace(/_/g, ' ');
    tds[1].textContent = f.venue === null || f.venue === undefined ? DASH : f.venue;
    tds[2].textContent = f.config === null || f.config === undefined ? DASH : f.config;
    if (!f.agrees && f.venue !== null && f.venue !== undefined) {
      const use = document.createElement('button');
      use.className = 'btn sm';
      use.textContent = 'Use the venue’s';
      use.onclick = () => {
        cField(f.field).value = f.venue;
        use.disabled = true;
        toast('OK', 'FILLED IN', f.field + ' set to ' + f.venue +
          ' — press Save contract to keep it');
      };
      tds[3].appendChild(use);
    }
    body.appendChild(tr);
  });
  panel.appendChild(table);
};

/* -- go ------------------------------------------------------------------ */

/* A contract picked in the instrument explorer arrives here as a query
   string. It only FILLS the form: nothing is saved until Save is pressed. */
function prefillFromExplorer() {
  const q = new URLSearchParams(window.location.search);
  if (q.get('prefill') !== '1') return;
  showContract(null);
  C_TEXT.concat(C_NUM).forEach((k) => {
    if (q.has(k) && q.get(k) !== '') cField(k).value = q.get(k);
  });
  // As many decimals as the tick has: a tick of 1 shows 8825, not 8825.0000.
  const tick = q.get('tick_size') || '';
  if (tick && !q.get('decimals')) {
    const dot = tick.indexOf('.');
    cField('decimals').value = dot < 0 ? 0 : tick.length - dot - 1;
  }
  // One venue configured is the only sensible choice; more than one is the
  // trader's to make.
  const venues = [...cField('venue').options].map((o) => o.value).filter(Boolean);
  if (venues.length === 1) cField('venue').value = venues[0];
  document.getElementById('contract-form-title').textContent =
    'New contract from TT — check it, confirm the venue, then Save';
  document.getElementById('contract-form-card').scrollIntoView({block: 'start'});
}

(async function load() {
  await loadVenues();
  await loadContracts();
  prefillFromExplorer();
})();
