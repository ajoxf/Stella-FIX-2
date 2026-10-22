/* Order tests: test every order flow on TT UAT.
 *   1  pick a contract
 *   2  check automatically — the program runs each flow (fixtrader/uat.py)
 *      through the same commands the ladder and the Algo window send
 *   3  try it yourself — the desk's own ladder, Algo window and Trading
 *      Monitor, embedded, as they are traded live
 */
'use strict';

const $ = (id) => document.getElementById(id);
const OT = { scenarios: [], env: null, running: false, contractsSeen: '',
             mode: null, sendWord: 'UAT', checks: {}, deskFor: null };
const ICON = { PASS: '✓', FAIL: '✗', SKIP: '–', RUNNING: '…' };

async function command(action, contract, args) {
  const q = await postJSON('/api/command', { action, contract: contract || '', args: args || {} });
  if (!q.data.ok) return q.data;
  const until = Date.now() + 20000;
  while (Date.now() < until) {
    const r = await getJSON('/api/result/' + q.data.id);
    if (!r.pending) return r;
    await new Promise((ok) => setTimeout(ok, 80));
  }
  return { ok: false, error: 'the engine did not answer' };
}

function flowRow(s, result, current) {
  const li = document.createElement('li');
  const status = current ? 'RUNNING' : (result ? result.status : '');
  li.className = status;
  const st = document.createElement('span'); st.className = 'st';
  st.textContent = ICON[status] || '·';
  const body = document.createElement('span');
  const name = document.createElement('b'); name.textContent = s.short;
  const exp = document.createElement('span'); exp.className = 'exp';
  exp.textContent = s.expect || '';
  body.append(name, exp);
  li.title = s.title;
  li.append(st, body);
  if (result || current) {
    const ev = document.createElement('small');
    const word = { PASS: 'Passed', FAIL: 'Failed', SKIP: 'Skipped' }[status] || '';
    ev.textContent = current ? 'running now…' : (word ? word + ': ' : '') +
      String(result.detail || '').replace(/^SKIP: /, '') + (result.at ? '  · ' + result.at : '');
    li.appendChild(ev);
  }
  return li;
}

function groups() {
  return [...document.querySelectorAll('.ot-group')];
}

function paint(body) {
  OT.env = body.environment || null;
  OT.checks = body.checks || {};
  const run = body.run && body.run.running ? body.run : null;
  OT.running = !!run;
  const shown = run || body.last || null;
  // The newest result of every flow, from whichever run; the running one's
  // over it as they land.
  const results = Object.assign({}, (body.last || {}).latest || {});
  delete results.PRE;
  // While a run is going its rows land over the record; the last run's own
  // refusal to start (PRE) is shown until the next run.
  ((run && run.results) || []).forEach((r) => { results[r.id] = r; });
  const refused = ((shown && shown.results) || []).find((r) => r.id === 'PRE');
  if (refused) results.PRE = refused;
  const hits = $('ot-hits').checked;
  groups().forEach((g) => {
    const ul = g.querySelector('.ot-flows');
    ul.textContent = '';
    OT.scenarios.filter((s) => s.kind === g.dataset.kind && s.order_type === g.dataset.type)
      .forEach((s) => {
        const li = flowRow(s, results[s.id], run && run.current === s.id);
        if (s.waits && !hits && !results[s.id]) li.classList.add('optional');
        ul.appendChild(li);
      });
  });
  const pre = $('ot-pre');
  pre.hidden = !results.PRE;
  pre.textContent = results.PRE ? 'Could not start: ' + results.PRE.detail : '';
  $('ot-log').textContent = ((shown && shown.log) || []).join('\n') ||
    (shown ? '' : 'Nothing has been run yet.');
  const uat = OT.env === 'UAT';
  document.querySelectorAll('.ot-check').forEach((b) => { b.disabled = OT.running || !uat; });
  $('ot-stop').disabled = !OT.running;
  $('ot-clear').disabled = OT.running;
  $('ot-run-note').textContent = run ? 'running…' : shown && shown.finished
    ? 'last run ' + shown.finished + ' · ' + (shown.contract || '') : '';
  paintTried();
  const b = $('ot-banner');
  if (uat) {
    b.className = 'ot-banner uat';
    b.textContent = 'TT UAT — orders placed here are REAL orders on TT UAT (a test venue). ' +
      'Nothing goes to a live market.';
  } else if (OT.env === 'PROD') {
    b.className = 'ot-banner prod';
    b.textContent = 'LIVE MARKET — order tests are off here. The last UAT results are kept below.';
  } else {
    b.className = 'ot-banner off';
    b.textContent = 'Not connected to TT UAT (' + (OT.env || 'the engine is not up') +
      ') — start the program on TT UAT to test orders.';
  }
}

/* What the trader has tried by hand: a tick per flow, kept with the time. */
/* What the trader has tried by hand: the same four groups as step 2, a
 * tick per flow, kept with the time. */
function paintTried() {
  const box = $('ot-tried');
  if (box.dataset.built === String(OT.scenarios.length)) {
    box.querySelectorAll('input').forEach((i) => { i.checked = !!(OT.checks[i.value] || {}).result; });
    return;
  }
  box.textContent = '';
  [['manual', 'market', 'Manual · Market'], ['manual', 'limit', 'Manual · Limit'],
   ['algo', 'market', 'Algo · Market'], ['algo', 'limit', 'Algo · Limit']].forEach(([k, t, label]) => {
    const col = document.createElement('div');
    const h = document.createElement('b'); h.textContent = label;
    col.appendChild(h);
    OT.scenarios.filter((s) => s.kind === k && s.order_type === t && s.id !== 'M6').forEach((s) => {
      const l = document.createElement('label');
      const i = document.createElement('input');
      i.type = 'checkbox'; i.value = s.id;
      i.checked = !!(OT.checks[s.id] || {}).result;
      i.onchange = () => postJSON('/api/order-tests/check',
        { id: s.id, result: i.checked ? 'PASS' : '', contract: $('ot-contract').value });
      l.append(i, document.createTextNode(s.short.replace(/^Algo /, '')));
      col.appendChild(l);
    });
    box.appendChild(col);
  });
  box.dataset.built = String(OT.scenarios.length);
}

function paintDesk() {
  const key = $('ot-contract').value;
  if (!key || key === OT.deskFor) return;
  OT.deskFor = key;
  $('ot-desk').src = '/desk?embed=uat&contract=' + encodeURIComponent(key);
}

async function refresh() {
  try {
    const body = await getJSON('/api/order-tests');
    if (!OT.scenarios.length) OT.scenarios = body.scenarios;
    paint(body);
  } catch (e) { /* the next poll tries again */ }
  try {
    const snap = await getJSON('/api/snapshot');
    const contracts = (snap.contracts || []).map((c) => c.key + '|' + (c.name || ''));
    if (contracts.join() !== OT.contractsSeen) {
      OT.contractsSeen = contracts.join();
      const sel = $('ot-contract');
      const was = sel.value;
      sel.textContent = '';
      (snap.contracts || []).forEach((c) => {
        const o = document.createElement('option');
        o.value = c.key;
        o.textContent = `${c.name || c.symbol || c.key} (${c.key})`;
        sel.appendChild(o);
      });
      if (was) sel.value = was;
      paintDesk();
    }
    const ex = (snap.engine || {}).execution || {};
    OT.mode = ex.mode || null;
    OT.sendWord = ex.send_word || 'UAT';
    $('ot-exec').textContent = 'ALGO ORDERS: ' + (ex.mode === 'LIVE' ? 'TT ' + OT.sendWord
      : ex.mode === 'PAPER' ? 'PAPER' : (ex.mode || DASH));
  } catch (e) { /* idem */ }
}

function chosen(kind, type) {
  const hits = $('ot-hits').checked;
  return OT.scenarios.filter((s) => s.kind === kind && s.order_type === type &&
    (hits || !s.waits)).map((s) => s.id);
}

async function run(kind, type) {
  const ids = chosen(kind, type);
  const contract = $('ot-contract').value;
  const qty = $('ot-qty').value;
  const sel = $('ot-contract');
  const name = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : contract;
  if (kind === 'algo' && OT.mode !== 'LIVE') {
    // The Algo's orders go to TT UAT, not filled on paper: the engine's own
    // words, confirmed once.
    const asked = await command('execution', '', { mode: 'LIVE' });
    if (!asked.ok && !asked.confirm) { toast('REJECT', 'Algo orders', asked.error || 'refused'); return; }
    if (asked.confirm) {
      if (!await ask('Send the Algo\'s orders to TT UAT?', asked.text, 'Send to UAT')) return;
      const done = await command('execution', '', { mode: 'LIVE', confirm: true });
      if (!done.ok) { toast('REJECT', 'Algo orders', done.error || 'refused'); return; }
    }
  }
  const what = (kind === 'algo' ? 'the Algo\'s ' : 'manual ') + type + ' order flows';
  const yes = await ask('Check ' + what + ' on ' + name + '?',
    `${ids.length} checks, ${qty} lot(s) per order. Real orders go to TT UAT; each check cancels and closes ` +
    'what it placed, so the contract ends flat. Takes about a minute' +
    ($('ot-hits').checked ? ' — longer with the "wait for a fill" checks.' : '.'), 'Start');
  if (!yes) return;
  const r = await postJSON('/api/order-tests/run', {
    ids, contract, qty, away: $('ot-away').value, hit_wait: $('ot-wait').value, confirm: true });
  if (!r.data.ok) toast('REJECT', 'Order tests', r.data.error || 'refused');
  refresh();
}

groups().forEach((g) => {
  g.querySelector('.ot-check').onclick = () => run(g.dataset.kind, g.dataset.type);
});
$('ot-stop').onclick = async () => {
  const r = await postJSON('/api/order-tests/stop', {});
  toast(r.data.ok ? 'INFO' : 'REJECT', 'Order tests', r.data.text || r.data.error || '');
  refresh();
};
$('ot-clear').onclick = async () => {
  if (!await ask('Clear every result?', 'The ticks and failures on this page are forgotten — ' +
      'automatic and by hand. Nothing is sent to TT.', 'Clear')) return;
  const r = await postJSON('/api/order-tests/clear', {});
  toast(r.data.ok ? 'INFO' : 'REJECT', 'Order tests', r.data.text || r.data.error || '');
  $('ot-tried').dataset.built = '';
  refresh();
};
$('ot-contract').onchange = paintDesk;

refresh();
setInterval(refresh, 1000);
