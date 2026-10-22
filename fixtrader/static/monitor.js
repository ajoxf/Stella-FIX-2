'use strict';
/* The Trading Monitor — the MT5 desk's Trading Monitor, for FIX, on one
 * contract per row. ONE renderer, mounted twice: the Account tab (its own
 * page, polling for itself) and the Trading Monitor window on the Algo desk
 * (fed the desk's own snapshot on every tick).
 *
 * Positions, Working orders, Fills, Closed trades, Slippage and the
 * Reconciler: the Algo's book (from the engine's snapshot and the recorded
 * journal) and the manual ticket's (from the manual terminal), each row
 * saying which it is. Unmeasured is a dash, never 0; a venue that could not
 * be read is said, never shown as flat.
 */
window.TradingMonitor = function (root, opts) {
  opts = opts || {};
  const q = (sel) => root.querySelector(sel);
  const DASH = '—';
  const tabKey = opts.storageKey || 'ft.account.tab';
  const state = { tab: 'positions', snap: null, journal: null, slip: null,
                  journalAt: 0, slipAt: 0, oursOnly: false, showPaper: true };
  try { state.tab = localStorage.getItem(tabKey) || 'positions'; } catch (e) {}
  if (state.tab === 'closed') state.tab = 'analysis';     // folded into Analysis

  const esc = (v) => String(v === null || v === undefined ? '' : v)
    .replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const has = (v) => v !== null && v !== undefined && v !== '' && !Number.isNaN(v);
  const num = (v, d) => has(v) ? Number(v).toFixed(d === undefined ? 4 : d) : DASH;
  const qty = (v) => has(v) ? String(Number(Number(v).toFixed(8))) : DASH;
  const money = (v) => has(v) ? (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : DASH;
  const signed = (v, d) => has(v) ? (v > 0 ? '+' : '') + Number(v).toFixed(d) : DASH;
  const cls = (v) => has(v) ? (v > 0 ? 'up' : v < 0 ? 'dn' : '') : '';
  const clock = (iso) => { if (!iso) return DASH; const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? DASH : t.toLocaleTimeString([], { hour12: false }); };
  const stamp = (iso) => { if (!iso) return DASH; const t = new Date(iso);
    return Number.isNaN(t.getTime()) ? DASH : t.toLocaleDateString([], { month: 'short', day: '2-digit' }) +
      ' ' + t.toLocaleTimeString([], { hour12: false }); };
  const held = (min) => !has(min) ? DASH : min < 60 ? Math.round(min) + 'm'
    : Math.floor(min / 60) + 'h ' + String(Math.round(min % 60)).padStart(2, '0') + 'm';
  const ticks = (t) => has(t) ? (t > 0 ? '+' : '') + Number(t).toFixed(2) + ' t' : DASH;
  const sideTag = (s) => s ? '<span class="side ' + esc(s) + '">' + esc(s) + '</span>' : DASH;
  const origin = (o) => '<span class="origin ' + esc(o) + '">' + esc(o) + '</span>';

  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) throw new Error('Request failed (' + r.status + ')');
    return r.json();
  }

  // Throws on a refusal, with the engine's (or TT's) own words.
  const command = opts.command || async function (action, contract, args) {
    const r = await fetch('/api/command', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, contract: contract || '', args: args || {} }) });
    const queued = await r.json();
    if (!queued.ok) throw new Error(queued.error || 'refused');
    for (const deadline = Date.now() + 15000; Date.now() < deadline;) {
      const res = await getJSON('/api/result/' + encodeURIComponent(queued.id));
      if (!res.pending) { if (!res.ok) throw new Error(res.error || 'refused'); return res; }
      await new Promise((ok) => setTimeout(ok, 60));
    }
    throw new Error('The engine did not answer.');
  };

  // One shared modal: never a native confirm().
  const ask = opts.ask || function (title, body, label) {
    return new Promise((resolve) => {
      const d = document.getElementById('acct-modal');
      document.getElementById('acct-modal-title').textContent = title;
      document.getElementById('acct-modal-body').textContent = body;
      document.getElementById('acct-modal-ok').textContent = label;
      const done = (v) => { d.close(); resolve(v); };
      document.getElementById('acct-modal-ok').onclick = () => done(true);
      document.getElementById('acct-modal-cancel').onclick = () => done(false);
      d.oncancel = () => resolve(false);
      d.showModal();
    });
  };

  const say = opts.say || function (text, bad) {
    const b = document.getElementById('acct-banner');
    if (!b) return;
    b.textContent = text;
    b.className = 'acct-banner' + (bad ? ' bad' : '');
    clearTimeout(say.t);
    say.t = setTimeout(() => { b.className = 'acct-banner hidden'; }, 8000);
  };

  // -- the pieces of the snapshot ------------------------------------------

  const engine = () => (state.snap && state.snap.engine) || {};
  const manual = () => engine().manual_terminal || {};
  const contracts = () => (state.snap && state.snap.contracts) || [];
  const portfolio = () => (state.snap && state.snap.portfolio) || { rows: [] };
  const decimalsOf = (key) => { const c = contracts().find((x) => x.key === key);
    return c && has(c.decimals) ? c.decimals : 4; };
  const nameOf = (key) => { const c = contracts().find((x) => x.key === key);
    return c ? c.name : key; };

  function manualWorking() {
    return (manual().orders || []).filter((o) =>
      ['PENDING_NEW', 'NEW', 'PARTIALLY_FILLED', 'REPLACED', 'PENDING'].includes(o.status));
  }
  function algoWorking() {
    const out = [];
    contracts().forEach((c) => (c.orders || []).forEach((o) => out.push([c, o])));
    return out;
  }

  // -- the cards --------------------------------------------------------------

  function renderCards() {
    const e = engine(), p = portfolio(), pnl = manual().pnl || {};
    const algoOpen = (p.rows || []).filter((r) => r.side).length;
    const manualOpen = (pnl.positions || []).length;
    const brow = document.getElementById('acct-eyebrow');
    if (brow) brow.textContent = (e.environment || 'TT') + ' · ACCOUNT · TRADING MONITOR' +
      (e.alive === false ? ' · ENGINE OFFLINE' : '');
    q('.n-positions').textContent = algoOpen + manualOpen || '';
    q('.n-orders').textContent = algoWorking().length + manualWorking().length || '';
  }

  // -- Accounts -----------------------------------------------------------------

  function accountsPane() {
    const e = engine(), p = portfolio(), m = manual(), pnl = m.pnl || {};
    const ex = e.execution || {};
    const rows = p.rows || [];
    const algoOpen = rows.filter((r) => r.side).length;
    const manualOpen = (pnl.positions || []).length;
    const ps = ex.positions || {};
    const sessions = ((e.fix_connection || {}).sessions || []);
    const cards = [
      ['FIX account (tag 1)', (pnl.account || {}).name || m.account || DASH, ''],
      ['Venue', e.environment || DASH, ''],
      ['Algo orders go to', ex.mode === 'LIVE' ? 'TT ' + (ex.send_word || 'LIVE')
        : ex.mode === 'PAPER' ? 'PAPER (nothing sent)' : (ex.mode || DASH),
        ex.mode === 'LIVE' ? 'live' : 'paper'],
      ['Algos trading', String(contracts().filter((c) => c.algo_state === 'PAPER' ||
        c.algo_state === 'LIVE').length) + ' of ' + contracts().length, ''],
      ['Open positions', String(algoOpen + manualOpen) +
        (manualOpen ? '  (' + algoOpen + ' algo · ' + manualOpen + ' manual)' : ''), ''],
      ['Algo open P&L (net)', money(p.open_pnl), cls(p.open_pnl)],
      ['Algo realised today', money(p.realised_today) + ' · ' + (p.trades_today || 0) +
        ' trades', cls(p.realised_today)],
      ['Manual floating (gross)', money(pnl.floating_total), cls(pnl.floating_total)],
      ['Manual realised (gross)', money(pnl.realized_total), cls(pnl.realized_total)],
      ['Margin in use (configured)', money(p.margin), ''],
      ['TT positions (AN)', ps.status === 'complete' ? 'read from TT'
        : ps.status ? ps.status.toUpperCase() : 'not asked', ps.status === 'complete' ? 'up' : 'warn'],
      ['Venue balance / equity', 'Unavailable', 'warn'],
    ];
    let html = '<div class="acct-cards">' + cards.map(([label, value, c]) =>
      '<div class="acct-card"><span>' + esc(label) + '</span><strong class="' + c + '">' +
      esc(value) + '</strong></div>').join('') + '</div>';
    html += '<table class="mon"><thead><tr><th>FIX session</th><th>Status</th><th>Sender / target</th>' +
      '<th class="r">In seq</th><th class="r">Out seq</th><th>Last heartbeat</th><th>Error</th></tr></thead><tbody>';
    sessions.forEach((x) => {
      html += '<tr><td><b>' + esc(x.name) + '</b></td><td class="' +
        (x.status === 'CONNECTED' ? 'up' : 'warn') + '">' + esc(x.status || DASH) + '</td><td class="mono">' +
        esc((x.sender_comp_id || '') + ' → ' + (x.target_comp_id || '')) + '</td><td class="r">' +
        esc(has(x.in_seq) ? x.in_seq : DASH) + '</td><td class="r">' + esc(has(x.out_seq) ? x.out_seq : DASH) +
        '</td><td>' + esc(clock(x.last_heartbeat)) + '</td><td class="wrap">' + esc(x.error || '') + '</td></tr>';
    });
    if (!sessions.length) html += '<tr><td colspan="7" class="empty">' +
      (e.simulated ? 'The simulator has no FIX sessions.' : 'No FIX session reported.') + '</td></tr>';
    html += '</tbody></table>';
    html += '<p class="tiny pad">' + esc(ps.why || '') + ' Balance and equity are not published on these ' +
      'FIX sessions: they need TT\'s Account/Risk access, and are shown as unavailable rather than as 0.</p>';
    return html;
  }

  // -- Positions ----------------------------------------------------------------

  function venueSays(r) {
    if (!r.venue_readable) return '<span class="warn">could not read</span>';
    if (r.both_sides_open) return '<span class="dn">LONG ' + qty(r.venue_long) +
      ' + SHORT ' + qty(r.venue_short) + '</span>';
    if (!has(r.venue_qty)) return 'nothing';
    return signed(r.venue_qty, 0) + (r.agrees ? ' <span class="up">✓</span>'
      : ' <span class="dn">≠ book</span>');
  }

  /* A close that did NOT happen — refused by TT, or a fill-or-cancel close
   * that found nothing — while the position is still open. First thing on
   * the Positions tab, in red, in TT's words. */
  function closeAlerts() {
    const out = [];
    contracts().forEach((c) => { if (c.close_alert) out.push(c.close_alert.text); });
    (manual().close_alerts || []).forEach((a) => out.push(a.text));
    return out.map((t) => '<div class="mon-check bad mon-closealert">' + esc(t) + '</div>').join('');
  }

  function positionsPane() {
    const p = portfolio(), rows = p.rows || [], pnl = manual().pnl || {};
    const head = '<table class="mon"><thead><tr><th>Contract</th><th>Origin</th><th>Side</th>' +
      '<th class="r">Open / opened</th><th class="r">Avg entry</th><th class="r">Mark</th>' +
      '<th class="r">Open P&amp;L</th><th class="r">Net after costs</th><th>Mode</th>' +
      '<th class="r">Slip</th><th class="r">Entry z</th><th class="r">z now</th>' +
      '<th class="r">Break-even</th><th class="r">Target</th><th class="r">Stop</th>' +
      '<th class="r">Held</th><th>TT says</th><th></th></tr></thead><tbody>';
    let html = closeAlerts() + head, any = false;
    rows.forEach((r) => {
      any = true;
      const d = has(r.decimals) ? r.decimals : 4;
      const tick = r.tick_size || Math.pow(10, -d);
      const slipT = has(r.entry_slippage) ? r.entry_slippage / tick : null;
      const rowCls = r.both_sides_open ? 'hedged' : (r.venue_readable && r.side && !r.agrees) ? 'disagrees' : '';
      html += '<tr class="pos ' + rowCls + '" data-key="' + esc(r.key) + '">' +
        '<td><b>' + esc(r.name) + '</b><div class="tiny">' + esc(r.symbol || '') + '</div></td>' +
        '<td>' + (r.side ? origin(r.paper ? 'PAPER' : 'ALGO') : origin('VENUE')) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td class="r">' + qty(r.qty) + (r.side ? ' / ' + qty(r.opened_qty) : '') + '</td>' +
        '<td class="r">' + num(r.avg_price, d) + '</td>' +
        '<td class="r">' + num(r.mark, d) + '</td>' +
        '<td class="r ' + cls(r.open_pnl) + '">' + money(r.open_pnl) + '</td>' +
        '<td class="r ' + cls(r.net) + '">' + money(r.net) + '</td>' +
        '<td>' + esc(r.entry_order_type || (r.paper ? 'PAPER' : DASH)) + '</td>' +
        '<td class="r ' + (slipT > 0 ? 'dn' : slipT < 0 ? 'up' : '') + '" title="' +
          (r.paper ? 'a PAPER fill is made at the decision price — nothing to measure' :
           'entry fill against the price the Algo decided at; positive is a cost') + '">' +
          ticks(slipT) + '</td>' +
        '<td class="r">' + signed(r.entry_z, 2) + '</td>' +
        '<td class="r">' + signed(r.z_close, 2) + '</td>' +
        '<td class="r">' + num(r.break_even, d) + '</td>' +
        '<td class="r up">' + num(r.target, d) + '</td>' +
        '<td class="r dn">' + num(r.stop, d) + '</td>' +
        '<td class="r">' + held(r.held_min) + '</td>' +
        '<td>' + venueSays(r) + '</td>' +
        '<td>' + (r.side ? '<button type="button" class="danger-btn close-pos" data-key="' +
          esc(r.key) + '" data-name="' + esc(r.name) + '">Close</button>' : '') + '</td></tr>';
      if (r.side) {
        html += '<tr class="detail"><td colspan="18">' +
          'position <b>P' + esc(r.id === null || r.id === undefined ? '?' : r.id) + '</b>' +
          ' · opened ' + esc(stamp(r.opened_at)) +
          ' · tickets <span class="mono">' + esc((r.tickets || []).join(', ') || DASH) + '</span>' +
          ' · margin ' + (has(r.margin_locked) ? money(r.margin_locked) : DASH) +
          ' · touch B ' + num((r.market || {}).bid, d) + ' / A ' + num((r.market || {}).ask, d) +
          (r.simulated ? ' · <span class="warn">simulator</span>' : '') + '</td></tr>';
      }
    });
    (pnl.positions || []).forEach((m) => {
      any = true;
      html += '<tr class="pos manual"><td><b>' + esc(m.instrument) + '</b></td>' +
        '<td>' + origin('MANUAL') + '</td><td>' + sideTag(m.side) + '</td>' +
        '<td class="r">' + qty(m.quantity) + '</td><td class="r">' + qty(m.entry_price) + '</td>' +
        '<td class="r">' + qty(m.mark_price) + '</td>' +
        '<td class="r ' + cls(m.floating_pnl) + '">' + (has(m.floating_pnl)
          ? esc(m.currency || '') + ' ' + money(m.floating_pnl) : DASH) + '</td>' +
        '<td class="r">' + DASH + '</td><td>' + DASH + '</td><td class="r">' + DASH +
        '</td><td class="r">' + DASH + '</td><td class="r">' + DASH + '</td><td class="r">' +
        DASH + '</td><td class="r">' + DASH + '</td><td class="r">' + DASH + '</td><td class="r">' +
        DASH + '</td><td>' + DASH + '</td>' +
        '<td><button type="button" class="danger-btn close-manual" data-id="' + esc(m.entry_order_id) +
          '" data-name="' + esc(m.instrument) + '" title="A closing manual ticket (77=C) for this order\'s open fills, at market — reviewed before it is sent">Close</button></td></tr>' +
        '<tr class="detail"><td colspan="18">manual fills · marked at ' + esc(m.mark_source || DASH) +
        ' · FIX seq ' + esc(m.quote_sequence || DASH) + '</td></tr>';
    });
    if (!any) html += '<tr><td colspan="18" class="empty">Nothing open — the Algo\'s book and the manual ticket are both flat.</td></tr>';
    html += '</tbody>';
    if (rows.length) {
      html += '<tfoot><tr><td colspan="6">' + rows.filter((r) => r.side).length + ' algo open</td>' +
        '<td class="r ' + cls(p.open_pnl) + '">' + money(p.open_pnl) + '</td><td colspan="11">margin ' +
        money(p.margin) + '</td></tr></tfoot>';
    }
    html += '</table>';
    return html + reconcileSummary();
  }

  function reconcileSummary() {
    const p = portfolio(), ex = engine().execution || {};
    const status = ex.positions || {};
    if (!p.venue_readable) {
      return '<div class="mon-check warn">TT\'s positions have not been read' +
        (status.why ? ' — ' + esc(status.why) : '') + '. What is shown is this book alone; ' +
        'it is NOT confirmation that the account is flat.' +
        (ex.positions_waived ? ' Sending to TT ' + (ex.send_word || 'LIVE') + ' was armed on the trader\'s word that this book\'s fills are the record.' : '') +
        '</div>';
    }
    const rows = (p.rows || []);
    const bad = rows.filter((r) => !r.agrees);
    return '<div class="mon-check ' + (bad.length ? 'bad' : 'good') + '">' + (bad.length
      ? bad.length + ' contract(s) where TT and this book disagree — see the Reconciler.'
      : 'TT\'s positions and this book agree on every contract.') + '</div>';
  }

  // -- Working orders -------------------------------------------------------------

  function ordersPane() {
    let html = '<table class="mon"><thead><tr><th>Sent</th><th>Contract</th><th>Origin</th>' +
      '<th>Side</th><th>Intent</th><th class="r">Qty / filled</th><th class="r">Price</th>' +
      '<th>Type</th><th>State</th><th>Closes</th><th>Client ID</th><th>Why / TT says</th>' +
      '<th></th></tr></thead><tbody>';
    let any = false;
    algoWorking().forEach(([c, o]) => {
      any = true;
      html += '<tr><td>' + clock(o.sent_at) + '</td><td><b>' + esc(c.name) + '</b></td><td>' +
        origin(engine().paper ? 'PAPER' : 'ALGO') + '</td><td>' + sideTag(o.side) + '</td><td>' +
        esc(o.intent) + (o.position_effect && o.intent === 'CLOSE' ? ' <span class="tiny">(' +
          esc(o.position_effect) + ')</span>' : '') + '</td>' +
        '<td class="r">' + qty(o.qty) + ' / ' + qty(o.filled_qty) + '</td><td class="r">' +
        (o.order_type === 'MARKET' ? 'MKT' : num(o.price, c.decimals)) + '</td><td>' +
        esc(o.order_type) + (o.escalated ? ' <span class="warn">escalated</span>' : '') +
        '</td><td>' + esc(o.state) + '</td><td>' + (o.position_id ? 'P' + esc(o.position_id) : DASH) +
        '</td><td class="mono">' + esc(o.clordid) + '</td><td class="wrap">' +
        esc(o.text || o.reason || '') + '</td><td><button type="button" class="cancel-algo" data-key="' +
        esc(c.key) + '" data-name="' + esc(c.name) + '">Cancel</button></td></tr>';
    });
    manualWorking().forEach((o) => {
      any = true;
      const t = o.ticket || {}, i = t.instrument || {};
      html += '<tr><td>' + clock(o.updated) + '</td><td><b>' + esc(i.display_name || i.symbol) +
        '</b></td><td>' + origin('MANUAL') + '</td><td>' + sideTag(t.side) + '</td><td>' +
        (o.close_of ? 'CLOSE' : 'OPEN') + '</td><td class="r">' + qty(t.quantity) + ' / ' +
        qty(o.filled_qty) + '</td><td class="r">' + qty(t.price) + '</td><td>' + esc(t.order_type) +
        '</td><td>' + esc(o.status) + (o.pending ? ' · ' + esc(o.pending.kind) + ' requested' : '') +
        '</td><td>' + esc(o.close_of || DASH) + '</td><td class="mono">' + esc(o.id) +
        '</td><td class="wrap">' + esc(o.text || '') + '</td><td>' + (o.pending ? '' :
        '<button type="button" class="cancel-manual" data-id="' + esc(o.id) + '">Cancel</button>') +
        '</td></tr>';
    });
    if (!any) html += '<tr><td colspan="13" class="empty">Nothing working.</td></tr>';
    return html + '</tbody></table>';
  }

  // -- Fills ------------------------------------------------------------------------

  function ttTime(v) {
    // TT's TransactTime (60) is UTC, YYYYMMDD-HH:MM:SS.sss — shown as sent.
    const m = /^(\d{4})(\d{2})(\d{2})-(\d{2}:\d{2}:\d{2}(?:\.\d+)?)/.exec(v || '');
    return m ? m[4] + ' <span class="tiny">(TT)</span>' : esc(v || DASH);
  }

  function timingRow(label, t, note) {
    return '<tr><td>' + esc(label) + '</td><td class="r">' + (t && t.n
      ? 'median ' + Math.round(t.median) + 'ms, worst ' + Math.round(t.worst) + 'ms over ' + t.n +
        ' — measured, not assumed'
      : '<span class="tiny">— nothing measured yet — not zero</span>') +
      (note ? ' <span class="tiny">' + esc(note) + '</span>' : '') + '</td></tr>';
  }

  /* A manual CLOSING fill's P&L, against the ticket it closed (close_of):
   * (fill - entry average) x side, in ticks x the tick value x qty — the
   * instrument's figures as TT defines them NOW (an old ticket may carry
   * figures read before the units were fixed). None where it cannot be
   * priced: a dash, never 0. */
  function manualClosePnl(r) {
    const orders = manual().orders || [];
    const o = orders.find((x) => (x.ids || []).includes(r.clordid) || x.current_id === r.clordid);
    if (!o || !o.close_of) return null;
    const entry = orders.find((x) => x.id === o.close_of);
    if (!entry || !has(entry.avg_price) || !has(r.price)) return null;
    const ticket = entry.ticket || {};
    const sid = String(ticket.security_id || r.security_id || '');
    // The desk contract first — the figures the trader set on the Exchanges
    // page — then the watchlist as TT defines it now. A ticket's own copy
    // was taken when it was sent and may predate a units fix (GC read as a
    // tick of 1, ES as 25): last, never first.
    const desk = contracts().find((c) => String(c.security_id || '') === sid
                                  && Number(c.tick_size) > 0 && Number(c.tick_value) > 0);
    const row = (manual().watchlist || []).find((w) => String((w.instrument || {}).security_id) === sid);
    const inst = desk || (row && row.instrument) || ticket.instrument || {};
    const tick = Number(inst.tick_size), value = Number(inst.tick_value);
    if (!(tick > 0) || !(value > 0)) return null;
    const sign = ticket.side === 'SELL' ? -1 : 1;
    return Math.round((Number(r.price) - Number(entry.avg_price)) * sign / tick * value * Number(r.qty || 0) * 100) / 100;
  }

  function fillsPane() {
    const j = state.journal;
    if (!j) return '<p class="empty">Loading the TT fills…</p>';
    const tm = j.timings || {};
    let html = '<table class="mon timing"><thead><tr><th>What</th><th class="r">Value</th></tr></thead><tbody>' +
      timingRow('order sent → first TT fill (MARKET)', tm.MARKET) +
      timingRow('order sent → first TT fill (LIMIT)', tm.LIMIT, 'includes the time it rested') +
      '</tbody></table>';
    html += '<div class="mon-tools">' +
      '<label class="check"><input type="checkbox" class="ours-only"' + (state.oursOnly ? ' checked' : '') + '> ours only</label>' +
      '<label class="check"><input type="checkbox" class="show-paper"' + (state.showPaper ? ' checked' : '') + '> include PAPER</label>' +
      '<a class="btn-link" href="/api/tt_fills.csv" download>Export CSV</a>' +
      '<a class="btn-link" href="/api/slippage.csv" download>Slippage CSV</a>' +
      '<span class="tiny">Read from TT\'s own execution reports on the Order Routing session — so it carries ' +
      'fills of orders this system did not send too, marked as not ours. Display only: the book is built from OUR fills.</span></div>';
    let rows = (j.tt_fills || []).map((f) => Object.assign({ tt: true }, f));
    if (state.showPaper) {
      (j.fills || []).filter((f) => String(f.exec_id || '').startsWith('PAPER-')).forEach((f) => rows.push({
        tt: false, tt_time: '', received: f.our_ts, account: DASH, name: nameOf(f.contract_key),
        decimals: decimalsOf(f.contract_key), security_id: '', side: f.side, open_close: '',
        qty: f.qty, price: f.price, exec_id: f.exec_id, clordid: f.clordid, order_id: '',
        ours: 'PAPER', text: 'filled here at the live bid/offer — nothing sent to TT', pnl: null }));
      rows.sort((a, b) => String(b.received || '').localeCompare(String(a.received || '')));
    }
    rows.forEach((r) => { if (r.ours === 'MANUAL' && !has(r.pnl)) r.pnl = manualClosePnl(r); });
    if (state.oursOnly) rows = rows.filter((r) => r.ours);
    const total = rows.reduce((a, r) => a + (Number(r.qty) || 0), 0);
    const traded = new Set(rows.map((r) => r.security_id || r.name)).size;
    const pnls = rows.filter((r) => has(r.pnl));
    const pnl = pnls.reduce((a, r) => a + r.pnl, 0);
    html += '<div class="mon-sum"><span>' + rows.length + ' fills</span><span>' + qty(total) + ' lots</span>' +
      '<span>' + traded + ' contract' + (traded === 1 ? '' : 's') + '</span>' +
      '<span>' + rows.filter((r) => r.ours).length + ' ours</span>' +
      '<span>account ' + esc(j.account || DASH) + '</span>' +
      '<span>closing fills\' P&amp;L <b class="' + cls(pnl) + '">' + (pnls.length ? money(pnl) : DASH) + '</b></span></div>';
    html += '<table class="mon fills"><thead><tr><th>TT time (60)</th><th>Account (1)</th><th>Contract</th>' +
      '<th>Security ID (48)</th><th>Side (54)</th><th>In/Out (77)</th><th class="r">Qty (32)</th>' +
      '<th class="r">Price (31)</th><th class="r">Cum / leaves</th><th class="r">P&amp;L</th>' +
      '<th>TT order (37)</th><th>Exec ID (17)</th><th>ClOrdID (11)</th><th>Ours</th><th>Text (58)</th>' +
      '</tr></thead><tbody>';
    rows.slice(0, 400).forEach((r) => {
      const d = has(r.decimals) ? r.decimals : null;
      html += '<tr class="' + (r.ours ? '' : 'foreign') + (r.tt ? '' : ' paperrow') + '">' +
        '<td>' + (r.tt ? ttTime(r.tt_time) : esc(clock(r.received)) + ' <span class="tiny">(here)</span>') + '</td>' +
        '<td>' + esc(r.account || DASH) + '</td>' +
        '<td><b>' + esc(r.name || r.symbol || DASH) + '</b></td>' +
        '<td class="mono">' + esc(r.security_id || DASH) + '</td>' +
        '<td>' + sideTag(r.side) + '</td>' +
        '<td>' + (r.open_close ? '<span class="oc ' + esc(r.open_close) + '">' + esc(r.open_close.toLowerCase()) + '</span>'
          : (r.intent ? esc(r.intent.toLowerCase()) : DASH)) + '</td>' +
        '<td class="r">' + qty(r.qty) + '</td>' +
        '<td class="r">' + (d === null ? qty(r.price) : num(r.price, d)) + '</td>' +
        '<td class="r">' + (has(r.cum_qty) ? qty(r.cum_qty) + ' / ' + qty(r.leaves_qty) : DASH) + '</td>' +
        '<td class="r ' + cls(r.pnl) + '">' + (has(r.pnl) ? money(r.pnl) : (r.intent === 'OPEN' ? '$0.00' : DASH)) + '</td>' +
        '<td class="mono">' + esc(r.order_id || DASH) + '</td>' +
        '<td class="mono">' + esc(r.exec_id || DASH) + '</td>' +
        '<td class="mono">' + esc(r.clordid || DASH) + '</td>' +
        '<td>' + (r.ours ? origin(r.ours) : '<span class="tiny">no</span>') + '</td>' +
        '<td class="wrap">' + esc(r.text || '') + (r.position_id ? ' <span class="tiny">P' + esc(r.position_id) + '</span>' : '') + '</td></tr>';
    });
    if (state.journalError) html += '<tr><td colspan="15" class="empty dn">The fills could not be read: ' +
      esc(state.journalError) + ' — this is not "no fills".</td></tr>';
    else if (!rows.length) html += '<tr><td colspan="15" class="empty">No fills from TT yet.' +
      (engine().paper ? ' The Algo is on PAPER, so it has sent nothing — tick “include PAPER” to see its paper fills.' : '') + '</td></tr>';
    return html + '</tbody></table>';
  }

  // -- Closed trades --------------------------------------------------------------------

  const EXIT_WORDS = { TARGET: 'Take profit', STOP_LOSS: 'Stop loss', MONEY_STOP: 'Stop loss ($)',
    ZSCORE: 'z stop', MEAN: 'Back at the mean', TIME_STOP: 'Time stop', CLOSE_NOW: 'CLOSE ALL (by hand)',
    CLOSE_LIMIT: 'Close @ LMT', KILL_ALL: 'Kill all', SESSION_FLAT: 'Session end', LIMIT_BREACH: 'Limit breach' };

  function analysisSummary(j) {
    // Each closed trade once, with the figure that decides it: the Algo's NET,
    // the manual ticket's gross (it has no configured round trip).
    const trades = (j.closed || []).map((t) => ({ name: t.name,
      origin: t.paper ? 'PAPER' : t.simulated ? 'SIM' : 'ALGO', net: t.net_pnl,
      why: EXIT_WORDS[t.exit_reason] || t.exit_reason || DASH }))
      .concat(((manual().pnl || {}).trades || []).map((t) => ({ name: t.instrument,
        origin: 'MANUAL', net: t.realized_pnl, why: 'Closed by hand' })));
    if (!trades.length) return '';
    const groups = [['All trades', trades]];
    const by = (f, label) => {
      const m = {};
      trades.forEach((t) => { (m[f(t)] = m[f(t)] || []).push(t); });
      Object.keys(m).sort().forEach((k) => groups.push([label + k, m[k]]));
    };
    by((t) => t.origin, 'Origin: ');
    by((t) => t.name, '');
    by((t) => t.why, 'Exit: ');
    let html = '<table class="mon"><thead><tr><th>Group</th><th class="r">Trades</th>' +
      '<th class="r">Won</th><th class="r">Lost</th><th class="r">Win rate</th>' +
      '<th class="r">Net</th><th class="r">Average</th><th class="r">Best</th>' +
      '<th class="r">Worst</th><th class="r">Unmeasured</th></tr></thead><tbody>';
    groups.forEach(([label, g], i) => {
      const nets = g.map((t) => t.net).filter(has);
      const won = nets.filter((n) => n > 0).length, lost = nets.filter((n) => n < 0).length;
      const sum = nets.reduce((a, n) => a + n, 0);
      html += '<tr' + (i === 0 ? ' class="grp"' : '') + '><td>' + (i === 0 ? '<b>' + esc(label) + '</b>' : esc(label)) +
        '</td><td class="r">' + g.length + '</td><td class="r up">' + won + '</td><td class="r dn">' + lost +
        '</td><td class="r">' + (nets.length ? Math.round(100 * won / nets.length) + '%' : DASH) +
        '</td><td class="r ' + cls(sum) + '"><b>' + (nets.length ? money(sum) : DASH) + '</b></td><td class="r">' +
        (nets.length ? money(sum / nets.length) : DASH) + '</td><td class="r up">' +
        (nets.length ? money(Math.max(...nets)) : DASH) + '</td><td class="r dn">' +
        (nets.length ? money(Math.min(...nets)) : DASH) + '</td><td class="r">' + (g.length - nets.length) + '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function closedPane() {
    const j = state.journal;
    if (!j) return '<p class="empty">Loading the journal…</p>';
    let html = (state.tab === 'analysis' ? analysisSummary(j) : '') +
      '<div class="mon-sum"><span><b>Closed trades</b>, newest first</span></div>' +
      '<table class="mon"><thead><tr><th>Closed</th><th>Contract</th><th>Origin</th>' +
      '<th>Side</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Exit</th>' +
      '<th class="r">Entry z</th><th>Why it closed</th><th class="r">Held</th>' +
      '<th class="r">Gross</th><th class="r">Fees</th><th class="r">Net</th>' +
      '<th class="r">Slip in / out</th><th>Tickets</th></tr></thead><tbody>';
    let any = false, net = 0, netN = 0;
    (j.closed || []).forEach((t) => {
      any = true;
      const mins = t.opened_at && t.closed_at ? (Date.parse(t.closed_at) - Date.parse(t.opened_at)) / 60000 : null;
      if (has(t.net_pnl)) { net += t.net_pnl; netN += 1; }
      html += '<tr><td>' + esc(stamp(t.closed_at)) + '</td><td><b>' + esc(t.name) + '</b></td><td>' +
        origin(t.paper ? 'PAPER' : t.simulated ? 'SIM' : 'ALGO') + '</td><td>' + sideTag(t.side) +
        '</td><td class="r">' + qty(t.qty) + '</td><td class="r">' + num(t.entry_price, t.decimals) +
        '</td><td class="r">' + num(t.exit_price, t.decimals) + '</td><td class="r">' +
        signed(t.entry_z, 2) + '</td><td>' + esc(EXIT_WORDS[t.exit_reason] || t.exit_reason || DASH) +
        '</td><td class="r">' + held(mins) + '</td><td class="r ' + cls(t.gross_pnl) + '">' +
        money(t.gross_pnl) + '</td><td class="r">' + money(t.fees) + '</td><td class="r ' +
        cls(t.net_pnl) + '"><b>' + money(t.net_pnl) + '</b></td><td class="r">' + ticks(t.entry_ticks) +
        ' / ' + ticks(t.exit_ticks) + '</td><td class="mono tiny">' +
        esc((t.tickets || []).join(', ') || DASH) + '</td></tr>';
    });
    (manual().pnl && manual().pnl.trades || []).forEach((t) => {
      any = true;
      html += '<tr><td>' + esc(stamp(t.closed_at)) + '</td><td><b>' + esc(t.instrument) +
        '</b></td><td>' + origin('MANUAL') + '</td><td>' + sideTag(t.side) + '</td><td class="r">' +
        qty(t.quantity) + '</td><td class="r">' + qty(t.entry_price) + '</td><td class="r">' +
        qty(t.exit_price) + '</td><td class="r">' + DASH + '</td><td>Closed by hand</td><td class="r">' +
        DASH + '</td><td class="r ' + cls(t.realized_pnl) + '">' + money(t.realized_pnl) +
        '</td><td class="r">' + DASH + '</td><td class="r">' + DASH + '</td><td class="r">' + DASH +
        '</td><td class="mono tiny">' + esc((t.entry_order_id || '') + ' → ' + (t.exit_order_id || '')) +
        '</td></tr>';
    });
    if (!any) html += '<tr><td colspan="15" class="empty">No closed trades recorded.</td></tr>';
    html += '</tbody>';
    if (netN) html += '<tfoot><tr><td colspan="12">' + netN + ' algo trades with a measured net</td><td class="r ' +
      cls(net) + '"><b>' + money(net) + '</b></td><td colspan="2"></td></tr></tfoot>';
    return html + '</table>';
  }

  // -- Slippage ------------------------------------------------------------------------------

  function statCells(s) {
    if (!s || !s.measured) return '<td class="r">' + DASH + '</td><td class="r">' + DASH +
      '</td><td class="r">' + DASH + '</td><td class="r">' + (s ? s.unmeasured || 0 : 0) + '</td>';
    return '<td class="r ' + cls(-s.ticks_mean) + '">' + ticks(s.ticks_mean) + '</td><td class="r">' +
      ticks(s.ticks_worst) + '</td><td class="r ' + cls(-s.money_total) + '">' + money(s.money_total) +
      '</td><td class="r">' + s.measured + (s.unmeasured ? ' (+' + s.unmeasured + ' unmeasured)' : '') + '</td>';
  }

  function slippagePane() {
    const s = state.slip;
    if (!s) return '<p class="empty">Loading slippage…</p>';
    const head = '<th class="r">Mean</th><th class="r">Worst</th><th class="r">Total $</th><th class="r">Measured</th>';
    let html = '<table class="mon"><thead><tr><th>Algo</th><th>End</th>' + head + '<th class="r">Budget</th></tr></thead><tbody>';
    const add = (label, body, budget) => ['entry', 'exit', 'round_trip'].forEach((end, i) => {
      html += '<tr' + (i === 0 ? ' class="grp"' : '') + '><td>' + (i === 0 ? '<b>' + esc(label) + '</b>' : '') +
        '</td><td>' + end.replace('_', ' ') + '</td>' + statCells(body[end]) + '<td class="r">' +
        (i < 2 && has(budget) ? Number(budget).toFixed(2) + ' t' : '') + '</td></tr>';
    });
    add('All contracts', s.overall || {}, null);
    Object.entries(s.by_contract || {}).forEach(([k, b]) => add(b.name || k, b, b.budget_ticks));
    Object.entries(s.by_order_type || {}).forEach(([k, b]) => add('Sent as ' + k, b, null));
    html += '</tbody></table>';
    const m = s.manual;
    if (m) {
      html += '<table class="mon"><thead><tr><th>Manual tickets</th><th>End</th>' + head + '<th></th></tr></thead><tbody>';
      [['entry', m.entry], ['exit', m.exit], ['all', m.all]].forEach(([end, st], i) => {
        html += '<tr' + (i === 0 ? ' class="grp"' : '') + '><td>' + (i === 0 ? '<b>Manual</b>' : '') +
          '</td><td>' + end + '</td>' + statCells(st) + '<td></td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '<p class="tiny">PAPER fills are made at the decision price by construction and are not counted: ' +
      (s.counts ? s.counts.paper : 0) + ' paper position(s) in the period.</p>';
    return html;
  }

  // -- Reconciler -------------------------------------------------------------------------------

  function reconcilePane() {
    const p = portfolio(), ex = engine().execution || {}, st = ex.positions || {};
    let html = '<div class="mon-check ' + (st.status === 'complete' ? 'good' : 'warn') + '">TT positions: <b>' +
      esc(st.status || 'not asked') + '</b>' + (st.why ? ' — ' + esc(st.why) : '') +
      (ex.positions_waived ? ' · sending to TT ' + (ex.send_word || 'LIVE') + ' armed on the trader\'s word that this book\'s fills are the record' : '') +
      ' · book ' + (engine().book_complete ? 'complete' : '<span class="warn">recovering</span>') + '</div>';
    html += recoveryCheck();
    html += '<table class="mon"><thead><tr><th>Contract</th><th>This book</th><th>TT says</th>' +
      '<th>Long at TT</th><th>Short at TT</th><th>Verdict</th></tr></thead><tbody>';
    contracts().forEach((c) => {
      const r = (p.rows || []).find((x) => x.key === c.key);
      const book = r && r.side ? (r.side === 'BUY' ? '+' : '-') + qty(r.qty) : 'flat';
      let verdict;
      if (!p.venue_readable) verdict = '<span class="warn">TT not read — unknown, not flat</span>';
      else if (r && r.both_sides_open) verdict = '<span class="dn">LONG AND SHORT at TT — a close that went out as an open</span>';
      else if (!r || r.agrees || (!r.side && !has(r.venue_qty))) verdict = '<span class="up">agree</span>';
      else verdict = '<span class="dn">DISAGREE — never auto-closed; a person decides</span>';
      html += '<tr class="' + (verdict.includes('dn') ? 'disagrees' : '') + '"><td><b>' + esc(c.name) +
        '</b></td><td>' + book + '</td><td>' + (r ? venueSays(r) : (p.venue_readable ? 'nothing' : DASH)) +
        '</td><td>' + (r ? qty(r.venue_long) : DASH) + '</td><td>' + (r ? qty(r.venue_short) : DASH) +
        '</td><td>' + verdict + '</td></tr>';
    });
    (engine().unclaimed || []).forEach((u) => {
      html += '<tr class="disagrees"><td colspan="6">At TT and not in this book: ' + esc(JSON.stringify(u)) + '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  /* TT FIX Recovery: what Order Routing missed while the line was down,
   * replayed after each logon and applied once. */
  function recoveryCheck() {
    const r = engine().fix_recovery;
    if (!r) return '';
    const tone = { DONE: 'good', RUNNING: 'warn', NOT_RUN: 'warn' }[r.state] || 'bad';
    const word = { DONE: 'done', RUNNING: 'running…', NOT_RUN: 'not run yet', OFF: 'not set up',
                   REFUSED: 'REFUSED by TT', FAILED: 'FAILED' }[r.state] || r.state;
    const mode = r.mode === 'WINDOW' ? 'time window' : r.mode === 'RECONCILE' ? 'missed messages' : '';
    return '<div class="mon-check ' + tone + '">TT FIX Recovery: <b>' + esc(word) + '</b>' +
      (mode ? ' (' + mode + ')' : '') + ' — ' + esc(r.text || '') +
      (r.finished ? ' · ' + stamp(r.finished) : r.started ? ' · since ' + stamp(r.started) : '') +
      (r.note ? '<br><span class="warn">' + esc(r.note) + '</span>' : '') +
      (r.last_heard ? '<br><span class="tiny">Last heard from Order Routing ' + stamp(r.last_heard) + '</span>' : '') +
      ' <button class="btn recover-now" type="button"' + (r.state === 'RUNNING' ? ' disabled' : '') +
      '>Recover now</button></div>';
  }

  // -- render --------------------------------------------------------------------------------------

  const NOTES = {
    positions: 'Marked at the touch each position would actually CLOSE at — the bid for a long, the offer ' +
      'for a short. Net after costs takes the whole round trip off, so a position shows a loss the instant ' +
      'it opens. Close sends a closing order by the position\'s own tickets (77=C) and stands that contract\'s Algo down.',
    orders: 'cancel is a REQUEST: an order stays working until TT says it is cancelled, and it can fill in between.',
    fills: 'Each row is a TT Execution Report (35=8) carrying a fill. P&L is shown on a closing fill of ours — the Algo\'s against the position it closed, a manual one against the ticket it closed — before fees. Sending is not a fill.',
    accounts: 'The account this desk trades, the FIX sessions it trades over, and the P&L by origin. A figure TT does not publish here is said, never shown as 0.',
    analysis: 'Built from what is recorded: the Algo\'s closed positions and the manual ticket\'s closed trades. Net is after the configured round trip; a figure nobody measured is a dash, never 0.',
    slippage: 'Measured against the price each decision was made at. Positive is a cost at both ends; negative an improvement.',
    reconcile: 'TT\'s own positions against this book. A difference is SHOWN, never smoothed and never closed automatically. ' +
      'After each logon TT FIX Recovery replays what Order Routing missed; a fill already booked is never booked again.',
  };

  function render() {
    if (!state.snap) return;
    renderCards();
    root.querySelectorAll('.mon-tabs button').forEach((b) =>
      b.classList.toggle('on', b.dataset.tab === state.tab));
    const pane = q('.mon-pane');
    const view = { positions: positionsPane, orders: ordersPane, fills: fillsPane,
                   slippage: slippagePane, accounts: accountsPane, reconcile: reconcilePane,
                   analysis: closedPane, closed: closedPane }[state.tab] || positionsPane;
    pane.innerHTML = view();
    q('.mon-note').textContent = NOTES[state.tab] || '';
  }

  async function loadJournal(force) {
    if (!force && Date.now() - state.journalAt < 5000) return;
    state.journalAt = Date.now();
    try { state.journal = await getJSON('/api/journal'); state.journalError = null; }
    catch (e) { state.journalError = e.message; }      // said on the tab, never "no fills"
  }
  async function loadSlippage(force) {
    if (!force && Date.now() - state.slipAt < 15000) return;
    state.slipAt = Date.now();
    try { state.slip = await getJSON('/api/slippage?mode=both'); } catch (e) { /* kept */ }
  }

  async function update(snap) {
    state.snap = snap;
    if (['fills', 'closed', 'analysis'].includes(state.tab)) await loadJournal();
    if (state.tab === 'slippage') await loadSlippage();
    render();
  }

  async function poll() {
    try {
      await update(await getJSON('/api/snapshot'));
    } catch (e) {
      say('The engine did not answer: ' + e.message, true);
    }
    setTimeout(poll, 1000);
  }

  q('.mon-tabs').addEventListener('click', async (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.tab = b.dataset.tab;
    try { localStorage.setItem(tabKey, state.tab); } catch (err) {}
    render();
    if (['fills', 'closed', 'analysis'].includes(state.tab)) { await loadJournal(true); render(); }
    if (state.tab === 'slippage') { await loadSlippage(true); render(); }
  });

  q('.mon-pane').addEventListener('change', (e) => {
    if (e.target.classList.contains('ours-only')) { state.oursOnly = e.target.checked; render(); }
    if (e.target.classList.contains('show-paper')) { state.showPaper = e.target.checked; render(); }
  });

  q('.mon-pane').addEventListener('click', async (e) => {
    if (e.target.closest('.recover-now')) {
      try {
        const r = await command('recover_missed', '', {});
        say(r.text || 'FIX Recovery started.');
      } catch (err) { say(err.message, true); }
      return;
    }
    const close = e.target.closest('.close-pos');
    const algo = e.target.closest('.cancel-algo');
    const man = e.target.closest('.cancel-manual');
    const closeMan = e.target.closest('.close-manual');
    try {
      if (close) {
        const ok = await ask('Close ' + close.dataset.name + '?',
          'The position is closed at market by its own tickets, now (77=C, capped at what is open). ' +
          'The Algo on this contract is stood down with it.', 'CLOSE NOW');
        if (ok) { await command('close_now', close.dataset.key); say('Close sent for ' + close.dataset.name + '.'); }
      } else if (algo) {
        const ok = await ask('Cancel the Algo\'s orders on ' + algo.dataset.name + '?',
          'A cancel request goes to TT for every working order of ours on this contract. They stay ' +
          'working until TT confirms.', 'Request cancel');
        if (ok) { await command('cancel_all', algo.dataset.key); say('Cancel requested.'); }
      } else if (closeMan) {
        const review = await command('terminal_preview_close', '', { order_id: closeMan.dataset.id });
        const t = review.ticket || {};
        const ok = await ask('Close ' + closeMan.dataset.name + '?',
          t.side + ' ' + t.quantity + ' at MARKET · account ' + t.account + ' · Open/Close: CLOSE (77=C). ' +
          'It closes the fills of ' + closeMan.dataset.id + ' and no more.', 'Send the close');
        if (ok) {
          await command('terminal_submit', '', { token: review.token, confirmed: true });
          say('Close sent for ' + closeMan.dataset.name + '.');
        }
      } else if (man) {
        const ok = await ask('Cancel manual order ' + man.dataset.id + '?',
          'It remains working until TT confirms the cancellation.', 'Request cancel');
        if (ok) { await command('terminal_cancel', '', { order_id: man.dataset.id }); say('Cancel requested.'); }
      }
    } catch (err) { say(err.message, true); }
  });

  return { update, start: poll, render };
};
