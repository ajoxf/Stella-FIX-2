/* The screen.
 *
 * It renders a snapshot and it sends commands. It computes nothing about
 * money — every figure here arrives already decided by the engine, because a
 * number worked out twice is a number that can disagree with itself.
 *
 * Two rules run through the whole file:
 *
 *   - **null renders as an em dash, never as 0.** A missing figure and a zero
 *     are different statements, and the second one is the dangerous kind.
 *   - **No native alert / confirm / prompt.** One shared modal; a test fails
 *     the build if the natives come back.
 */
'use strict';

const DASH = '—';
/* Embedded in the Order tests page (/desk?embed=uat&contract=KEY): ONE
 * contract's ladder and Algo window, as they are live, with windows placed
 * and kept apart from the desk's own. */
const EMBED = (() => {
  const q = new URLSearchParams(location.search);
  return q.get('embed') === 'uat' ? (q.get('contract') || '') : null;
})();
const LS = EMBED === null ? 'ft.' : 'ft.uat.';
if (EMBED !== null) document.documentElement.classList.add('embed-uat');
const state = {
  refresh: 500,
  timer: null,
  sound: true,
  closed: new Set(JSON.parse(localStorage.getItem(LS + 'closed') || '[]')),
  //: Windows minimised to the taskbar. Kept in this browser.
  minimised: new Set(JSON.parse(localStorage.getItem(LS + 'min') || '[]')),
  places: JSON.parse(localStorage.getItem(LS + 'places') || '{}'),
  lastEvent: {},
  focused: null,
  free: false,
  notify: { orders: false, fills: true, positions: true, rejects: true },
};

/* -- formatting ---------------------------------------------------------- */

function num(value, decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return Number(value).toFixed(decimals === undefined ? 2 : decimals);
}
function signed(value, decimals) {
  if (value === null || value === undefined) return DASH;
  const s = Number(value).toFixed(decimals === undefined ? 2 : decimals);
  return Number(value) > 0 ? '+' + s : s;
}
function money(value) {
  if (value === null || value === undefined) return DASH;
  const sign = value < 0 ? '-' : '+';
  return sign + '$' + Math.abs(value).toLocaleString(undefined,
    { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function held(since) {
  if (!since) return DASH;
  const mins = Math.floor((Date.now() - new Date(since).getTime()) / 60000);
  if (mins < 60) return mins + 'm';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

/* -- sound: generated in the page, so nothing on the network can silence it */

let audio = null;
function beep(freqs, duration) {
  if (!state.sound) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    freqs.forEach((f, i) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = f;
      osc.type = 'sine';
      gain.gain.value = 0.05;
      osc.connect(gain); gain.connect(audio.destination);
      const start = audio.currentTime + i * duration;
      osc.start(start); osc.stop(start + duration);
    });
  } catch (e) { /* a browser that will not make sound is not a broken screen */ }
}
const SOUNDS = {
  ORDER: () => beep([660], 0.06),
  FILL: () => beep([660, 880], 0.08),
  OPEN: () => beep([660, 880], 0.08),
  CLOSED: () => beep([880, 660], 0.08),
  REJECT: () => beep([220], 0.18),
  GUARD: () => beep([220], 0.14),
  // SIGNALS mode: the Algo calls a trade for the trader to take — a chime
  // of its own, rising for an entry, falling for an exit on their position.
  SIGNAL: () => beep([523, 659, 784, 1047], 0.09),
  EXIT_SIGNAL: () => beep([1047, 784, 659, 523], 0.09),
};

/* -- toasts: errors stay until dismissed --------------------------------- */

function toast(kind, title, message, sub) {
  const host = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  const time = new Date().toLocaleTimeString([], { hour12: false });
  el.innerHTML = '<div class="hd"><span class="kind"></span>' +
    '<span class="tm"></span><span class="x">&times;</span></div>' +
    '<div class="msg"></div><div class="sub"></div>';
  el.querySelector('.kind').textContent = title;
  el.querySelector('.tm').textContent = time;
  el.querySelector('.msg').textContent = message;
  el.querySelector('.sub').textContent = sub || '';
  el.querySelector('.x').onclick = () => el.remove();
  host.prepend(el);
  if (SOUNDS[kind]) SOUNDS[kind]();
  // A reject or a withheld order STAYS: a failure that vanishes in three
  // seconds is one the operator misses.
  // A SIGNAL stays too: it is a trade the trader still has to take.
  if (kind !== 'REJECT' && kind !== 'GUARD' && kind !== 'SIGNAL' &&
      kind !== 'EXIT_SIGNAL') {
    setTimeout(() => el.remove(), 12000);
  }
  while (host.children.length > 4) host.lastChild.remove();
}

/* -- the one modal ------------------------------------------------------- */

function ask(title, body, confirmLabel, warn) {
  return new Promise((resolve) => {
    // `warn`: {text, wide} — a line above the body, red when wide.
    const w = document.getElementById('modal-warn');
    if (w) {
      w.hidden = !(warn && warn.text);
      w.textContent = (warn && warn.text) || '';
      w.classList.toggle('wide', !!(warn && warn.wide));
    }
    const modal = document.getElementById('modal');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').textContent = body;
    const yes = document.getElementById('modal-confirm');
    const no = document.getElementById('modal-cancel');
    yes.textContent = confirmLabel || 'Confirm';
    modal.classList.remove('hidden');
    const done = (answer) => {
      modal.classList.add('hidden');
      yes.onclick = null; no.onclick = null;
      resolve(answer);
    };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);          // an unanswered prompt means NO
  });
}

/* -- talking to the engine ------------------------------------------------ */

/* Send a command and WAIT for the engine's answer to it.
 *
 * Two reasons this does not just post and forget. A refusal has to be seen —
 * an algo switch the engine declined must not sit on the screen looking
 * armed. And a control has to feel immediate: without this the switch only
 * moves when the next snapshot lands, which is up to a screen refresh after
 * the engine has already acted.
 *
 * Resolves to the ENGINE's result, not the acknowledgement of the post.
 */
async function command(action, contract, args) {
  const res = await fetch('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, contract: contract || '', args: args || {} }),
  });
  const posted = await res.json();
  if (!posted.ok || !posted.id) {
    toast('REJECT', 'NOT SENT', posted.error || 'the command was not accepted',
      contract || '');
    return posted;
  }
  const answer = await commandResult(posted.id);
  if (answer === null) {
    // No answer is NOT success. The engine may be down, and a switch that
    // moved anyway would be a screen describing a system that is not there.
    toast('REJECT', 'NO ANSWER',
      'the engine did not answer ' + action + ' — it may not be running',
      contract || '');
    return { ok: false, error: 'no answer from the engine' };
  }
  if (answer.ok === false && !answer.confirm) {
    // A request for confirmation is a question, not a refusal.
    toast('REJECT', 'REFUSED', answer.error || 'the engine refused it',
      contract || '');
  }
  tick();                    // show the new state now, not at the next poll
  return answer;
}

/* Poll for one command's result. Bounded: a click must not hang for ever on
 * an engine that is not going to answer. */
async function commandResult(id, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 2000);
  while (Date.now() < deadline) {
    try {
      const r = await (await fetch('/api/result/' + encodeURIComponent(id),
        { cache: 'no-store' })).json();
      if (!r.pending) return r;
    } catch (e) { return null; }
    await new Promise((done) => setTimeout(done, 30));
  }
  return null;
}

/* The engine takes on a new contract when it starts. It stops the way it
 * always stops — this system's own working orders cancelled, the book
 * already on disk — and the launcher starts it again. */
async function restartEngine() {
  const ok = await ask('Restart the engine',
    'The engine stops — cancelling any working orders this system sent — ' +
    'and the launcher starts it again with the saved changes. Open ' +
    'positions are kept and recovered. Prices pause and the Stellar sessions ' +
    'log on again: allow about 15 seconds.', 'Restart engine');
  if (!ok) return;
  const answer = await command('restart_engine');
  if (answer && answer.ok) {
    toast('OK', 'RESTARTING', 'the engine is restarting — the desk comes ' +
      'back within about 15 seconds');
  }
}

/* -- one window ----------------------------------------------------------- */

/* What every desk window does with its own chrome: close, minimise,
 * click-to-raise, drag and resize. */
function wireWindow(el, key) {
  el.querySelector('.close').onclick = () => {
    state.closed.add(key);
    localStorage.setItem(LS + 'closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  const min = el.querySelector('.min');
  if (min) min.onclick = () => setMinimised(el, true);
  if (state.minimised.has(key)) el.classList.add('minimised');
  el.onmousedown = () => { state.focused = key; raise(el); };
  makeDraggable(el, key);
}

/* Minimised: off the desk, its button still on the taskbar. */
function setMinimised(el, on) {
  const key = el.dataset.key;
  el.classList.toggle('minimised', on);
  if (on) state.minimised.add(key); else state.minimised.delete(key);
  try { localStorage.setItem(LS + 'min', JSON.stringify([...state.minimised])); }
  catch (e) { /* a private window forgets; the desk still works */ }
  if (!on) { state.focused = key; raise(el); }
  renderTabs();
}

/* -- the Algo switch ----------------------------------------------------------
 *
 * ONE per contract, the MT5 desk's: OFF (traded by hand), SIGNALS (signals
 * only — still traded by hand) or TRADES (PAPER or LIVE, as the desk's
 * Execution says; hand orders on this contract are refused). The ladder and
 * the Algo window carry the same button and always read the same word.
 */

/* What SENDING orders to the venue is called: the venue's own word — UAT on
 * Stellar UAT, LIVE only on a live market. Never "LIVE" for a test venue. */
function sendWord() {
  return (((window.__lastSnapshot || {}).engine || {}).execution || {}).send_word || 'LIVE';
}
const ALGO_WORDS = {
  OFF: 'ALGO OFF', DRY: 'ALGO SIGNALS', PAPER: 'ALGO PAPER',
  get LIVE() { return 'ALGO ' + sendWord(); },
};

function renderAlgoSwitch(el, c) {
  const st = c.algo_state || (c.algo_on ? 'DRY' : 'OFF');
  const btn = el.querySelector('.algo-btn');
  if (!btn) return;
  btn.textContent = ALGO_WORDS[st] || st;
  btn.className = 'algo-btn ' + ({ OFF: 'off', DRY: 'dry', PAPER: 'paper', LIVE: 'live' }[st] || 'off');
  btn.dataset.state = st;
  menuWords(el);
  btn.title = st === 'LIVE' ? 'The Algo TRADES this contract and SENDS its orders to Stellar ' + sendWord() + ' — ' +
      'hand orders on it are refused; CLOSE ALL still closes. Click to change.'
    : st === 'PAPER' ? 'The Algo trades this contract on PAPER — filled here at the live ' +
      'bid/offer, nothing sent. Hand orders on it are refused. Click to change.'
    : st === 'DRY' ? 'Signals: the Algo signals entries — with an alert — and watches YOUR ' +
      'position with its take-profit and stop loss; it sends nothing. You trade on the ladder. ' +
      'this contract by hand. Click to change.'
    : 'The Algo is off: you trade this contract by hand. Click to turn the Algo on.';
}

/* The menu's last item says where the orders go: UAT on Stellar UAT, LIVE on a
 * live market. */
function menuWords(el) {
  const send = el.querySelector('.algo-menu button[data-algo="SEND"]');
  if (send) {
    send.textContent = sendWord() === 'UAT'
      ? 'UAT — the Algo trades it, orders sent to Stellar UAT'
      : 'LIVE — the Algo trades it, REAL orders to the live market';
  }
}

/* ONE switch per contract: Off · Signals · Paper · UAT (LIVE on a live
 * venue). Paper and UAT both mean "the Algo trades it"; they differ only in
 * where its orders go — filled here, or sent to Stellar — and the switch sets
 * that too, so there is no second control to find. Where the orders go is
 * one setting for the whole desk: the engine refuses to change it while
 * anything is open or working, in its own words. */
function wireAlgoSwitch(el, key) {
  const btn = el.querySelector('.algo-btn');
  const menu = el.querySelector('.algo-menu');
  if (!btn || !menu) return;
  btn.onclick = (e) => { e.stopPropagation(); menuWords(el); menu.hidden = !menu.hidden; };
  menu.querySelectorAll('button[data-algo]').forEach((item) => {
    item.onclick = async (e) => {
      e.stopPropagation();
      menu.hidden = true;
      const choice = item.dataset.algo;
      const now = btn.dataset.state;
      const want = choice === 'SEND' ? 'LIVE' : choice;
      if (want === now || (choice === 'OFF' && now === 'OFF')) return;
      const name = (el.querySelector('.title').textContent || key).replace(' · Algo', '');
      const word = sendWord();
      if (choice === 'PAPER' || choice === 'SEND') {
        const ex = ((window.__lastSnapshot || {}).engine || {}).execution || {};
        const sending = ex.mode === 'LIVE';
        if (choice === 'PAPER' && sending) {
          const r = await command('execution', '', { mode: 'PAPER' });
          if (!r || !r.ok) return;
        } else if (choice === 'SEND' && !sending) {
          const asked = await command('execution', '', { mode: 'LIVE' });
          if (!asked || (!asked.ok && !asked.confirm)) return;
          if (asked.confirm) {
            const ok = await ask('The Algo trades ' + name + ' on ' + word + '?',
              asked.text + ' It enters when the contract stretches past its band and gets out at its ' +
              'take-profit or stop loss. Hand orders on this contract are refused while it trades; ' +
              'CLOSE ALL still closes.', 'Trade on ' + word);
            if (!ok) return;
            const done = await command('execution', '', { mode: 'LIVE', confirm: true });
            if (!done || !done.ok) return;
          }
        } else if (choice === 'PAPER') {
          const ok = await ask('The Algo trades ' + name + ' on PAPER?',
            'Its orders are filled here at the live bid/offer — nothing is sent to Stellar. ' +
            'Hand orders on this contract are refused while it trades; CLOSE ALL still closes.',
            'Trade on PAPER');
          if (!ok) return;
        }
      }
      const r = await command('algo_state', key,
        { state: choice === 'PAPER' || choice === 'SEND' ? 'TRADE' : choice });
      if (r && r.ok) toast('OK', 'ALGO', (ALGO_WORDS[r.algo_state] || r.algo_state), key);
    };
  });
  document.addEventListener('click', () => { menu.hidden = true; });
}

/* The Algo window's ladder button: the contract's ladder, brought back —
 * reopened if it was closed, restored if minimised — and to the front, for
 * trading by hand (in MANUAL mode) or reading the book. */
function openLadder(key) {
  const wkey = '__ladder__' + key;
  if (state.closed.has(wkey)) {
    state.closed.delete(wkey);
    localStorage.setItem(LS + 'closed', JSON.stringify([...state.closed]));
    tick();
  }
  const el = document.querySelector('.win[data-key="' + wkey + '"]');
  if (!el) return;
  if (el.classList.contains('minimised')) setMinimised(el, false);
  state.focused = wkey;
  raise(el);
  el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function closeNowAsk(key, name) {
  return ask('Close ' + name + '?',
    'The position is closed at market, now — this crosses the spread. ' +
    'The algo on this contract is stood down with it, so it does not ' +
    're-enter on the next pass; switch it back on when you want it.',
    'CLOSE NOW').then(async (ok) => {
    if (ok) { await command('close_now', key); toast('ORDER', 'CLOSING', key); }
  });
}

function windowFor(key) {
  let el = document.querySelector('.win[data-key="' + key + '"]');
  if (el) return el;
  const tpl = document.getElementById('contract-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = key;
  wireWindow(el, key);
  wireAlgoSwitch(el, key);
  el.querySelector('.close-now').onclick = () => closeNowAsk(key,
    el.querySelector('.title').textContent);
  el.querySelector('.cog').onclick = () => configWindow(key);
  el.querySelector('.open-ladder').onclick = () => openLadder(key);
  el.querySelector('.aw-bt-run').onclick = () => runBacktest(el, key);
  document.getElementById('desktop').appendChild(el);

  const place = state.places[key];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

/* Bring a window to the front. The desk paints in DOM order otherwise, so a
 * window underneath another could never be read — and one of them is the
 * Positions window. */
let topZ = 5;
function raise(el) {
  document.querySelectorAll('.win.raised').forEach((w) => {
    if (w !== el) { w.classList.remove('raised'); w.style.zIndex = ''; }
  });
  el.classList.add('raised');
  el.style.zIndex = String(++topZ);
}

function placeWindow(el, x, y) {
  document.getElementById('desktop').classList.add('free');
  state.free = true;
  el.classList.add('placed');
  el.style.left = x + 'px';
  el.style.top = y + 'px';
}

function makeDraggable(el, key) {
  const bar = el.querySelector('.titlebar');
  bar.addEventListener('mousedown', (down) => {
    if (down.target.closest('.winbtn, .algo-switch')) return;
    const rect = el.getBoundingClientRect();
    const desk = document.getElementById('desktop').getBoundingClientRect();
    const dx = down.clientX - rect.left;
    const dy = down.clientY - rect.top;
    // Snapshot every other window's place first, or the ones still in the
    // grid jump to the corner the moment this one leaves it.
    document.querySelectorAll('.win').forEach((other) => {
      if (other === el || other.classList.contains('placed')) return;
      const r = other.getBoundingClientRect();
      placeWindow(other, r.left - desk.left + desk.left * 0,
        r.top - desk.top + document.getElementById('desktop').scrollTop);
      state.places[other.dataset.key] = Object.assign({},
        state.places[other.dataset.key],
        { x: parseFloat(other.style.left), y: parseFloat(other.style.top) });
    });
    const move = (m) => {
      const x = m.clientX - desk.left - dx;
      const y = m.clientY - desk.top - dy + document.getElementById('desktop').scrollTop;
      placeWindow(el, Math.max(0, x), Math.max(0, y));
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      state.places[key] = Object.assign({}, state.places[key],
        { x: parseFloat(el.style.left), y: parseFloat(el.style.top) });
      savePlaces();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    down.preventDefault();
  });
  makeResizable(el, key);
}

function savePlaces() {
  try { localStorage.setItem(LS + 'places', JSON.stringify(state.places)); }
  catch (e) { /* a private window: the desk still works, it just forgets */ }
}

/* The corner handle is the browser's own (`resize: both`). What is kept is
 * the size the TRADER set: a resize writes an inline width and height, which
 * a window growing with its own content never does — so a window whose
 * table got longer is not remembered at that height. */
function makeResizable(el, key) {
  const saved = state.places[key];
  if (saved && saved.w && saved.h) {
    el.style.width = saved.w + 'px';
    el.style.height = saved.h + 'px';
  }
  if (!window.ResizeObserver) return;
  let timer = null;
  new ResizeObserver(() => {
    if (!el.style.width && !el.style.height) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.places[key] = Object.assign({}, state.places[key],
        { w: el.offsetWidth, h: el.offsetHeight });
      savePlaces();
    }, 250);
  }).observe(el);
}

/* Minutes as a short figure: 84 not 84.0, 0.5 as 0.5. */
function minutes(v) {
  if (v === null || v === undefined || isNaN(v)) return DASH;
  return v >= 10 ? Math.round(v).toString() : (Math.round(v * 10) / 10).toString();
}

function renderContract(c) {
  if (state.closed.has(c.key)) return;
  const el = windowFor(c.key);
  const q = (sel) => el.querySelector(sel);

  q('.title').textContent = c.name + ' · Algo';
  q('.state').textContent = c.state;
  q('.state').className = 'state s-' + c.state;
  q('.state').title = c.halted_by ? 'Halted: ' + c.halted_by
    : c.market_note ? 'Market: ' + c.market_note : '';
  el.classList.toggle('stale', !!(c.feed && c.feed.stale));

  renderAlgoSwitch(el, c);

  // The one-way badge: a contract that is armed and passes over half its
  // signals otherwise looks broken.
  const dir = (c.filters || {}).trade_direction || 'BOTH';
  const badge = q('.dirbadge');
  badge.classList.toggle('hidden', dir === 'BOTH');
  badge.textContent = dir === 'SELL_ONLY' ? 'H to L ONLY'
    : dir === 'BUY_ONLY' ? 'L to H ONLY' : '';

  renderAlgoBody(el, c);

  // footer
  const foot = q('.wfoot');
  const age = c.feed && c.feed.age_sec;
  q('.age').textContent = age === null || age === undefined ? DASH : age + 's';
  q('.last-event').textContent = c.last_event || '';
  foot.className = 'wfoot' + (c.feed && c.feed.stale ? ' bad'
    : c.state === 'WARMING' ? ' warnf' : '');

  markPosition(el, c);

  // A change of last_event is a thing that happened: say it once, and only
  // if the operator asked to hear about that kind. Order traffic is off by
  // default — every send and every fill on eight contracts buries the screen
  // it is meant to be reporting on.
  if (c.last_event && state.lastEvent[c.key] !== c.last_event) {
    if (state.lastEvent[c.key] !== undefined) {
      const kind = /reject/i.test(c.last_event) ? 'REJECT'
        : / out at /i.test(c.last_event) ? 'CLOSED'
          : /^(BUY|SELL) \d/i.test(c.last_event) ? 'OPEN'
            : 'ORDER';
      const want = state.notify;
      const allowed = kind === 'REJECT' ? want.rejects !== false
        : kind === 'CLOSED' || kind === 'OPEN' ? want.positions !== false
          : want.orders === true;
      if (allowed) toast(kind, kind, c.last_event, c.name);
    }
    state.lastEvent[c.key] = c.last_event;
  }
}

/* -- the Algo window --------------------------------------------------------
 *
 * SIGNAL & POSITION, STATISTICS, FILTERS — the MT5 desk's Algo window, on
 * ONE contract. The venue lists the spread itself, so "H to L" is selling
 * the contract into its BID and "L to H" is buying its OFFER; there are no
 * legs. Every figure comes from the engine's Algo block.
 */

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Money with cents: a round trip of $4.15 is not "$4". */
function cash(value) {
  if (value === null || value === undefined || isNaN(value)) return DASH;
  const sign = value < 0 ? '-' : '';
  return sign + '$' + Math.abs(value).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function zText(value) {
  if (value === null || value === undefined || isNaN(value)) return DASH;
  return (value > 0 ? '+' : '') + Number(value).toFixed(2);
}

function kv(label, value, cls, title, wide) {
  return '<div class="aw-kv' + (wide ? ' wide' : '') + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '><span>' + label +
    '</span><b' + (cls ? ' class="' + cls + '"' : '') + '>' + value +
    '</b></div>';
}

function awBadge(text, tone, title) {
  return '<span class="aw-badge ' + tone + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '>' + text + '</span>';
}

function sideWords(side) {
  return side === 'SELL' ? 'H to L' : side === 'BUY' ? 'L to H' : (side || DASH);
}

const ALGO_EXIT_WORDS = {
  STOP_LOSS: 'stop loss', PROFIT_TARGET: 'profit target',
  Z_STOP: 'z-stop', MEAN_REVERSION: 'back to the mean', TIME_STOP: 'time stop',
};

function levelWords(level, worth, d) {
  /* "13.7000 (+$4.00)": the price to compare with the closing side, and
   * what closing there is worth to the whole position, net. */
  return num(level, d) + (worth === null || worth === undefined ? ''
    : ' (' + (worth > 0 ? '+' : '') + cash(worth) + ')');
}

function positionWords(p, d) {
  return (p.side || '?') + ' @ ' + num(p.entry, d);
}

function algoLine(block, d) {
  /* One short line: what the Algo says right now. */
  const st = block.state;
  if (st === 'SIGNAL') {
    const z = block.signal === 'SELL' ? block.z_sell : block.z_buy;
    return sideWords(block.signal) + ' signal  z ' + zText(z);
  }
  if (st === 'EXIT') {
    return (block.positions || []).filter((p) => p.exit).map((p) =>
      'EXIT ' + positionWords(p, d) + ': ' + (ALGO_EXIT_WORDS[p.exit] || p.exit))
      .join('; ');
  }
  if (st === 'IN_POSITION') {
    const first = (block.positions || [])[0] || {};
    return 'in ' + positionWords(first, d) + ' — TP ' +
      levelWords(first.tp, first.tp_money, d) + ' · SL ' +
      (first.sl === null || first.sl === undefined
        ? ((block.params || {}).stop_loss_on ? DASH : 'off')
        : levelWords(first.sl, first.sl_money, d));
  }
  if (st === 'BLOCKED') return 'held: ' + (block.blocked || '');
  if (st === 'CONFIRMING') {
    const streak = block.streak || {};
    return 'confirming ' + Math.max(streak.BUY || 0, streak.SELL || 0) + '/' +
      ((block.params || {}).confirm_ticks || '?');
  }
  if (st === 'STARTING') return 'starting…';
  return 'watching';
}

function progressHtml(first, d) {
  /* SL <- entry -> TP, with the closing price on it; each half on its own
   * scale, as the engine measures them. */
  const entry = first.entry;
  const toTp = first.tp === null || first.tp === undefined ? null
    : Math.abs(first.tp - entry);
  const toSl = first.sl === null || first.sl === undefined ? null
    : Math.abs(entry - first.sl);
  const split = (toTp && toSl) ? toSl / (toSl + toTp) : (toSl ? 0.5 : 0);
  const p = first.progress;
  const at = p === null || p === undefined ? null
    : (p >= 0 ? split + p * (1 - split) : split + p * split);
  const fill = at === null ? '' :
    '<div class="ap-fill ' + (p >= 0 ? 'up' : 'down') + '" style="left:' +
    (Math.min(at, split) * 100) + '%;width:' + (Math.abs(at - split) * 100) +
    '%"></div><div class="ap-mark" style="left:calc(' + (at * 100) +
    '% - 1px)"></div>';
  return '<div class="algo-progress" title="' + esc(positionWords(first, d) +
    ', closing at ' + num(first.closing, d)) + '"><div class="ap-pct">' +
    (p === null || p === undefined ? DASH : (p >= 0
      ? Math.round(p * 100) + '% to TP' : Math.round(-p * 100) + '% to SL')) +
    '</div><div class="ap-track">' + fill + '<div class="ap-entry" style="left:' +
    (split * 100) + '%"></div></div><div class="ap-ends"><span>' +
    (toSl === null ? 'no SL' : 'SL ' + levelWords(first.sl, first.sl_money, d)) +
    '</span><span>' + (toTp === null ? 'no TP'
      : 'TP ' + levelWords(first.tp, first.tp_money, d)) + '</span></div></div>';
}

function warmupHtml(warmup) {
  /* Live time watched since the Algo was armed, against what it needs. A
   * band rebuilt from the recording in a second is not a feed watched. */
  if (!warmup || !warmup.need_sec) return '';
  const done = !!warmup.done;
  const pct = done ? 100 : Math.floor(100 * warmup.sec / warmup.need_sec);
  return '<div class="aw-data aw-warmup" title="No entry until the Algo has ' +
    'watched this long of live prices since it was armed. Time with no price ' +
    'does not count."><span>Live</span><div class="aw-bar"><div class="' +
    (done ? 'ok' : 'wait') + '" style="width:' + pct + '%"></div></div><span>' +
    (done ? 'warmed up' : 'warming up ' + Math.floor(warmup.sec / 60) + '/' +
      Math.round(warmup.need_sec / 60) + ' min') + '</span></div>';
}

function trendBadge(trend) {
  /* Which way the band's middle moved over the lookback: up holds H to L,
   * down holds L to H, flat holds neither. */
  if (!trend.on) return awBadge('OFF', 'off');
  const drift = trend.drift_sigma;
  const why = drift === null || drift === undefined
    ? 'not enough candles to measure the trend yet'
    : 'the middle moved ' + (drift >= 0 ? '+' : '') + drift.toFixed(2) +
      'σ in ' + trend.lookback_min + ' min (limit ' + trend.limit + 'σ)';
  if (trend.state === 'UP') return awBadge('↑', 'bad', why + ' — no H to L');
  if (trend.state === 'DOWN') return awBadge('↓', 'bad', why + ' — no L to H');
  if (trend.state === 'FLAT') return awBadge('–', 'ok', why);
  return awBadge('WAIT', 'wait', why);
}

function lastOrderHtml(block) {
  /* What the Algo last DID, and what became of it — a refusal in its own
   * words, never a blank. */
  const last = (block.recent || [])[0];
  let html = '<div class="aw-blocked aw-last-order"><div class="aw-head2">' +
    'Last order</div>';
  if (!last) return html + '<div class="hint">none yet</div></div>';
  const what = esc(last.action || '') + ' ' + esc(sideWords(last.side)) +
    (last.z === null || last.z === undefined ? '' : ' z ' + zText(last.z)) +
    ' · ' + new Date(last.at * 1000).toLocaleTimeString();
  let outcome;
  if (last.mode === 'DRY RUN') {
    outcome = '<div class="hint">signal — nothing sent; you trade it</div>';
  } else if (last.done) {
    outcome = '<div class="up">' + (last.mode === 'PAPER'
      ? 'filled on paper — nothing sent' : 'sent — done') + '</div>';
  } else {
    outcome = '<div class="down">REFUSED — ' +
      esc(last.result || 'no reason given') + '</div>';
  }
  return html + '<div><b>' + what + '</b></div>' + outcome + '</div>';
}

function renderAlgoBody(el, c) {
  const d = c.decimals === undefined ? 4 : c.decimals;
  const block = c.algo || {};
  const params = block.params || {};
  const filters = block.filters || {};
  const market = c.market || {};
  const entryZ = params.entry_z;
  const direction = params.direction || 'BOTH';

  const mode = el.querySelector('.aw-mode') || document.createElement('span');
  mode.textContent = block.mode === 'LIVE' ? sendWord() : (block.mode || DASH);
  mode.className = 'aw-mode ' + (block.mode === 'LIVE' ? 'live'
    : block.mode === 'PAPER' ? 'paper' : 'dry');
  mode.title = block.mode === 'LIVE'
    ? 'the Algo SENDS its orders to Stellar ' + sendWord()
    : block.mode === 'PAPER'
      ? 'the Algo fills on paper at the live bid/offer — nothing is sent'
      : 'signals only — nothing is sent or filled (Auto trade is off)';

  // -- SIGNAL & POSITION ----------------------------------------------------
  function entryLine(sell) {
    if (!params.reentry_on) {
      return '<div class="aw-tile-entry">' + (sell ? 'short' : 'long') +
        ' at ' + (sell ? '≥ +' : '≤ −') + (entryZ || '?') + ' (' +
        num(sell ? block.upper : block.lower, d) + ')</div>';
    }
    const inZ = Math.max(0, (entryZ || 0) - (params.reentry_back || 0));
    const windowPct = params.reentry_window_pct === undefined ||
      params.reentry_window_pct === null ? 50 : params.reentry_window_pct;
    const outZ = inZ * (1 - windowPct / 100);
    const priced = !(block.mean === null || block.mean === undefined ||
                     !block.sigma);
    const level = priced ? block.mean + (sell ? 1 : -1) * inZ * block.sigma : null;
    const outLevel = priced ? block.mean + (sell ? 1 : -1) * outZ * block.sigma : null;
    const armed = (block.armed || {})[sell ? 'SELL' : 'BUY'];
    return '<div class="aw-tile-entry" title="Armed when the stretch reaches ' +
      (sell ? '+' : '−') + entryZ + '; entered on the way back in, between ' +
      (sell ? '+' : '−') + inZ.toFixed(2) + ' and ' + (sell ? '+' : '−') +
      outZ.toFixed(2) + '. Past ' + outZ.toFixed(2) + ' it disarms — too close ' +
      'to the mean to trade.">' +
      (armed ? '<b class="aw-armed">ARMED</b> ' : 'arm ' +
        (sell ? '≥ +' : '≤ −') + entryZ + ' · ') +
      (sell ? 'short' : 'long') + ' back at ' + (sell ? '+' : '−') +
      inZ.toFixed(2) + ' (' + num(level, d) + ') to ' + (sell ? '+' : '−') +
      outZ.toFixed(2) + ' (' + num(outLevel, d) + ')</div>';
  }
  function tile(sell) {
    const z = sell ? block.z_sell : block.z_buy;
    const hit = z !== null && z !== undefined && entryZ &&
      (sell ? z >= entryZ : z <= -entryZ);
    const off = (sell && direction === 'L_TO_H') || (!sell && direction === 'H_TO_L');
    return '<div class="aw-tile ' + (sell ? 'sell' : 'buy') + (hit ? ' hit' : '') +
      (off ? ' off' : '') + '" title="' + (sell
        ? 'What SELLING the contract gets now (its bid). The Algo sells when '
          + 'this z reaches +' + entryZ + ', and closes a long here.'
        : 'What BUYING the contract costs now (its offer). The Algo buys when '
          + 'this z reaches −' + entryZ + ', and closes a short here.') +
      '"><div class="aw-tile-head">' + (sell ? 'H to L' : 'L to H') +
      (off ? ' <small>(entries off)</small>' : '') +
      '</div><div class="aw-tile-price">' + num(sell ? market.bid : market.ask, d) +
      '</div><div class="aw-tile-z">' + zText(z) + '</div>' + entryLine(sell) +
      sizingLine(sell) + '</div>';
  }
  // This direction's own TP / SL sizing — H to L and L to H can differ.
  function sizingLine(sell) {
    const sp = sideParams(params, sell ? 'SELL' : 'BUY');
    const own = Object.keys((params.sides || {})[sell ? 'SELL' : 'BUY'] || {}).length > 0;
    const tp = sp.target_mode === 'ATR' ? sp.atr_target_mult + '×ATR' : sp.target_pct + '%';
    const sl = !sp.stop_loss_on ? 'off'
      : sp.stop_mode === 'ATR' ? sp.atr_stop_mult + '×ATR' : sp.stop_loss_pct + '%';
    return '<div class="aw-tile-levels' + (own ? ' own' : '') + '" title="' +
      (own ? 'this direction\'s own target and stop' : 'the shared target and stop') +
      ' — % of the margin, or a multiple of the ATR at entry, from break-even">TP ' +
      tp + ' · SL ' + sl + (own ? ' <small>own</small>' : '') + '</div>';
  }
  const first = (block.positions || [])[0] || null;
  // SIGNALS mode: the position is the TRADER's, watched by the Algo — said.
  const position = first ? (first.manual ? 'YOUR ' : '') +
    (first.side === 'BUY' ? 'LONG' : 'SHORT') + ' ' + num(first.quantity, 0) : 'FLAT';
  let html = '<div class="aw-head">Signal &amp; Position</div>' +
    '<div class="aw-tiles">' + tile(true) + tile(false) +
    '<div class="aw-pos ' + (first ? (first.side === 'BUY' ? 'long' : 'short')
      : 'flat') + '">' + position + '</div></div>' +
    '<div class="aw-line" title="' + esc(block.blocked || '') + '">' +
    esc(algoLine(block, d)) + '</div>';
  if (first && first.manual && first.levels_why) {
    html += '<div class="aw-line warn">' + esc(first.levels_why) + '</div>';
  }
  if (first) {
    const delta = first.closing === null || first.closing === undefined ||
      first.entry === null ? null : first.closing - first.entry;
    const good = delta === null ? ''
      : ((first.side === 'BUY' ? delta >= 0 : delta <= 0) ? 'up' : 'down');
    html += '<div class="aw-grid">' +
      kv('Entry', num(first.entry, d)) +
      kv('Δ price', delta === null ? DASH : (delta > 0 ? '+' : '') + num(delta, d),
        good, 'closing price now − entry: a LONG makes money when this is ' +
        'positive, a SHORT when it is negative') +
      kv('Entry z', zText(first.entry_z)) +
      kv('Entry slip', first.entry_slip_ticks === null ||
        first.entry_slip_ticks === undefined
        ? (first.paper ? 'paper' : DASH)
        : (first.entry_slip_ticks > 0 ? '+' : '') +
          Number(first.entry_slip_ticks).toFixed(2) + ' t',
        first.entry_slip_ticks > 0 ? 'down' : first.entry_slip_ticks < 0 ? 'up' : '',
        'the fill against the price the Algo decided at: positive a cost, ' +
        'negative an improvement. A paper fill is made at that price — ' +
        'nothing to measure') +
      kv('Net P&amp;L', cash(first.net_pnl), first.net_pnl === null ||
        first.net_pnl === undefined ? '' : (first.net_pnl >= 0 ? 'up' : 'down'),
        'after the whole round trip, at the side it would close on') +
      kv('Age', first.age_sec === null || first.age_sec === undefined ? DASH
        : Math.floor(first.age_sec / 60) + 'm ' + Math.floor(first.age_sec % 60) + 's') +
      kv('Size', num(first.quantity, 0) + ' contract(s)' +
        (first.paper ? ' <small>paper</small>' : '') +
        (first.manual ? ' <small>yours — the Algo signals its exits, you close it</small>' : '')) +
      '</div>' +
      kv('Levels', 'BE ' + num(first.break_even, d) + ' · TP ' +
        (first.tp === null || first.tp === undefined ? DASH
          : levelWords(first.tp, first.tp_money, d)) + ' · SL ' +
        (first.sl === null || first.sl === undefined
          ? (params.stop_loss_on ? DASH : 'off')
          : levelWords(first.sl, first.sl_money, d)) +
        ((sideParams(params, first.side).stop_mode === 'ATR' ||
          sideParams(params, first.side).target_mode === 'ATR')
          ? ' <small>(ATR ' + num(first.entry_atr, d) + ' at entry)</small>' : ''),
        '', 'compare with the CLOSING price: the bid for a LONG, the offer ' +
        'for a SHORT. In brackets: the net P&L of closing there', true) +
      (params.progress_bar === false ? '' : progressHtml(first, d));
  }
  el.querySelector('.aw-signal').innerHTML = html;
  el.querySelector('.aw-closebar').classList.toggle('hidden', !c.position);
  el.querySelector('.close-now').disabled = !c.position;

  // -- STATISTICS -------------------------------------------------------------
  const history = block.history || {};
  const regime = filters.regime || {};
  const count = block.count || 0;
  const needed = block.needed || params.length || 0;
  const pctDone = needed ? Math.min(100, Math.round(count * 100 / needed)) : 0;
  const regimeText = regime.state === 'TRENDING'
    ? 'Trend ' + ((regime.slope || 0) > 0 ? '↑' : '↓')
    : regime.state === 'RANGE' ? 'Mean-rev'
      : regime.state === 'COLLECTING' ? 'Collect' : DASH;
  el.querySelector('.aw-stats').innerHTML =
    '<div class="aw-head">Statistics</div><div class="aw-grid">' +
    kv('Mean (EMA)', num(block.mean, d)) +
    kv('Std dev', num(block.sigma, d)) +
    kv('ATR(' + (block.atr_period || params.atr_period || 14) + ')',
      num(block.atr, d), '', 'the average size of one candle’s move, close ' +
      'to close (Wilder). In ATR mode the stop and target are multiples of ' +
      'it, frozen when the trade opens') +
    kv('Half-life', filters.half_life_minutes === null ||
      filters.half_life_minutes === undefined ? DASH
      : Math.round(filters.half_life_minutes) + ' min', '',
      'how long a stretch takes to halve, from the candles (AR(1))') +
    kv('Regime', regimeText, regime.state === 'TRENDING' ? 'down'
      : (regime.state === 'RANGE' ? 'up' : ''),
      regime.efficiency_ratio === null || regime.efficiency_ratio === undefined
        ? '' : 'efficiency ' + regime.efficiency_ratio.toFixed(2) + ', ' +
          regime.crossings + ' mean crossings') +
    kv('Candles', (block.timeframe_min || params.timeframe_min) + 'm × ' + needed) +
    kv('Band', num(block.lower, d) + ' … ' + num(block.upper, d), '', '', true) +
    '</div><div class="aw-data" title="' + esc(history.note || '') + '">' +
    '<span>Data</span><div class="aw-bar"><div class="' +
    (block.ready ? 'ok' : 'wait') + '" style="width:' +
    (block.ready ? 100 : pctDone) + '%"></div></div><span>' +
    (block.ready ? 'candles ready' : count + '/' + needed + ' candles') +
    '</span></div>' + warmupHtml(block.warmup);

  // -- FILTERS ---------------------------------------------------------------
  const edge = filters.edge || {};
  const cost = filters.cost || {};
  const band = filters.half_life_band || [0, 0];
  function onOff(on, ok, yes, no, why) {
    if (!on) return awBadge('OFF', 'off', why);
    if (ok === null || ok === undefined) return awBadge('—', 'wait', why);
    return awBadge(ok ? yes : no, ok ? 'ok' : 'bad', why);
  }
  const trending = regime.state === 'TRENDING';
  const day = block.day || {};
  const last = block.last_blocked;
  el.querySelector('.aw-filters').innerHTML =
    '<div class="aw-head">Filters</div><div class="aw-badges">' +
    '<div>' + onOff(edge.on, edge.ok, '✓', '✗',
      'expected capture against the round-trip cost, at the entry z') +
    '<small>Edge</small></div>' +
    '<div>' + (regime.on ? awBadge(trending ? 'TR' : (regime.state === 'RANGE'
      ? 'MR' : 'WAIT'), trending ? 'bad' : (regime.state === 'RANGE' ? 'ok'
      : 'wait')) : awBadge('OFF', 'off')) + '<small>Regime</small></div>' +
    '<div>' + trendBadge(filters.trend || {}) + '<small>Trend</small></div>' +
    '<div>' + awBadge(filters.ready ? 'YES' : 'NO', filters.ready ? 'ok' : 'wait')
    + '<small>Ready</small></div>' +
    ((band[0] || band[1]) ? '<div>' + awBadge(Math.round(band[0] || 0) + '–' +
      (band[1] ? Math.round(band[1]) : '∞'), 'off') + '<small>HL min</small></div>'
      : '') +
    '</div><div class="aw-grid">' +
    kv('Capture / cost', (edge.ratio === null || edge.ratio === undefined
      ? DASH : edge.ratio.toFixed(2) + '×') + ' / req ' +
      (edge.required === undefined ? DASH : edge.required + '×'),
      edge.ok === true ? 'up' : (edge.ok === false ? 'down' : ''),
      'expected capture ' + cash(edge.capture) + ' (' +
      (params.edge_capture_frac || 0.5) + ' × entry z × σ) against the round ' +
      'trip, at the entry z', true) +
    kv('Round trip', cash(cost.total), '', 'crossing ' + cash(cost.crossing) +
      ' + fees ' + cash(cost.commission) + ' + slippage BUDGET ' +
      cash(cost.slippage)) +
    kv('Slippage today', day.slip_sides
      ? ((day.slip_ticks / day.slip_sides) > 0 ? '+' : '') +
        (day.slip_ticks / day.slip_sides).toFixed(2) + ' t/side · ' +
        cash(day.slip_money) : DASH,
      day.slip_sides ? (day.slip_ticks > 0 ? 'down' : day.slip_ticks < 0
        ? 'up' : '') : '',
      'MEASURED today, each fill against the price its decision was made at ' +
      '(positive a cost) — beside the budget of ' +
      (block.slip_budget_ticks || 0) + ' t/side the edge filter charges. ' +
      (day.slip_sides || 0) + ' side(s) measured' +
      (day.slip_unmeasured ? ', ' + day.slip_unmeasured + ' not (paper or ' +
        'no decision price)' : '') + '.', true) +
    kv('Today', (day.trades || 0) + (params.max_trades_day
      ? '/' + params.max_trades_day : '') + ' trades · ' + (day.losses_row || 0) +
      ' in a row · ' + cash(day.pnl), '', 'the day’s limits stop entries, ' +
      'never exits', true) +
    kv('Size', num(filters.qty || params.algo_qty || 1, 0) + ' contract(s)') +
    '</div><div class="aw-blocked"><div class="aw-head2">Last signal blocked</div>' +
    (last ? '<div><b>' + esc(sideWords(last.side)) + '</b> z ' + zText(last.z) +
      ' · ' + new Date(last.at * 1000).toLocaleTimeString() + '</div><div>' +
      esc(last.reason || '') + '</div>'
      : '<div class="hint">none yet</div>') + '</div>' + lastOrderHtml(block);
}

/* -- the backtest ------------------------------------------------------------ */

async function runBacktest(el, key) {
  const out = el.querySelector('.aw-bt-out');
  const button = el.querySelector('.aw-bt-run');
  const days = parseInt(el.querySelector('.aw-bt-days').value, 10) || 5;
  out.className = 'aw-bt-out hint';
  out.textContent = 'running on the last ' + days + ' days…';
  button.disabled = true;
  try {
    const res = await fetch('/api/backtest/' + encodeURIComponent(key) +
      '?days=' + days, { cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) {
      out.className = 'aw-bt-out down';
      out.textContent = 'could not run: ' + (data.reason || data.error || 'unknown');
    } else {
      out.className = 'aw-bt-out';
      out.innerHTML = backtestHtml(data, (window.__lastSnapshot || {}).contracts
        ? ((window.__lastSnapshot.contracts.find((c) => c.key === key) || {})
          .decimals) : 4);
    }
  } catch (e) {
    out.className = 'aw-bt-out down';
    out.textContent = 'could not run: ' + e.message;
  }
  button.disabled = false;
}

function backtestHtml(data, d) {
  const s = data.summary || {};
  const plain = data.without_protections || {};
  function line(sum) {
    return (sum.trades || 0) + ' trade(s) · ' + (sum.wins || 0) + ' won, ' +
      (sum.losses || 0) + ' lost · net ' + cash(sum.net) + ' · worst run ' +
      cash(sum.max_drawdown);
  }
  function when(at) {
    return at ? new Date(at * 1000).toLocaleString([], { month: 'short',
      day: 'numeric', hour: '2-digit', minute: '2-digit' }) : DASH;
  }
  const heldBy = Object.keys(data.held || {}).map((r) => [r, data.held[r]])
    .sort((a, b) => b[1] - a[1]).slice(0, 3);
  const rows = (data.trades || []).slice(-8).reverse().map((t) =>
    '<tr><td>' + when(t.opened_at) + '</td><td>' + sideWords(t.side) +
    '</td><td>' + zText(t.entry_z) + '</td><td>' +
    esc(ALGO_EXIT_WORDS[t.reason] || t.reason || '') + '</td><td class="' +
    (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : '')) + '">' + cash(t.pnl) +
    '</td></tr>').join('');
  return '<div><b>These settings:</b> ' + line(s) + '</div>' +
    '<div class="hint">Without re-entry and the trend filter: ' + line(plain) +
    '</div>' + (heldBy.length ? '<div class="hint">Held back most by: ' +
      heldBy.map((h) => esc(h[0]) + ' (' + h[1] + ')').join('; ') + '</div>' : '') +
    (rows ? '<table class="aw-bt-trades"><tr><th>Entered</th><th>Side</th>' +
      '<th>z</th><th>Exit</th><th>P&amp;L</th></tr>' + rows + '</table>' : '') +
    '<div class="hint aw-bt-caveats" title="' + esc((data.caveats || []).join('; ')) +
    '">' + (s.candles || 0) + ' candles of recorded mids, ' + when(s.from) +
    ' – ' + when(s.to) + ' · hover for what a backtest cannot see</div>';
}

/* -- the ladder ----------------------------------------------------------------
 *
 * The MT5 desk's ladder, on one contract. The Bids and Asks columns are
 * solid furniture — they never change size; what moves is the sizes in
 * them, at the touch. Work carries our working orders and the Algo's levels
 * (TP, SL, BE, the band, the mean). In ALGO mode a person does not trade it
 * and the lock banner says so; CLOSE ALL and Close @ LMT always close.
 */

const LADDER_ROWS = 24;          // each side of the centre
const ladderCentre = {};         // key -> centre price, held until Centre
const ladderPrefs = {};          // key -> {lock, filter, increment}

/* The Algo's parameters for one direction: its own target / stop laid over
 * the shared ones (algo.side_params). */
function sideParams(params, side) {
  return Object.assign({}, params || {}, ((params || {}).sides || {})[side] || {});
}

function ladderPref(key) {
  if (!ladderPrefs[key]) {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(LS + 'ladder.' + key) || '{}'); }
    catch (e) { /* private window: defaults */ }
    ladderPrefs[key] = Object.assign({ lock: false, filter: false, increment: null }, saved);
  }
  return ladderPrefs[key];
}

function saveLadderPref(key) {
  try { localStorage.setItem(LS + 'ladder.' + key, JSON.stringify(ladderPrefs[key])); }
  catch (e) { /* kept for this page only */ }
}

function ladderFor(key) {
  const wkey = '__ladder__' + key;
  let el = document.querySelector('.win[data-key="' + wkey + '"]');
  if (el) return el;
  el = document.getElementById('ladder-template').content.firstElementChild
    .cloneNode(true);
  el.dataset.key = wkey;
  el.dataset.contract = key;
  wireWindow(el, wkey);
  const pref = ladderPref(key);
  el.querySelector('.ld-lockscroll').checked = !!pref.lock;
  el.querySelector('.ld-filter').checked = !!pref.filter;
  if (pref.increment) el.querySelector('.ld-increment').value = pref.increment;
  el.querySelector('.ld-lockscroll').onchange = (e) => { pref.lock = e.target.checked; saveLadderPref(key); };
  el.querySelector('.ld-filter').onchange = (e) => { pref.filter = e.target.checked; saveLadderPref(key); };
  el.querySelector('.ld-increment').onchange = (e) => {
    const v = parseFloat(e.target.value);
    pref.increment = v > 0 ? v : null;
    delete ladderCentre[key];
    saveLadderPref(key);
  };
  el.querySelector('.ld-centre').onclick = () => { delete ladderCentre[key]; el.dataset.recentre = '1'; };
  el.querySelector('.ld-flatten').onclick = () => (ladderManualPositions(key).length
    ? closeManual(el, key, null)
    : closeNowAsk(key, el.querySelector('.title').textContent));
  el.querySelector('.ld-close-go').onclick = () => closeAtLimitAsk(el, key);
  el.querySelector('.ld-depth').onclick = async () => {
    const full = el.querySelector('.ld-depth').dataset.full === '1';
    const r = await command('depth', key, { on: !full });
    if (r && r.ok) toast('OK', 'DEPTH', r.full ? 'full depth requested from Stellar'
      : 'top of book only', key);
  };
  el.querySelector('.ld-refresh').onclick = async () => {
    const r = await command('refresh_feed', key);
    if (r && r.ok) toast('OK', 'FEED', 'prices re-requested from Stellar', key);
  };
  const cxl = (side, label) => async () => {
    if (el.classList.contains('manual-on') && !el.classList.contains('algo-test')) {
      return cancelManual(el, key, (o) => !side || o.ticket.side === side, label);
    }
    const ok = await ask('Cancel ' + label + ' on ' + el.querySelector('.title').textContent + '?',
      'A cancel request goes to Stellar for each of our working orders. They stay working until Stellar ' +
      'confirms — and can fill in between.', 'Request cancel');
    if (ok) await command('cancel_all', key, side ? { side } : {});
  };
  el.querySelector('.ld-cxl-s').onclick = cxl('SELL', 'the working SELL orders');
  el.querySelector('.ld-cxl-b').onclick = cxl('BUY', 'the working BUY orders');
  el.querySelector('.ld-cxl-all').onclick = cxl(null, 'every working order');
  // MANUAL mode: hand trading on this ladder, through the manual ticket —
  // every order reviewed, then sent as an FTM- order. ManualTerminal refuses
  // it in ALGO mode whatever this page does.
  wireAlgoSwitch(el, key);
  el.querySelector('.ld-buy').onclick = () => manualOrder(el, key, 'BUY', null);
  el.querySelector('.ld-sell').onclick = () => manualOrder(el, key, 'SELL', null);
  el.querySelectorAll('.ld-keypad button').forEach((b) => {
    b.onclick = () => {
      const v = b.textContent === 'CLR' ? '' : b.textContent;
      el.querySelectorAll('.ld-qtybox').forEach((box) => {
        box.value = v;
        box.dataset.cleared = v === '' ? '1' : '';
      });
    };
  });
  el.querySelectorAll('.ld-qtybox').forEach((box) => {
    box.oninput = () => { box.dataset.cleared = box.value === '' ? '1' : ''; };
  });
  el.querySelector('.ld-ot').onchange = (e) => { pref.ot = e.target.value; saveLadderPref(key); };
  el.querySelector('.ld-tif').onchange = (e) => { pref.tif = e.target.value; saveLadderPref(key); };
  // Stellar's convention: a click in Bids BUYS at that price, in Asks SELLS.
  el.querySelector('.ld-grid tbody').addEventListener('click', (e) => {
    const cell = e.target.closest('td.bid, td.ask');
    if (!cell || !el.classList.contains('manual-on')) return;
    const price = parseFloat(cell.parentElement.querySelector('td.price').textContent);
    manualOrder(el, key, cell.classList.contains('bid') ? 'BUY' : 'SELL', price);
  });
  el.querySelector('.ld-grid tbody').addEventListener('contextmenu', (e) => {
    const cell = e.target.closest('td.work');
    if (!cell || !el.classList.contains('manual-on') || el.classList.contains('algo-test')) return;
    e.preventDefault();
    const price = parseFloat(cell.parentElement.querySelector('td.price').textContent);
    cancelManual(el, key, (o) => Math.abs(Number(o.ticket.price) - price) < 1e-9,
      'the manual orders at ' + cell.parentElement.querySelector('td.price').textContent);
  });
  document.getElementById('desktop').appendChild(el);
  const place = state.places[wkey];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

async function closeAtLimitAsk(el, key) {
  const box = el.querySelector('.ld-close-px');
  if (ladderManualPositions(key).length) {
    const price = parseFloat(box.value);
    if (!(price || price === 0)) { toast('REJECT', 'CLOSE @ LMT', 'type the price to close at first', key); return; }
    return closeManual(el, key, price);
  }
  const resting = el.querySelector('.ld-closelmt').classList.contains('resting');
  const name = el.querySelector('.title').textContent;
  if (resting) {
    const ok = await ask('Pull the Close @ LMT on ' + name + '?',
      'The resting closing limit is cancelled (a request, at a venue). The position stays open.',
      'Pull it');
    if (ok) await command('cancel_close_limit', key);
    return;
  }
  const price = parseFloat(box.value);
  if (!(price || price === 0)) { toast('REJECT', 'CLOSE @ LMT', 'type the price to close at first', key); return; }
  const ok = await ask('Close ' + name + ' at ' + box.value + '?',
    'ONE closing limit rests at ' + box.value + ' for the whole open position, carrying its ' +
    'tickets (77=C). It waits there — a target, and no stop. The Algo on this contract is stood ' +
    'down with it. CLOSE ALL still crosses at once.', 'Rest the close');
  if (ok) {
    const r = await command('close_limit', key, { price });
    if (r && r.ok) toast('ORDER', 'CLOSE @ LMT', 'resting at ' + box.value, key);
  }
}

/* SIGNALS mode: the Algo's call, said ONCE — read off `signal_alert.seq`,
 * never off the wording of an event line. First sight of a contract is not
 * an alert: a reload must not replay the last one. */
const signalSeen = {};

function signalAlert(el, c) {
  const a = c.signal_alert;
  const seq = (a && a.seq) || 0;
  if (signalSeen[c.key] === undefined) { signalSeen[c.key] = seq; return; }
  if (seq === signalSeen[c.key]) return;
  signalSeen[c.key] = seq;
  if (!a) return;
  const exit = a.kind === 'EXIT';
  toast(exit ? 'EXIT_SIGNAL' : 'SIGNAL', exit ? 'SIGNAL · EXIT' : 'SIGNAL · ENTRY',
    a.text, c.name);
  el.classList.remove('signal-flash');
  void el.offsetWidth;
  el.classList.add('signal-flash');
}

/* -- hand trading on the desk ladder (MANUAL mode) ------------------------- */

const ladderData = {};           // key -> {c, engine}: the last snapshot drawn

function ladderManual(key) {
  const d = ladderData[key];
  return (d && (d.engine || {}).manual_terminal) || null;
}

function ladderSecurity(key) {
  const d = ladderData[key];
  return d ? String(d.c.security_id || '') : '';
}

// This contract's manual orders still working at Stellar.
function ladderManualWorking(key) {
  const term = ladderManual(key), sid = ladderSecurity(key);
  if (!term || !sid) return [];
  return (term.orders || []).filter((o) => o.ticket && String(o.ticket.security_id) === sid &&
    ['PENDING', 'NEW', 'PARTIALLY_FILLED', 'REPLACED'].includes(o.status));
}

// This contract's open manual fills, one row per opening ticket.
function ladderManualPositions(key) {
  const term = ladderManual(key), sid = ladderSecurity(key);
  if (!term || !sid) return [];
  return ((term.pnl || {}).positions || []).filter((p) => String(p.security_id) === sid);
}

async function manualOrder(el, key, side, price) {
  if (!el.classList.contains('manual-on')) return;
  const d = ladderData[key];
  if (!d) return;
  if (el.classList.contains('algo-test')) return algoTestOrder(el, key, side, price);
  const m = d.c.market || {};
  const ot = el.querySelector('.ld-ot').value;
  const tif = el.querySelector('.ld-tif').value;
  const qty = parseFloat(el.querySelector('.ld-qtybox.' + (side === 'BUY' ? 'buy' : 'sell')).value);
  if (!(qty > 0)) { toast('REJECT', 'NOT SENT', 'type the ' + side + ' quantity first', key); return; }
  let px = price;
  if (px === null && ot === 'LIMIT') px = side === 'BUY' ? m.ask : m.bid;   // at the touch
  if (ot === 'LIMIT' && (px === null || px === undefined)) {
    toast('REJECT', 'NOT SENT', 'no ' + (side === 'BUY' ? 'offer' : 'bid') + ' to price the limit at', key);
    return;
  }
  const term = ladderManual(key);
  const args = { security_id: d.c.security_id, account: term.account || '', side,
                 order_type: price !== null ? 'LIMIT' : ot, quantity: String(qty),
                 price: (price !== null || ot === 'LIMIT') ? String(px) : '', tif,
                 open_close: 'O' };
  const review = await command('terminal_preview', '', args);
  if (!review || !review.ok || !review.token) return;      // refused: already toasted
  const t = review.ticket || {};
  const dec = d.c.decimals === undefined ? 4 : d.c.decimals;
  const ok = await ask(side + ' ' + t.quantity + ' ' + (d.c.name || key) + ' — send to Stellar?',
    side + ' ' + t.quantity + ' @ ' + (t.order_type === 'MARKET' ? 'MARKET' : num(Number(t.price), dec) +
    ' LIMIT') + ' · ' + t.tif + ' · account ' + t.account + ' · Open/Close: OPEN (77=O). ' +
    'A manual ticket (FTM-), sent to Stellar ' + ((d.engine || {}).environment || '') +
    ' on your confirmation; an acknowledgement or fill comes from Stellar.',
    'Send ' + side, review.book);
  if (!ok) return;
  const sent = await command('terminal_submit', '', { token: review.token, confirmed: true });
  if (sent && sent.ok) toast('ORDER', 'SENT', side + ' ' + t.quantity + ' — ' + (sent.order_id || ''), key);
}

/* UAT, on the Order tests page, with the Algo TRADING this contract: the
 * ladder sends through the ALGO's own order path — the executor, an FT- id,
 * 77=O, 452=24 — as its signal would on a live market; the Algo's TP / SL
 * then manage the position. The engine refuses it on any venue but UAT. */
async function algoTestOrder(el, key, side, price) {
  const d = ladderData[key];
  const m = d.c.market || {};
  const ot = price !== null ? 'LIMIT' : el.querySelector('.ld-ot').value;
  const qty = parseFloat(el.querySelector('.ld-qtybox.' + (side === 'BUY' ? 'buy' : 'sell')).value);
  if (!(qty > 0)) { toast('REJECT', 'NOT SENT', 'type the ' + side + ' quantity first', key); return; }
  let px = price;
  if (px === null && ot === 'LIMIT') px = side === 'BUY' ? m.ask : m.bid;
  if (ot === 'LIMIT' && (px === null || px === undefined)) {
    toast('REJECT', 'NOT SENT', 'no ' + (side === 'BUY' ? 'offer' : 'bid') + ' to price the limit at', key);
    return;
  }
  const dec = d.c.decimals === undefined ? 4 : d.c.decimals;
  const ok = await ask('Algo test order — ' + side + ' ' + qty + ' ' + (d.c.name || key) + '?',
    side + ' ' + qty + ' @ ' + (ot === 'MARKET' ? 'MARKET' : num(Number(px), dec) + ' LIMIT (rests there)') +
    ' through the ALGO\'s own order path to Stellar UAT — an FT- order, 77=O, 452=24, exactly as its ' +
    'signal would send it on a live market. The Algo then manages the position: its take-profit and ' +
    'stop loss, as on the Algo window. CLOSE ALL and Close @ LMT close it as they would live.',
    'Send ' + side);
  if (!ok) return;
  const args = { side, order_type: ot, qty };
  if (ot === 'LIMIT') args.price = px;
  const r = await command('uat_order', key, args);
  if (r && r.ok) toast('ORDER', 'ALGO TEST', side + ' ' + qty + ' ' + ot + ' — ' + r.clordid, key);
  else if (r && r.error) toast('REJECT', 'NOT SENT', r.error, key);
}

async function cancelManual(el, key, which, label) {
  const orders = ladderManualWorking(key).filter((o) => !o.pending && which(o));
  if (!orders.length) { toast('REJECT', 'CANCEL', 'no manual order to cancel', key); return; }
  const ok = await ask('Cancel ' + label + '?',
    orders.length + ' manual order(s): ' + orders.map((o) => o.id).join(', ') +
    '. Each stays working until Stellar confirms the cancel — and can fill in between.', 'Request cancel');
  if (!ok) return;
  for (const o of orders) await command('terminal_cancel', '', { order_id: o.id });
}

// CLOSE ALL (price null: at market) or Close @ LMT on the manual position:
// one CLOSING ticket (77=C) per opening ticket, for its open fills and no more.
async function closeManual(el, key, price) {
  const open = ladderManualPositions(key);
  if (!open.length) return;
  const reviews = [];
  for (const p of open) {
    const r = await command('terminal_preview_close', '', price === null
      ? { order_id: p.entry_order_id } : { order_id: p.entry_order_id, price: String(price) });
    if (!r || !r.ok) return;                                  // refused: already toasted
    reviews.push(r);
  }
  const lines = reviews.map((r) => (r.ticket || {}).side + ' ' + (r.ticket || {}).quantity +
    (price === null ? ' at MARKET' : ' @ ' + price + ' LIMIT') + ' closing ' + r.close_of);
  const ok = await ask((price === null ? 'CLOSE ALL' : 'Close @ LMT') + ' — ' +
    el.querySelector('.title').textContent + '?',
    lines.join('; ') + '. Open/Close: CLOSE (77=C), capped at what each ticket has open.',
    price === null ? 'CLOSE NOW' : 'Rest the close', (reviews[0] || {}).book);   // shown, never a block
  if (!ok) return;
  for (const r of reviews) await command('terminal_submit', '', { token: r.token, confirmed: true });
  toast('ORDER', 'CLOSING', lines.join('; '), key);
}

function renderLadder(c, engine) {
  const wkey = '__ladder__' + c.key;
  if (state.closed.has(wkey)) return;
  const el = ladderFor(c.key);
  const d = c.decimals === undefined ? 4 : c.decimals;
  const tick = c.tick_size || Math.pow(10, -d);
  const pref = ladderPref(c.key);
  const inc = pref.increment || tick;
  const m = c.market || {};
  const block = c.algo || {};
  const pos = c.position;
  const ex = (engine || {}).execution || {};
  const algoState = c.algo_state || (c.algo_on ? 'DRY' : 'OFF');
  const algoTrades = algoState === 'PAPER' || algoState === 'LIVE';
  const st = c.settings || {};
  const orders = c.orders || [];
  const has = (v) => v !== null && v !== undefined;
  ladderData[c.key] = { c, engine };
  signalAlert(el, c);
  const term = (engine || {}).manual_terminal;
  // Hand trading on this ladder: its Algo not trading it (Off or Signals)
  // and holding nothing here, a Stellar manual ticket behind it, and the session
  // up. Anything else and the order controls are off — and the banner says
  // which.
  const sessionUp = ((engine || {}).session || {}).state === 'LOGGED_ON';
  // On the Order tests page, on UAT, with the Algo TRADING on LIVE: the
  // ladder is the Algo's — its orders go the Algo's own way (algoTestOrder).
  const algoTest = EMBED !== null && algoTrades && (engine || {}).environment === 'UAT' &&
    ex.mode === 'LIVE' && !!term && !!c.security_id && sessionUp && !(c.position || {}).side &&
    !(c.orders || []).length;
  const manualOn = algoTest || (!c.manual_block && !!term && !!c.security_id && sessionUp &&
    (engine || {}).alive !== false);
  el.classList.toggle('manual-on', manualOn);
  el.classList.toggle('algo-test', algoTest);
  const mWorking = ladderManualWorking(c.key);
  const mOpen = ladderManualPositions(c.key);
  // SIGNALS mode: the trader's position as the Algo watches it — with the
  // Algo's break-even, take-profit and stop loss, and its net P&L.
  const watched = (block.positions || []).find((x) => x.manual) || null;

  el.querySelector('.title').textContent = c.name;
  el.querySelector('.ld-route').textContent = c.security_id || c.symbol || '';
  const algoOt = String(st.entry_order_type || 'MARKET').toUpperCase();
  const otSel = el.querySelector('.ld-ot'), tifSel = el.querySelector('.ld-tif');
  otSel.disabled = tifSel.disabled = !manualOn;
  // MANUAL: the trader's own choice on this ladder. ALGO: what the Algo sends.
  if (manualOn) {
    if (document.activeElement !== otSel) otSel.value = pref.ot || 'LIMIT';
    if (document.activeElement !== tifSel) tifSel.value = pref.tif || 'DAY';
  } else {
    otSel.value = algoOt === 'LIMIT' ? 'LIMIT' : 'MARKET';
    tifSel.value = 'DAY';
  }
  const ot = otSel.value;
  el.querySelector('.ld-ordtype').textContent = ot + ' · ' + tifSel.value;

  // Who trades this contract: the ONE Algo switch, as on the Algo window.
  renderAlgoSwitch(el, c);

  // The quote strip: the mid against today's open, and the day's H/L/O —
  // ours, from the mids watched, and the badge says so.
  const hlo = c.hlo || {};
  const chg = has(m.mid) && has(hlo.open) ? m.mid - hlo.open : null;
  const net = el.querySelector('.ld-netchg');
  net.textContent = chg === null ? DASH : (chg > 0 ? '+' : '') + chg.toFixed(d);
  net.className = 'ld-netchg ' + (chg > 0 ? 'up' : chg < 0 ? '' : 'flat');
  el.querySelector('.ld-h').textContent = num(hlo.high, d);
  el.querySelector('.ld-l').textContent = num(hlo.low, d);
  el.querySelector('.ld-o').textContent = num(hlo.open, d);
  el.querySelector('.ld-lt').textContent = num(c.last_trade, d);

  const lock = el.querySelector('.ld-lock');
  const lockText = el.querySelector('.ld-lock-text');
  const word = ALGO_WORDS[algoState] || algoState;
  if (algoTest) {
    lock.className = 'ld-lock algo-test';
    lockText.textContent = word + ' — BUY / SELL or a click in Bids / Asks makes the Algo send its ' +
      'order to Stellar UAT now, as on a signal. Its TP / SL then manage it.';
    lock.title = 'Order tests page only, on Stellar UAT. One test position at a time.';
  } else if (algoTrades && EMBED !== null) {
    lock.className = 'ld-lock';
    lockText.textContent = word + ' — ' + ((c.position || {}).side || (c.orders || []).length
      ? 'the Algo holds this contract: CLOSE ALL or Close @ LMT, then the next test order.'
      : ex.mode !== 'LIVE' ? 'set the Algo to UAT to send its orders to Stellar UAT.'
      : 'the Stellar session is not up.');
    lock.title = '';
  } else if (algoTrades) {
    lock.className = 'ld-lock';
    lockText.textContent = word + ' — manual orders are off on this ladder. ' +
      'CLOSE ALL and Close @ LMT still close.';
    lock.title = 'Set the Algo to Off or Signals (the button in the title bar) to trade ' +
      'this contract by hand.';
  } else if (c.manual_block) {
    lock.className = 'ld-lock';
    lockText.textContent = 'Manual orders are off: ' + c.manual_block;
    lock.title = '';
  } else if (manualOn) {
    lock.className = 'ld-lock manual';
    lockText.textContent = (algoState === 'DRY' ? 'SIGNALS — the Algo signals and watches your TP/SL, you trade. '
      : 'ALGO OFF — you trade. ') + 'BUY / SELL, or click a price: Bids buys, Asks sells.';
    lock.title = 'Every order is reviewed before it goes to Stellar: a manual ticket (FTM-), with ' +
      'the same checks and safety limits as Instruments & orders. Right-click a Work cell to ' +
      'cancel there.';
  } else {
    lock.className = 'ld-lock manual';
    lockText.textContent = word + ' — ' + (!term ? 'hand orders need the Stellar session (not the simulator).'
      : !c.security_id ? 'this contract has no Stellar Security ID.'
      : 'the Stellar session is not logged on.') + ' CLOSE ALL still closes.';
    lock.title = '';
  }
  el.classList.toggle('algo-locked', !manualOn);

  // A close that did NOT happen, said loudly on the ladder: the Algo's (in
  // the engine's words) or a manual ticket's (in Stellar's), until a close fills.
  const alerts = [];
  if (c.close_alert) alerts.push(c.close_alert.text);
  ((term || {}).close_alerts || []).forEach((a) => {
    if (String(a.security_id) === String(c.security_id)) alerts.push(a.text);
  });
  let ca = el.querySelector('.ld-closealert');
  if (!ca) {
    ca = document.createElement('div');
    ca.className = 'ld-closealert';
    lock.insertAdjacentElement('afterend', ca);
  }
  ca.hidden = !alerts.length;
  ca.textContent = alerts.join(' · ');

  // The rail.
  el.querySelector('.ld-rb-sym').innerHTML = 'Contract <b>' + esc(c.symbol || c.name) + '</b>';
  el.querySelector('.ld-rb-id').innerHTML = 'Stellar ID <b>' + esc(c.security_id || DASH) + '</b>';
  const acct = ((engine || {}).manual_terminal || {}).account;
  el.querySelector('.ld-rb-acct').innerHTML = 'Account <b>' + esc(acct || (engine || {}).environment || DASH) + '</b>';
  el.querySelector('.ld-quoting').textContent = ex.mode === 'PAPER' || ex.mode === 'SIMULATOR'
    ? 'PAPER: filled here at the bid/offer'
    : ot === 'LIMIT' ? 'LIMIT: rests at the touch, escalates' : 'MARKET: crosses the touch';
  if (manualOn) el.querySelector('.ld-quoting').textContent =
    ot === 'LIMIT' ? 'LIMIT: BUY at the offer, SELL at the bid' : 'MARKET: crosses now';
  const q = st.quantity || 1;
  const qb = el.querySelector('.ld-qtybox.buy'), qs = el.querySelector('.ld-qtybox.sell');
  [qb, qs].forEach((b) => {
    b.disabled = !manualOn;
    // The trader's own size stays put; it is only filled in when empty.
    if (!manualOn || (b.value === '' && document.activeElement !== b && !b.dataset.cleared)) b.value = q;
  });
  el.querySelectorAll('.ld-keypad button').forEach((b) => { b.disabled = !manualOn; });
  el.querySelector('.ld-buy').disabled = !manualOn;
  el.querySelector('.ld-sell').disabled = !manualOn;
  el.querySelector('.ld-buy').textContent = 'BUY ' + (qb.value || '');
  el.querySelector('.ld-sell').textContent = 'SELL ' + (qs.value || '');
  el.querySelector('.ld-flatten').disabled = !pos && !mOpen.length;
  const cl = c.close_limit;
  const lmtRow = el.querySelector('.ld-closelmt');
  lmtRow.classList.toggle('resting', !!cl);
  const go = el.querySelector('.ld-close-go');
  go.disabled = !pos && !cl && !mOpen.length;
  go.textContent = cl ? 'Pull' : '@ LMT';
  go.title = cl ? 'A Close @ LMT is resting at ' + num(cl.price, d) +
    (cl.paper ? ' (PAPER)' : '') + ' — click to pull it' : go.title;
  const box = el.querySelector('.ld-close-px');
  if (cl && document.activeElement !== box) box.value = num(cl.price, d);
  if (!cl && !pos && !mOpen.length && document.activeElement !== box) box.value = '';

  const working = orders.filter((o) => !['FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'].includes(o.state));
  // The manual tickets working on this contract are on the ladder too.
  mWorking.forEach((o) => working.push({ side: o.ticket.side,
    price: o.ticket.price === null || o.ticket.price === undefined || o.ticket.price === '' ? null : Number(o.ticket.price),
    qty: Number(o.ticket.quantity), filled_qty: Number(o.filled_qty || 0), manual: true }));
  const nB = working.filter((o) => o.side === 'BUY').length;
  const nS = working.filter((o) => o.side === 'SELL').length;
  el.querySelector('.ld-n-b').textContent = nB || '';
  el.querySelector('.ld-n-s').textContent = nS || '';
  el.querySelector('.ld-n-all').textContent = working.length || '';
  el.querySelector('.ld-cxl-b').disabled = !nB;
  el.querySelector('.ld-cxl-s').disabled = !nS;
  el.querySelector('.ld-cxl-all').disabled = !working.length;
  el.querySelector('.ld-cnt-b').textContent = 'B:' + nB;
  el.querySelector('.ld-cnt-s').textContent = 'S:' + nS;
  el.querySelector('.ld-cnt-w').textContent = 'W:' + working.length;

  // The footer: the feed, the contract's own book, the two directions.
  const feed = c.feed || {};
  const fe = el.querySelector('.ld-feed');
  fe.textContent = feed.stale ? 'STALE (' + (has(feed.age_sec) ? feed.age_sec + 's' : 'no quote') + ')'
    : feed.settling ? 'SETTLING after a jump' : has(feed.age_sec) ? 'OK (quote ' + feed.age_sec + 's)' : 'no quote yet';
  fe.className = 'ld-feed ' + (feed.stale ? 'bad' : feed.settling ? 'warn' : 'ok');
  // Why there are no prices, when there are none: Stellar refused (permissions),
  // nobody quotes it, or Stellar has not answered — the engine's words.
  const md = c.md || null;
  if (md && !['LIVE', 'ONE_SIDED'].includes(md.state)) {
    fe.textContent = ({ REFUSED: 'REFUSED by Stellar', NO_ANSWER: 'NO ANSWER from Stellar',
      EMPTY: 'NO QUOTES on UAT', WAITING: 'requesting…', NO_SESSION: 'MD not connected',
      NOT_REQUESTED: 'not requested', NO_ID: 'no Security ID' })[md.state] || md.state;
    fe.className = 'ld-feed ' + (md.state === 'WAITING' ? 'warn' : 'bad');
  }
  fe.title = md ? md.text : '';
  el.querySelector('.ld-bk-sym').textContent = c.symbol || c.name;
  el.querySelector('.ld-bk-bid').textContent = num(m.bid, d);
  el.querySelector('.ld-bk-ask').textContent = num(m.ask, d);
  el.querySelector('.ld-bk-spr').textContent = has(m.bid) && has(m.ask) ? (m.ask - m.bid).toFixed(d) : DASH;
  el.querySelector('.ld-bk-age').textContent = has(feed.age_sec) ? feed.age_sec + 's' : DASH;
  const armed = block.armed || {};
  const hl = el.querySelector('.ld-hl'), lh = el.querySelector('.ld-lh');
  hl.classList.toggle('armed', !!armed.SELL);
  lh.classList.toggle('armed', !!armed.BUY);
  el.querySelector('.ld-zb').textContent = signed(block.z_sell, 2);
  el.querySelector('.ld-za').textContent = signed(block.z_buy, 2);
  const pe = el.querySelector('.ld-pos'), pn = el.querySelector('.ld-pnl');
  if (pos) {
    pe.textContent = (pos.side === 'BUY' ? '+' : '-') + pos.qty + ' @ ' + num(pos.avg_price, d);
    pn.textContent = has(pos.net) ? money(pos.net) : DASH;
    pn.className = 'ld-pnl ' + (pos.net > 0 ? 'up' : pos.net < 0 ? 'dn' : '');
    pn.title = 'Net after the round trip, at the side it would close on';
  } else if (mOpen.length) {
    // The manual position: its open fills, netted for the label only.
    const net = mOpen.reduce((a, p) => a + (p.side === 'BUY' ? 1 : -1) * p.quantity, 0);
    const avg = mOpen.reduce((a, p) => a + p.entry_price * p.quantity, 0) /
      mOpen.reduce((a, p) => a + p.quantity, 0);
    const fl = mOpen.every((p) => has(p.floating_pnl))
      ? mOpen.reduce((a, p) => a + p.floating_pnl, 0) : null;
    pe.textContent = (net > 0 ? '+' : '') + net + ' @ ' + num(avg, d) + ' (manual)' +
      (watched ? ' · TP ' + num(watched.tp, d) + ' · SL ' +
        (has(watched.sl) ? num(watched.sl, d) : 'off') : '');
    const shown = watched && has(watched.net_pnl) ? watched.net_pnl : fl;
    pn.textContent = has(shown) ? money(shown) : DASH;
    pn.className = 'ld-pnl ' + (shown > 0 ? 'up' : shown < 0 ? 'dn' : '');
    pn.title = watched ? 'Net after the round trip, at the side it would close on — ' +
      'the Algo watching your position (Signals)'
      : 'Gross, marked at the Stellar mid — the manual ticket\'s own figure';
  } else {
    pe.textContent = 'flat';
    pn.textContent = '';
  }

  // The grid, centred on the market until Centre — or held, with Lock.
  const mid = m.mid;
  const body = el.querySelector('.ld-grid tbody');
  if (!has(mid)) {
    body.innerHTML = '<tr><td colspan="5" class="price">' + esc(c.market_note || 'no price yet') + '</td></tr>';
    return;
  }
  let recentred = false;
  if (ladderCentre[c.key] === undefined ||
      (!pref.lock && Math.abs(mid - ladderCentre[c.key]) > inc * (LADDER_ROWS / 2))) {
    ladderCentre[c.key] = mid;
    recentred = true;
  }
  const round = (p) => Math.round(p / inc) * inc;
  const key = (p) => round(p).toFixed(d);
  const top = round(ladderCentre[c.key]) + LADDER_ROWS * inc;
  const levels = {};
  function level(price, label, cls) {
    if (!has(price)) return;
    (levels[key(price)] = levels[key(price)] || []).push('<span class="lv ' + cls + '">' + label + '</span>');
  }
  if (block.ready) {
    level(block.mean, 'MEAN', 'mean');
    level(block.upper, '+' + (block.params || {}).entry_z + 'σ', 'band');
    level(block.lower, '−' + (block.params || {}).entry_z + 'σ', 'band');
  }
  mOpen.forEach((p) => level(p.entry_price, 'ENTRY', 'en'));
  // SIGNALS: the Algo watches the trader's position — its levels go on the
  // ladder like the Algo's own.
  if (watched && !pos) {
    level(watched.break_even, 'BE', 'be');
    level(watched.tp, 'TP', 'tp');
    level(watched.sl, 'SL', 'sl');
  }
  if (pos) {
    level(pos.avg_price, 'ENTRY', 'en');
    level(pos.break_even, 'BE', 'be');
    level(pos.target, 'TP', 'tp');
    level(pos.stop, 'SL', 'sl');
  }
  const work = {};
  working.forEach((o) => {
    if (!has(o.price)) return;
    const k = key(o.price);
    work[k] = work[k] || { BUY: 0, SELL: 0 };
    work[k][o.side] += (o.qty || 0) - (o.filled_qty || 0);
  });
  if (cl && cl.paper) {
    const k = key(cl.price);
    work[k] = work[k] || { BUY: 0, SELL: 0, held: true };
    work[k].held = true;
    work[k][cl.side] += pos ? pos.qty : 0;
  }
  const bidK = has(m.bid) ? key(m.bid) : null;
  const askK = has(m.ask) ? key(m.ask) : null;
  // The book: every level Stellar sends (FULL depth), summed into the ladder's
  // rows; without depth, the touch alone. A level is Stellar's, never inferred.
  const depth = c.depth || null;
  const bidSz = {}, askSz = {};
  const addLevels = (levels, into) => (levels || []).forEach((lv) => {
    if (!has(lv.price)) return;
    const k = key(lv.price);
    into[k] = (into[k] || 0) + (has(lv.size) ? lv.size : 0);
  });
  if (depth && (depth.bids || []).length) addLevels(depth.bids, bidSz);
  else if (bidK !== null && has(m.bid_size)) bidSz[bidK] = m.bid_size;
  if (depth && (depth.asks || []).length) addLevels(depth.asks, askSz);
  else if (askK !== null && has(m.ask_size)) askSz[askK] = m.ask_size;
  const dBtn = el.querySelector('.ld-depth');
  dBtn.dataset.full = depth && depth.full ? '1' : '';
  dBtn.textContent = 'Depth: ' + (depth && depth.full ? 'FULL' : 'TOP');
  dBtn.classList.toggle('on', !!(depth && depth.full));
  dBtn.disabled = !depth;
  const fmtSz = (v) => (Math.round(v * 1e6) / 1e6).toString();
  const midK = key(mid);
  const ltK = has(c.last_trade) ? key(c.last_trade) : null;
  let html = '';
  for (let i = 0; i <= LADDER_ROWS * 2; i += 1) {
    const k = (top - i * inc).toFixed(d);
    const w = work[k];
    const isBid = k === bidK, isAsk = k === askK;
    if (pref.filter && !w && !levels[k] && !isBid && !isAsk && k !== ltK && k !== midK &&
        !(k in bidSz) && !(k in askSz)) continue;
    let workCell = '<td class="work">';
    if (w && (w.BUY || w.SELL)) {
      const side = w.BUY ? 'buy' : 'sell';
      workCell = '<td class="work ' + side + (w.held ? ' held' : '') + '" title="' +
        (w.held ? 'Close @ LMT resting on PAPER' : 'our working ' + side) + '">' +
        (w.BUY || w.SELL) + ' ';
    }
    workCell += (levels[k] || []).join('') + '</td>';
    html += '<tr class="' + (k === midK ? 'mid-line' : '') + '">' + workCell +
      '<td class="bid' + (k in bidSz ? ' has-qty' : '') + (isBid ? ' touch' : '') + '">' +
        (k in bidSz ? fmtSz(bidSz[k]) : '') + '</td>' +
      '<td class="price' + (k === ltK ? ' last-trade' : '') + (isBid ? ' at-bid' : '') + (isAsk ? ' at-ask' : '') + '">' + k + '</td>' +
      '<td class="ask' + (k in askSz ? ' has-qty' : '') + (isAsk ? ' touch' : '') + '">' +
        (k in askSz ? fmtSz(askSz[k]) : '') + '</td>' +
      '<td class="ltq' + (k === ltK ? ' print' : '') + '" title="' + (k === ltK ? 'Stellar last trade' : '') + '">' +
      (k === ltK ? '●' : '') + '</td></tr>';
  }
  body.innerHTML = html;
  if (recentred || el.dataset.recentre) {
    delete el.dataset.recentre;
    const grid = el.querySelector('.ld-grid');
    const row = body.querySelector('tr.mid-line') || body.rows[Math.floor(body.rows.length / 2)];
    if (row) grid.scrollTop = row.offsetTop - grid.clientHeight / 2;
  }
}

/* -- marking the window ---------------------------------------------------
 *
 * BLUE while a position is open, and a flash of GREEN or RED when one
 * closes. Two different kinds of thing, marked differently on purpose: the
 * blue is a STATE read straight off the snapshot, so it is still there after
 * a page reload and it cannot get stuck on a contract that is flat. The
 * flash is an EVENT, so it fades — a window left red says "this is losing",
 * which is a different statement from "the last trade lost".
 *
 * The close is read from `last_close.seq`, not from the wording of the event
 * line. A highlight driven by a regex over a sentence stops working the day
 * somebody rewords the sentence, and stops working silently.
 */
const HIGHLIGHT_HOLD_MS = 2600;
const marks = {};        // key -> { seq, timer }

function markPosition(el, c) {
  const open = !!(c.position && c.position.qty);
  el.classList.toggle('in-position', open);

  const close = c.last_close;
  const seq = (close && close.seq) || 0;
  const seen = marks[c.key];

  // First sight of a contract is not an event. Whatever it had closed
  // before this page loaded is history — without this, reloading after a
  // session flashes every window at once for trades nobody was watching.
  // Note it is recorded even when there is NO close yet: "watched, and it
  // had not closed anything" and "never seen" are different, and confusing
  // them swallows the first close of the session.
  if (seen === undefined) {
    marks[c.key] = { seq: seq, timer: null };
    return;
  }
  if (seq === seen.seq) return;                    // already marked this one
  seen.seq = seq;
  if (!seq) return;                                // nothing to mark
  flash(el, closeColour(close.net));
}

/* Green for a profit, red for a loss — and NEITHER for the two cases that
 * are not either. A net of exactly zero is not a win. A net of null is
 * UNMEASURED, and colouring it red would state a loss the system never
 * measured. Both get the neutral mark. */
function closeColour(net) {
  if (net === null || net === undefined) return 'closed-flat';
  if (net > 0) return 'closed-up';
  if (net < 0) return 'closed-down';
  return 'closed-flat';
}

function flash(el, cls) {
  const key = el.dataset.key;
  const mark = marks[key] || (marks[key] = {});
  if (mark.timer) clearTimeout(mark.timer);
  el.classList.remove('closed-up', 'closed-down', 'closed-flat', 'fading');
  // Forces the browser to notice the class went away before it comes back,
  // so two closes in a row flash twice rather than once.
  void el.offsetWidth;
  el.classList.add(cls);
  mark.timer = setTimeout(() => {
    el.classList.add('fading');
    mark.timer = setTimeout(() => {
      el.classList.remove('closed-up', 'closed-down', 'closed-flat', 'fading');
      mark.timer = null;
    }, 1700);
  }, HIGHLIGHT_HOLD_MS);
}

/* -- the Trading Monitor window ---------------------------------------------
 *
 * The MT5 desk's Trading Monitor, on the desk: Positions, Working orders,
 * Fills (the Stellar fills tape), Closed trades, Slippage and the Reconciler —
 * the SAME renderer as the Account tab (monitor.js), fed this desk's
 * snapshot on every tick rather than polling for itself. What the VENUE says
 * sits beside what this book holds, because the two disagreeing is the whole
 * reason to look.
 */

let tradingMonitor = null;

function positionsWindow() {
  let el = document.querySelector('.win[data-key="__positions__"]');
  if (el) return el;
  const tpl = document.getElementById('positions-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  wireWindow(el, '__positions__');
  document.getElementById('desktop').prepend(el);
  const place = state.places['__positions__'];
  if (place) placeWindow(el, place.x, place.y);
  tradingMonitor = TradingMonitor(el.querySelector('.tmon'), {
    storageKey: LS + 'monitor.tab',
    // The desk's own command(): it toasts a refusal itself, so the monitor
    // is told of it as an error and does not say it twice.
    command: async (action, contract, args) => {
      const r = await command(action, contract, args);
      if (!r || r.ok === false) throw new Error((r && r.error) || 'refused');
      return r;
    },
    ask: ask,
    say: (text, bad) => { if (!bad) toast('OK', 'MONITOR', text); },
  });
  return el;
}

function renderPositions(snap) {
  if (state.closed.has('__positions__')) return;
  const el = positionsWindow();
  const rows = ((snap.portfolio || {}).rows || []).filter((r) => r.side);
  const manual = (((snap.engine || {}).manual_terminal || {}).pnl || {}).positions || [];
  el.querySelector('.tm-count').textContent = (rows.length + manual.length) + ' open';
  if (!el.classList.contains('minimised')) tradingMonitor.update(snap);
}

/* -- the Analysis window --------------------------------------------------
 *
 * The feedback loop. One contract at a time by default, because a win rate
 * blended over eight of them cannot answer the only question it exists for.
 * It fetches its own data on a slow timer — this is history, not a market,
 * and re-reading it twice a second would be pointless work on the desk's
 * critical path.
 */

const analysis = { key: null, period: 'all', mode: 'live', timer: null,
                   modeChosen: false,
                   //: Which load is current. Two are easily in flight at
                   //: once — a poll and a filter change — and if the slower
                   //: one renders last the window shows one filter's rows
                   //: under another filter's label. On this window that is
                   //: not cosmetic: it is simulated trades appearing in a
                   //: figure labelled live.
                   seq: 0,
                   //: The replay is expensive and its answer does not change
                   //: with a poll — only with the contract or the period.
                   replayFor: null, replayCache: null };

function analysisWindow() {
  let el = document.querySelector('.win[data-key="__analysis__"]');
  if (el) return el;
  const tpl = document.getElementById('analysis-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.querySelector('.close').onclick = () => {
    state.closed.add('__analysis__');
    localStorage.setItem(LS + 'closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  el.querySelector('.an-period').onchange = (e) => {
    analysis.period = e.target.value; loadAnalysis();
  };
  el.querySelector('.an-mode').onchange = (e) => {
    analysis.mode = e.target.value;
    analysis.modeChosen = true;        // a chosen filter is never overridden
    loadAnalysis();
  };
  el.onmousedown = () => raise(el);
  makeDraggable(el, '__analysis__');
  document.getElementById('desktop').appendChild(el);
  const place = state.places['__analysis__'];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

function tile(k, v, sub, cls) {
  return '<div class="tile"><div class="k">' + k + '</div>' +
    '<div class="v ' + (cls || '') + '">' + v + '</div>' +
    '<div class="s">' + (sub || '') + '</div></div>';
}

function minutes(m) {
  if (m === null || m === undefined) return DASH;
  if (m < 60) return Math.round(m) + 'm';
  return Math.floor(m / 60) + 'h ' + Math.round(m % 60) + 'm';
}

function seconds(s) {
  if (s === null || s === undefined) return DASH;
  return s < 90 ? Math.round(s) + 's' : minutes(s / 60);
}

function pct(v, d) {
  return (v === null || v === undefined) ? DASH : Number(v).toFixed(d || 1) + '%';
}

function bar(width, negative) {
  return '<div class="cbar"><i class="' + (negative ? 'neg' : 'pos') +
    '" style="width:' + Math.max(2, Math.min(100, width)) + '%"></i></div>';
}

function renderTiles(el, s) {
  el.querySelector('.an-tiles').innerHTML =
    tile('Trades', s.trades, s.won + ' won / ' + s.lost + ' lost' +
      (s.unmeasured ? ' · ' + s.unmeasured + ' unmeasured' : '')) +
    tile('Win rate', pct(s.win_rate), 'of ' + s.measured + ' measured') +
    tile('Net P&L', money(s.net), s.fees === null ? 'costs not measured'
      : 'after ' + money(s.fees).replace('+', '') + ' costs',
      s.net > 0 ? 'up' : s.net < 0 ? 'dn' : '') +
    tile('Avg / trade', money(s.avg),
      'win ' + money(s.avg_win) + ' · loss ' + money(s.avg_loss),
      s.avg > 0 ? 'up' : s.avg < 0 ? 'dn' : '') +
    tile('Expectancy', money(s.expectancy), 'per trade, weighted',
      s.expectancy > 0 ? 'up' : s.expectancy < 0 ? 'dn' : '') +
    tile('On margin', pct(s.on_margin, 2),
      s.on_margin === null ? 'margin not reported' : 'return on what was tied up',
      s.on_margin > 0 ? 'up' : s.on_margin < 0 ? 'dn' : '') +
    tile('Cost drag', pct(s.cost_drag), 'of gross') +
    tile('Avg hold', minutes(s.avg_hold_min), '') +
    tile('Worst run', money(s.worst_run), 'consecutive losses',
      s.worst_run < 0 ? 'dn' : '');
}

function renderTouches(el, study, threshold) {
  const body = el.querySelector('.an-touches tbody');
  body.innerHTML = '';
  const most = Math.max(1, ...study.levels.map((l) => l.touches));
  study.levels.forEach((l) => {
    const tr = document.createElement('tr');
    const tag = Math.abs(l.level) >= (threshold || 2)
      ? (l.level > 0 ? 't-sell' : 't-buy') : 't-cxl';
    tr.innerHTML =
      '<td><span class="tag ' + tag + '">' +
        (l.level > 0 ? '+' : '') + l.level.toFixed(0) + ' SD</span></td>' +
      '<td class="r">' + l.touches + '</td>' +
      '<td>' + bar(100 * l.touches / most, l.level < 0) + '</td>' +
      '<td class="r">' + pct(l.reverted_pct) + '</td>' +
      '<td class="r">' + seconds(l.median_seconds) + '</td>' +
      '<td class="r">' + (l.median_adverse_sigma === null ? DASH
        : l.median_adverse_sigma.toFixed(1) + 'σ') + '</td>' +
      '<td class="r">' + l.traded + '</td>' +
      '<td class="r">' + (l.unresolved || DASH) + '</td>';
    // A level whose move cannot cover the round trip is dimmed: it may
    // revert beautifully and still lose money every time.
    if (l.pays === false) tr.className = 'cannot-pay';
    body.appendChild(tr);
  });

  const finding = el.querySelector('.an-touch-finding');
  const tail = study.unresolved
    ? ' ' + study.unresolved + ' touch(es) still running are counted ' +
      'separately and left out of the percentages.' : '';
  finding.classList.remove('hidden', 'warn');
  if (study.best_level && study.best_reverted_pct !== null) {
    finding.innerHTML = '<b>±' + study.best_level.toFixed(1) +
      ' is the level to trade on this contract</b> — ' +
      study.best_reverted_pct.toFixed(0) + '% of those touches reverted, and ' +
      'the move back to the mean covers the round trip. The entry threshold ' +
      'is currently ±' + (threshold || 2).toFixed(2) + '.' + tail;
  } else if (study.nothing_pays) {
    // Rather than falling back to the innermost band and calling it an answer.
    finding.classList.add('warn');
    finding.innerHTML = '<b>No level covers its own round trip on this ' +
      'contract.</b> The inner bands revert more often, as they always do, ' +
      'but the move back to the mean does not pay for the trade — this is a ' +
      'costs problem or a contract to leave alone, not a threshold to lower.' +
      tail;
  } else {
    finding.innerHTML = 'Not enough resolved touches yet to say which level ' +
      'is worth trading.' + tail;
  }
}

function renderExits(el, rows) {
  const body = el.querySelector('.an-exits tbody');
  body.innerHTML = '';
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="txt">' + r.reason.toLowerCase().replace(/_/g, ' ') +
      '</td><td class="r">' + r.count + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
      money(r.net) + '</td>';
    body.appendChild(tr);
  });
  if (!rows.length) {
    body.innerHTML = '<tr><td class="txt" colspan="3">nothing closed yet</td></tr>';
  }
}

function renderCosts(el, costs, key) {
  const body = el.querySelector('.an-costs tbody');
  body.innerHTML = '';
  costs.lines.forEach((l) => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="txt">' + l.item + '</td>' +
      '<td class="r">' + money(l.budgeted) + '</td>' +
      '<td class="r">' + money(l.actual) + '</td>' +
      '<td class="r">' + (l.diff === null ? DASH : money(l.diff)) + '</td>';
    body.appendChild(tr);
  });
  const total = document.createElement('tr');
  total.innerHTML = '<td class="txt"><b>round trip</b></td>' +
    '<td class="r">' + money(costs.total.budgeted) + '</td>' +
    '<td class="r">' + money(costs.total.actual) + '</td>' +
    '<td class="r">' + money(costs.total.diff) + '</td>';
  body.appendChild(total);

  const ticks = document.createElement('tr');
  ticks.innerHTML = '<td class="txt">slippage, per side</td>' +
    '<td class="r">' + (costs.budget_ticks === null ? DASH
      : costs.budget_ticks.toFixed(2) + ' tk') + '</td>' +
    '<td class="r">' + (costs.measured_ticks === null ? DASH
      : costs.measured_ticks.toFixed(2) + ' tk') + '</td>' +
    '<td class="r">' + (costs.unmeasured_fills
      ? costs.unmeasured_fills + ' unmeasured' : '') + '</td>';
  body.appendChild(ticks);

  const finding = el.querySelector('.an-cost-finding');
  finding.innerHTML = '';
  if (!costs.finding) { finding.classList.add('hidden'); return; }
  finding.classList.remove('hidden');
  const text = document.createElement('span');
  text.innerHTML = '<b>' + costs.finding.text + '</b>';
  finding.appendChild(text);
  const apply = document.createElement('button');
  apply.className = 'btn sm';
  apply.style.marginTop = '5px';
  apply.textContent = 'Set the budget to ' +
    costs.finding.suggest_ticks.toFixed(2);
  // It PROPOSES. Nothing in this system applies its own findings.
  apply.onclick = async () => {
    const res = await fetch('/api/contracts/' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slippage_budget_ticks: costs.finding.suggest_ticks }),
    });
    const data = await res.json();
    if (data.ok) {
      toast('OPEN', 'APPLIED', 'Slippage budget set to ' +
        costs.finding.suggest_ticks.toFixed(2) + ' ticks', key);
      loadAnalysis();
    } else {
      toast('REJECT', 'NOT APPLIED', data.error || 'refused', key);
    }
  };
  finding.appendChild(apply);
}

function renderJournal(el, rows, decimals) {
  const body = el.querySelector('.an-journal tbody');
  body.innerHTML = '';
  const when = (iso) => iso ? new Date(iso).toLocaleString([],
    { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    : DASH;
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + when(r.opened_at) + '</td><td>' + when(r.closed_at) + '</td>' +
      '<td><span class="tag ' + (r.side === 'BUY' ? 't-buy' : 't-sell') + '">' +
        (r.side === 'BUY' ? 'LONG' : 'SHORT') + '</span></td>' +
      '<td class="r">' + r.qty + '</td>' +
      '<td class="r">' + signed(r.entry_z, 2) + '</td>' +
      '<td class="r">' + signed(r.exit_z, 2) + '</td>' +
      '<td class="r">' + num(r.entry_price, decimals) + '</td>' +
      '<td class="r">' + num(r.exit_price, decimals) + '</td>' +
      '<td class="r">' + money(r.gross) + '</td>' +
      '<td class="r">' + money(r.fees) + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
        money(r.net) + '</td>' +
      '<td class="r">' + pct(r.on_margin, 2) + '</td>' +
      '<td class="r">' + minutes(r.held_min) + '</td>' +
      '<td class="txt">' + (r.exit_reason || DASH).toLowerCase().replace(/_/g, ' ') +
        (r.simulated ? ' <span class="tag t-cxl">sim</span>' : '') + '</td>';
    body.appendChild(tr);
  });
  if (!rows.length) {
    body.innerHTML = '<tr><td class="txt" colspan="14">no closed trades in ' +
      'this period</td></tr>';
  }
}

function renderDesk(el, report) {
  const body = el.querySelector('.an-desk-table tbody');
  body.innerHTML = '';
  const best = Math.max(1, ...report.rows.map((r) => Math.abs(r.net || 0)));
  report.rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="txt">' + r.name + '</td>' +
      '<td class="r">' + r.trades + '</td>' +
      '<td class="r">' + pct(r.win_rate) + '</td>' +
      '<td>' + bar(100 * Math.abs(r.net || 0) / best, (r.net || 0) < 0) + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
        money(r.net) + '</td>' +
      '<td class="r">' + money(r.avg) + '</td>' +
      '<td class="r">' + pct(r.on_margin, 2) + '</td>' +
      '<td class="r">' + pct(r.cost_drag) + '</td>' +
      '<td class="r">' + minutes(r.avg_hold_min) + '</td>' +
      '<td class="r">' + (r.best_level ? '±' + r.best_level.toFixed(1) : DASH) + '</td>' +
      '<td class="txt">' + r.verdict + '</td>';
    body.appendChild(tr);
  });
  const t = report.total;
  el.querySelector('.an-desk-total').innerHTML =
    '<td class="txt"><b>All, blended</b></td><td class="r">' + t.trades +
    '</td><td class="r">' + pct(t.win_rate) + '</td><td></td>' +
    '<td class="r">' + money(t.net) + '</td><td class="r">' + money(t.avg) +
    '</td><td class="r">' + pct(t.on_margin, 2) + '</td><td class="r">' +
    pct(t.cost_drag) + '</td><td class="r">' + minutes(t.avg_hold_min) +
    '</td><td></td><td></td>';
  el.querySelector('.an-desk-note').innerHTML =
    'A contract with fewer than <b>' + report.min_trades_for_a_verdict +
    '</b> closed trades is marked <b>too few to judge</b> and is never given ' +
    'a verdict on a win rate. No figure here is blended across contracts ' +
    'except the last row, which says so.';
}

function renderAnalysisTabs(el, contracts) {
  const host = el.querySelector('.an-tabs');
  host.innerHTML = '';
  const add = (key, label) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = (analysis.key === key) ? 'on' : '';
    b.onclick = () => { analysis.key = key; loadAnalysis(); };
    host.appendChild(b);
  };
  contracts.forEach((c) => add(c.key, c.name));
  add(null, 'All contracts');
}

/* -- what a different threshold would have done ---------------------------
 *
 * The touch table says which level REVERTS most — the inner bands always do.
 * This is the question that follows, and it has a different answer: what did
 * each level MAKE, after this contract's own round trip.
 *
 * Everything the replay does not know is printed under the table. A backtest
 * whose assumptions are not on the page is a backtest somebody will quote
 * without them.
 */
function renderReplay(el, report, live) {
  const body = el.querySelector('.an-replay tbody');
  const finding = el.querySelector('.an-replay-finding');
  const note = el.querySelector('.an-replay-assumptions');
  body.innerHTML = '';
  // Nothing to run, or nothing long enough to run: that is about the
  // RECORDING, and a table of dashes would read as "nothing paid".
  if (!report || !report.rows || !report.rows.length || report.blocked_by) {
    finding.className = 'finding warn an-replay-finding';
    finding.textContent = 'Nothing to replay — ' +
      ((report && report.blocked_by) ||
       'no recorded prices for this contract over this period') +
      '. The engine records a sample per pass while it runs.';
    note.textContent = '';
    return;
  }
  const best = report.best;
  report.rows.forEach((r) => {
    const tr = document.createElement('tr');
    const isLive = live !== null && live !== undefined &&
      Math.abs(r.entry_threshold - live) < 1e-9;
    // A row nobody can judge is shown — the contrast is the point — and
    // dimmed, because it is not a threshold to aim at.
    if (!r.enough_to_judge) tr.classList.add('cannot-pay');
    if (best && r.entry_threshold === best.entry_threshold) {
      tr.classList.add('pays');
    }
    tr.innerHTML =
      '<td><span class="tag">±' + num(r.entry_threshold, 2) + '</span>' +
      (isLive ? ' <small>live</small>' : '') + '</td>' +
      '<td class="r">' + (r.trades || 0) + '</td>' +
      '<td class="r">' + (r.win_rate === null ? DASH : num(r.win_rate, 1) + '%') + '</td>' +
      '<td class="r ' + cls(r.net) + '">' + money(r.net) + '</td>' +
      '<td class="r ' + cls(r.per_trade) + '">' + money(r.per_trade) + '</td>' +
      '<td class="r ' + cls(r.worst) + '">' + money(r.worst) + '</td>' +
      '<td class="r">' + (r.median_hold_sec === null ? DASH
        : seconds(r.median_hold_sec)) + '</td>' +
      '<td>' + (r.enough_to_judge ? 'yes'
        : '<span title="fewer than ten closed trades">too few</span>') + '</td>';
    body.appendChild(tr);
  });

  if (best) {
    finding.className = 'finding an-replay-finding';
    finding.innerHTML = '<b>±' + num(best.entry_threshold, 2) +
      ' is the threshold that PAID on this recording</b> — ' + best.trades +
      ' closed trades for ' + money(best.net) + ' after costs (' +
      money(best.per_trade) + ' each). A level that reverts more often is ' +
      'not the same as a level that pays: the inner bands come back more ' +
      'reliably and their move cannot cover the round trip.';
  } else {
    finding.className = 'finding warn an-replay-finding';
    // If every row was withheld, the reason is the signal's own words, not
    // a guess about the threshold — that is what sends a desk to change the
    // number that was never the problem.
    const withheld = report.rows.map((r) => r.blocked_by).filter(Boolean);
    finding.textContent = withheld.length === report.rows.length
      ? 'Nothing was entered at any threshold — ' + withheld[0] +
        '. The threshold is not what is stopping this.'
      : 'No threshold cleared its own costs on this recording with enough ' +
        'trades to judge. That is a finding, not a missing number — ' +
        'lowering the threshold onto something that reverts beautifully is ' +
        'exactly how it gets worse.';
  }

  const a = report.assumptions || {};
  note.innerHTML = 'This is a <b>signal</b> replay, not a fill simulator. ' +
    'The book was ' + (a.book || 'assumed') + '; ' + (a.fills || '') + '; ' +
    (a.costs || '') + '. ' +
    (a.margin ? '<b>Margin:</b> ' + a.margin + '. ' : '') +
    'Read against ' + (report.samples || 0) +
    ' recorded prices.';
}

function cls(v) {
  if (v === null || v === undefined) return '';
  return v > 0 ? 'up' : v < 0 ? 'dn' : '';
}

async function loadAnalysis() {
  if (state.closed.has('__analysis__')) return;
  const mine = ++analysis.seq;
  const current = () => mine === analysis.seq;
  const el = analysisWindow();
  const contracts = (window.__lastSnapshot || {}).contracts || [];
  if (analysis.key && !contracts.some((c) => c.key === analysis.key)) {
    analysis.key = contracts.length ? contracts[0].key : null;
  }
  if (analysis.key === undefined) analysis.key = null;
  if (analysis.key === null && contracts.length && analysis.key !== null) { /* noop */ }
  if (analysis.key === null && !el.dataset.touched) {
    analysis.key = contracts.length ? contracts[0].key : null;
    el.dataset.touched = '1';
  }
  // Simulated fills are never BLENDED into a live figure without being
  // asked for — but a desk running the simulator opening on "Live only" is
  // shown an empty window for the session it just watched. So the default
  // follows what the engine actually is, until somebody chooses otherwise.
  if (!analysis.modeChosen) {
    const sim = (window.__lastSnapshot || {}).engine
      && window.__lastSnapshot.engine.simulated;
    analysis.mode = sim ? 'sim' : 'live';
  }
  renderAnalysisTabs(el, contracts);
  el.querySelector('.an-period').value = analysis.period;
  el.querySelector('.an-mode').value = analysis.mode;

  const query = '?period=' + analysis.period + '&mode=' + analysis.mode;
  const say = (text) => {
    // Never silently render nothing: a window that has failed to load must
    // look different from one with nothing to report.
    el.querySelector('.an-foot').textContent = text;
    el.querySelector('.an-note').textContent = text;
  };
  const desk = el.querySelector('.an-desk');
  const perContract = el.querySelectorAll('.an-tiles, .an-two, .an-journal');

  if (analysis.key === null) {
    const report = await getAnalysis('/api/analysis' + query);
    if (!current()) return;                 // superseded while in flight
    if (!report) { say('the analysis could not be read'); return; }
    desk.classList.remove('hidden');
    el.querySelectorAll('.an-tiles, .an-two').forEach((n) => n.classList.add('hidden'));
    el.querySelector('.an-journal').closest('.card').classList.add('hidden');
    renderDesk(el, report);
    el.querySelector('.an-note').textContent = 'all contracts · closed trades only';
    el.querySelector('.an-foot').textContent =
      report.rows.length + ' contracts · ' + report.total.trades + ' closed trades';
    el.querySelector('.an-csv').classList.add('hidden');
  } else {
    const report = await getAnalysis('/api/analysis/' +
      encodeURIComponent(analysis.key) + query);
    if (!current()) return;                 // superseded while in flight
    if (!report) {
      say('no analysis for ' + analysis.key +
          ' — it is on the screen but not in the configuration');
      return;
    }
    desk.classList.add('hidden');
    el.querySelectorAll('.an-tiles, .an-two').forEach((n) => n.classList.remove('hidden'));
    el.querySelector('.an-journal').closest('.card').classList.remove('hidden');
    renderTiles(el, report.summary);
    renderTouches(el, report.touches, report.entry_threshold);
    renderExits(el, report.exits);
    renderCosts(el, report.costs, report.key);
    renderJournal(el, report.journal, report.decimals);
    // Much slower than the rest of the window — it re-runs the whole
    // recorded series at every threshold — so it is fetched separately, and
    // only when the question it answers has actually changed. Re-running it
    // on the fifteen-second refresh would spend most of the desk's CPU
    // recomputing an answer nobody asked again.
    const asked = analysis.key + '|' + analysis.period;
    if (asked !== analysis.replayFor) {
      analysis.replayFor = asked;
      getAnalysis('/api/replay/' + encodeURIComponent(analysis.key) +
        '?period=' + analysis.period).then((r) => {
          analysis.replayCache = r;
          if (current()) renderReplay(el, r, report.entry_threshold);
        });
    } else if (analysis.replayCache) {
      renderReplay(el, analysis.replayCache, report.entry_threshold);
    }
    el.querySelector('.an-note').textContent = report.symbol +
      ' · closed trades only';
    el.querySelector('.an-journal-note').textContent =
      report.journal.length + ' closed, newest first';
    el.querySelector('.an-foot').textContent =
      report.summary.trades + ' closed · ' + report.open_positions +
      ' open (excluded) · ' + report.touches.unresolved + ' touches unresolved';
    // Zero under one filter and non-zero under another is a statement about
    // the filter, not about the desk. Say which.
    if (!report.summary.trades) elsewhereNote(el, query);
    const csv = el.querySelector('.an-csv');
    csv.classList.remove('hidden');
    csv.href = '/api/analysis/' + encodeURIComponent(analysis.key) +
      '/trades.csv' + query;
  }
  const slipQuery = query + (analysis.key ? '&contract=' +
    encodeURIComponent(analysis.key) : '');
  const slip = await getAnalysis('/api/slippage' + slipQuery);
  if (!current()) return;
  renderSlippage(el, slip, analysis.key);
  el.querySelector('.an-slip-csv').href = '/api/slippage.csv' + slipQuery;
  el.querySelector('.an-updated').textContent =
    'updated ' + new Date().toLocaleTimeString([], { hour12: false });
}

/* -- the slippage report ----------------------------------------------------
 *
 * Measured against the price each decision was made at, positive a cost.
 * Cost is red, an improvement green, unmeasured a dash in its own column —
 * never a zero in the mean.
 */
function slipCell(ticks) {
  if (ticks === null || ticks === undefined) return '<td class="r">' + DASH + '</td>';
  return '<td class="r ' + (ticks > 0 ? 'dn' : ticks < 0 ? 'up' : '') + '">' +
    (ticks > 0 ? '+' : '') + Number(ticks).toFixed(2) + ' t</td>';
}

function slipCash(value) {
  if (value === null || value === undefined) return '<td class="r">' + DASH + '</td>';
  return '<td class="r ' + (value > 0 ? 'dn' : value < 0 ? 'up' : '') + '">' +
    cash(value) + '</td>';
}

function slipRow(label, st, cls) {
  if (!st) return '';
  return '<tr' + (cls ? ' class="' + cls + '"' : '') + '><td>' + label +
    '</td><td class="r">' + st.measured + '</td><td>' +
    (st.unmeasured ? '<span class="hint">' + st.unmeasured + ' unmeasured</span>'
      : '') + '</td>' + slipCell(st.ticks_mean) + slipCell(st.ticks_median) +
    slipCell(st.ticks_worst) + slipCell(st.ticks_best) +
    slipCash(st.money_total) + '<td>' + (st.measured ? st.paid + ' paid / ' +
      st.earned + ' earned' : DASH) + '</td></tr>';
}

function renderSlippage(el, report, key) {
  const body = el.querySelector('.an-slip tbody');
  const note = el.querySelector('.an-slip-note');
  if (!report || report.ok === false) {
    body.innerHTML = '';
    note.textContent = 'the slippage report could not be read';
    return;
  }
  const counts = report.counts || {};
  if (!counts.positions && !(report.manual && report.manual.counts &&
      report.manual.counts.tickets)) {
    body.innerHTML = '';
    note.textContent = 'Nothing was traded under these filters. That is not ' +
      'a slippage of zero — there is nothing to measure yet.';
    return;
  }
  let html = slipRow('Entries', report.overall.entry) +
    slipRow('Exits', report.overall.exit) +
    slipRow('Round turn', report.overall.round_trip, 'an-slip-total');
  // MARKET against LIMIT on the same desk: a limit that is not beating a
  // market fill is costing time for nothing, and only side by side says so.
  const types = Object.keys(report.by_order_type || {});
  if (types.length) {
    html += '<tr class="an-slip-head"><td colspan="9">Entries by order type</td></tr>';
    types.forEach((t) => { html += slipRow(esc(t), report.by_order_type[t].entry); });
  }
  if (!key) {
    html += '<tr class="an-slip-head"><td colspan="9">Round turn by contract ' +
      '<span class="hint">(budget, ticks a side)</span></td></tr>';
    Object.keys(report.by_contract || {}).forEach((k) => {
      const c = report.by_contract[k];
      html += slipRow(esc(c.name || k) + ' <span class="hint">budget ' +
        (c.budget_ticks || 0) + '</span>', c.round_trip);
    });
  }
  const worst = report.worst || [];
  if (worst.length) {
    html += '<tr class="an-slip-head"><td colspan="9">Worst entries</td></tr>';
    worst.forEach((r) => {
      html += '<tr><td>' + esc(r.name || r.contract_key) + ' · ' + esc(r.side) +
        ' ' + r.qty + '</td><td class="r">' + esc(r.entry_order_type || '') +
        '</td><td class="hint">' + (r.opened_at ? new Date(r.opened_at)
          .toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit',
            minute: '2-digit' }) : '') + '</td>' + slipCell(r.entry_ticks) +
        '<td></td><td></td><td></td>' + slipCash(r.entry_money) + '<td></td></tr>';
    });
  }
  const manual = report.manual;
  if (manual && manual.counts && manual.counts.tickets) {
    html += '<tr class="an-slip-head"><td colspan="9">Manual tickets ' +
      '<span class="hint">(average fill against the touch when the ticket ' +
      'was sent)</span></td></tr>' +
      slipRow('Manual entries', manual.entry) +
      slipRow('Manual closes', manual.exit);
  }
  body.innerHTML = html;
  note.textContent = counts.positions + ' position(s) · ' + counts.open +
    ' still open (no exit yet — not an unmeasured one)' +
    (counts.paper ? ' · ' + counts.paper + ' PAPER, filled at the decision ' +
      'price by construction — nothing to measure, so not counted' : '') +
    '. A limit escalated to market is measured from the price the limit ' +
    'was decided at: the wait is part of what it cost.';
}

/* Nothing here, but something under another filter? Say so. An empty
 * Analysis window that reads as "you have not traded" when in fact the rows
 * are one dropdown away is the window quietly lying. */
async function elsewhereNote(el, query) {
  const modes = { live: 'Live only', sim: 'Simulated only', both: 'Live + simulated' };
  const mine = analysis.seq;
  const others = Object.keys(modes).filter((m) => m !== analysis.mode);
  for (const other of others) {
    if (mine !== analysis.seq) return;      // the filter moved on
    const alt = await getAnalysis('/api/analysis/' +
      encodeURIComponent(analysis.key) +
      query.replace('mode=' + analysis.mode, 'mode=' + other));
    if (alt && alt.summary && alt.summary.trades) {
      el.querySelector('.an-foot').textContent =
        'nothing under ' + modes[analysis.mode] + ' — ' + alt.summary.trades +
        ' closed trade(s) are under ' + modes[other];
      return;
    }
  }
}

async function getAnalysis(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return res.json();
  } catch (e) {
    return null;
  }
}

/* -- chrome --------------------------------------------------------------- */

function renderTabs() {
  const host = document.getElementById('tabs');
  host.innerHTML = '';
  document.querySelectorAll('.win').forEach((el) => {
    const b = document.createElement('button');
    b.className = 'tk' + (state.focused === el.dataset.key ? ' act' : '');
    b.textContent = el.querySelector('.title').textContent;
    if (el.dataset.key === '__positions__') b.classList.add('wide-tab');
    if (el.classList.contains('minimised')) b.classList.add('minimised');
    // Like any taskbar: a minimised window comes back; the window in front
    // goes down; any other comes to the front.
    b.onclick = () => {
      if (el.classList.contains('minimised')) { setMinimised(el, false); return; }
      if (state.focused === el.dataset.key && el.classList.contains('raised')) {
        setMinimised(el, true);
        return;
      }
      state.focused = el.dataset.key;
      raise(el);
      el.scrollIntoView({ block: 'nearest' });
      renderTabs();
    };
    host.appendChild(b);
  });
}

function renderChrome(snap) {
  const engine = snap.engine || {};
  const badge = document.getElementById('env-badge');
  // UAT and PROD are separate venues and the screen always says which — and
  // a configured venue that nothing is connected to is NOT that venue. A
  // badge reading a plain "UAT" while the simulator drives is a screen
  // describing a session that does not exist.
  badge.textContent = engine.simulated
    ? (engine.environment && engine.environment !== 'SIMULATED'
      ? engine.environment + ' · SIM' : 'SIMULATED')
    : (engine.environment || DASH);
  badge.title = engine.simulated
    ? 'these prices come from the simulator, not from a venue'
    : 'live venue session';
  badge.className = 'env' + (engine.environment === 'PROD' ? ' prod'
    : engine.simulated ? ' sim' : '');

  const banner = document.getElementById('engine-banner');
  if (engine.alive === false) {
    banner.classList.remove('hidden');
    banner.classList.add('critical');
    banner.textContent = engine.text ||
      'the engine is not publishing — these prices are not live';
  } else if (engine.killed) {
    banner.classList.remove('hidden');
    banner.classList.remove('critical');
    banner.textContent = 'KILL ALL is on — every algo is stood down and ' +
      'our working orders are cancelled. Positions are untouched.';
  } else if (engine.connection_only) {
    banner.classList.remove('hidden', 'critical');
    banner.textContent = 'Live FIX market data and algo proposals are available. Automatic orders require account recovery and FIX execution integration. Reviewed manual UAT orders remain under Instruments & orders.';
  } else if (engine.execution && engine.execution.mode === 'LIVE' &&
             engine.execution.positions_waived) {
    banner.classList.remove('hidden', 'critical');
    banner.textContent = sendWord() + ' — the Algo sends real orders. Stellar positions ' +
      'could not be read on this session: this book\'s own fills are the ' +
      'record, on your confirmation. A position opened in Stellar by other ' +
      'means on this account is invisible to the Algo.';
  } else if (engine.book_complete === false) {
    // Not an error in trading: Stellar has not told us the account's positions,
    // so this book cannot be checked against Stellar's. Said with the reason and
    // with what still works — "nothing will be closed" read as "my closes
    // will not work", which is not what it meant.
    const why = ((engine.execution || {}).positions || {}).why;
    banner.classList.remove('hidden', 'critical');
    banner.classList.add('warn');
    banner.textContent = 'Stellar has not sent this account\'s positions' +
      (why ? ' (' + why + ')' : '') + ', so this program cannot check its own ' +
      'book against Stellar\'s. Your orders and closes work normally — CLOSE ALL, ' +
      'Close @ LMT, the Algo\'s take-profit and stop loss. Only the Reconciler ' +
      'waits: it closes nothing on its own until Stellar\'s positions are known.';
  } else {
    banner.classList.add('hidden');
  }

  const unclaimed = document.getElementById('unclaimed-banner');
  const items = engine.unclaimed || [];
  unclaimed.classList.toggle('hidden', items.length === 0);
  if (items.length) {
    unclaimed.textContent = 'UNCLAIMED — ' +
      items.map((u) => u.text).join('  ');
  }

  // A setting that looks saved and is not in force is worse than one that
  // plainly says it needs the engine bounced. The engine names them.
  const restart = document.getElementById('restart-banner');
  const waiting = engine.config_restart_needed || [];
  restart.classList.toggle('hidden', waiting.length === 0);
  if (waiting.length) {
    const said = 'SAVED, NOT IN FORCE — ' + waiting.join(', ') +
      '. These change something the running engine already holds; restart it ' +
      'for them to take effect. Everything else you saved is live now.';
    // Rebuilt only when the list changes, or the button would be replaced
    // under the pointer twice a second.
    if (restart.dataset.said !== said) {
      restart.dataset.said = said;
      restart.textContent = said + ' ';
      if (engine.supervised) {
        const b = document.createElement('button');
        b.className = 'btn sm';
        b.textContent = 'Restart engine now';
        b.onclick = restartEngine;
        restart.appendChild(b);
      }
    }
  } else {
    restart.dataset.said = '';
  }

  const link = document.getElementById('link-badge');
  const session = engine.session || {};
  link.textContent = session.state === 'LOGGED_ON'
    ? (engine.simulated ? 'SIMULATED' : 'CONNECTED')
    : (session.text || 'no session');
  link.className = 'link ' + (session.state === 'LOGGED_ON'
    ? (engine.simulated ? 'part' : 'ok') : 'bad');

  const master = document.getElementById('master-toggle');
  master.textContent = 'Master: ' + (engine.master_algo ? 'ON' : 'OFF');
  master.classList.toggle('act', !!engine.master_algo);
  // PAPER or LIVE: where the Algo's orders go. LIVE is armed by a person,
  // confirmed every time, and is off after every restart.
  const ex = engine.execution || {};
  const exBtn = document.getElementById('execution-toggle');
  exBtn.classList.toggle('hidden', !ex.can_live);
  exBtn.textContent = 'Orders: ' + (ex.mode === 'LIVE' ? (ex.send_word || 'LIVE')
    : ex.mode === 'PAPER' ? 'PAPER' : (ex.mode || DASH));
  exBtn.classList.toggle('live', ex.mode === 'LIVE');
  const pos = ex.positions || {};
  exBtn.title = ex.mode === 'LIVE'
    ? 'The Algo SENDS its orders to Stellar ' + (ex.send_word || 'LIVE') + '. Click for PAPER.' +
      (ex.positions_waived ? ' Stellar positions were NOT read — this book\'s own ' +
        'fills are the record, on your confirmation.' : '')
    : 'PAPER: the Algo fills here at the live price; nothing is sent. Click ' +
      'to send to Stellar ' + (ex.send_word || 'LIVE') + '. Stellar positions: ' + (pos.why || pos.status || 'unknown') + '.';

  const stat = document.getElementById('loop-stat');
  stat.textContent = 'loop ' + (engine.loop_ms === undefined ? DASH
    : engine.loop_ms + 'ms') +
    (engine.snapshot_age_sec !== null && engine.snapshot_age_sec !== undefined
      ? ' · ' + engine.snapshot_age_sec + 's' : '');

  if (engine.notify) state.notify = engine.notify;
  if (engine.refresh_sec) {
    const ms = Math.round(engine.refresh_sec * 1000);
    if (ms !== state.refresh) { state.refresh = ms; restartTimer(); }
  }
}

/* -- the loop ------------------------------------------------------------- */

/* -- one contract's settings, behind the gear -----------------------------
 *
 * The full set of §4.1, because the comprehensiveness is the point: a desk
 * that cannot set a contract's own costs ends up trading eight instruments
 * on one instrument's assumptions.
 *
 * Two rules the whole panel turns on:
 *
 *   - **A blank box means "use the desk default"**, and the effective value
 *     is rendered beside it in grey so a blank is never mistaken for a zero.
 *     `0` is a real number and sets an override.
 *   - **A change that cannot be adopted while running SAYS SO.** The engine
 *     reports what it could not apply; a setting that looks saved and is not
 *     in force is worse than one that plainly needs a restart.
 */

const CFG_GROUPS = [
  { name: 'Signal', fields: [
    ['timeframe_min', 'Candles of', 'minutes; each closes on the last MID in it, the one forming now counts', 'select', { options: [['1', '1 min'], ['5', '5 min'], ['15', '15 min'], ['30', '30 min'], ['60', '1 hour'], ['240', '4 hours']] }],
    ['length', 'Band length N', 'the middle is EMA(N) of the closes, σ the population σ of the last N', 'number', { step: 1, min: 2 }],
    ['entry_threshold', 'Entry z', 'H to L when the BID z reaches +this; L to H when the OFFER z reaches &minus;this', 'number', { step: 0.1, min: 0 }],
    ['max_entry_z', 'No entry beyond |z|', 'a blow-out, not a stretch; 0 = no cap', 'number', { step: 0.1, min: 0 }],
    ['confirm_samples', 'Confirm for', 'fresh quotes in a row', 'number', { step: 1, min: 1 }],
    ['trade_direction', 'Enter', 'entries only &mdash; an exit is never restricted', 'select', { options: [['BOTH', 'Both ways'], ['SELL_ONLY', 'H to L only (sell)'], ['BUY_ONLY', 'L to H only (buy)']] }],
    ['reentry_on', 'Re-entry', 'ARMED at the band, entered on the way back in &mdash; a trend rides the band and never gives one', 'check'],
    ['reentry_back', 'Enter back by', 'z back inside the band', 'number', { step: 0.1, min: 0.05 }],
    ['reentry_window_pct', 'Re-entry window', '% of the way back to the mean that still counts; past it the side disarms', 'number', { step: 5, min: 5 }],
    ['entry_cooldown_seconds', 'Cooldown after an exit', 'seconds before the next entry', 'number', { step: 30, min: 0 }],
    ['warmup_min', 'Warm-up', 'minutes of LIVE prices watched since the Algo was armed; 0 = off', 'number', { step: 10, min: 0 }],
    ['cutoff_buffer_min', 'No entry before the close', 'minutes before the session close; 0 = off', 'number', { step: 5, min: 0 }],
  ] },
  { name: 'Filters', fields: [
    ['edge_on', 'Edge', 'expected capture must be a multiple of the round trip', 'check'],
    ['edge_multiple', 'Edge required', '&times; the round-trip cost', 'number', { step: 0.1, min: 0 }],
    ['edge_capture_frac', 'Capture expected', 'of the full move back to the mean, 0&ndash;1', 'number', { step: 0.05, min: 0 }],
    ['regime_on', 'Regime', 'no entry while the contract is TRENDING', 'check'],
    ['regime_er_max', 'Trending above efficiency', 'net move over path, 0&ndash;1', 'number', { step: 0.05, min: 0 }],
    ['regime_min_crossings', '&hellip; and mean crossings at most', 'over the last 2N candles', 'number', { step: 1, min: 0 }],
    ['trend_on', 'Trend', 'no entry AGAINST a middle that moved this far', 'check'],
    ['trend_sigma', 'Trend limit', 'σ of drift in the EMA', 'number', { step: 0.1, min: 0 }],
    ['trend_lookback_min', 'over the last', 'minutes', 'number', { step: 15, min: 1 }],
    ['half_life_min_min', 'Half-life at least', 'minutes; 0 = off', 'number', { step: 5, min: 0 }],
    ['half_life_max_min', 'Half-life at most', 'minutes; 0 = off', 'number', { step: 30, min: 0 }],
  ] },
  { name: 'Exit', fields: [
    ['margin_per_contract', 'Margin per contract', 'money; Stellar does not report it. Target and stop are % of this &mdash; no margin, no entries', 'number', { step: 50, min: 0 }],
    ['target_mode', 'Target sized by', '', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['profit_target_pct', 'Profit target', '% of the margin, from break-even (after the whole round trip)', 'number', { step: 0.5, min: 0 }],
    ['stop_loss_on', 'Stop loss', 'from break-even, the mirror of the target', 'check'],
    ['stop_mode', 'Stop sized by', '', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['stop_loss_pct', 'Stop loss at', '% of the margin, below break-even', 'number', { step: 0.5, min: 0 }],
    ['atr_period', 'ATR period', 'candles, Wilder, close to close', 'number', { step: 1, min: 2 }],
    ['atr_target_mult', 'Target', '&times; ATR (in ATR mode), frozen at entry', 'number', { step: 0.1, min: 0 }],
    ['atr_stop_mult', 'Stop', '&times; ATR (in ATR mode), frozen at entry', 'number', { step: 0.1, min: 0 }],
    ['stop_z_on', 'Z stop', 'on the side it would close on', 'check'],
    ['stop_loss_z', 'Z stop at |z|', '', 'number', { step: 0.5, min: 0 }],
    ['exit_at_mean', 'Exit at the mean when paid', 'back at the mean and net positive, short of the target', 'check'],
    ['max_hold_minutes', 'Time stop', 'minutes, whatever the P&amp;L; 0 = none', 'number', { step: 15, min: 0 }],
    ['progress_bar', 'SL &larr; entry &rarr; TP bar', 'in the Algo window while a position is on', 'check'],
  ] },
  { name: 'Exit by side', fields: [
    ['target_mode_hl', 'H to L: target sized by', 'blank = same as both', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['profit_target_pct_hl', 'H to L: profit target', '% of the margin, from break-even; blank = same as both', 'number', { step: 0.5, min: 0 }],
    ['atr_target_mult_hl', 'H to L: target', '&times; ATR (ATR mode); blank = same as both', 'number', { step: 0.1, min: 0 }],
    ['stop_mode_hl', 'H to L: stop sized by', 'blank = same as both', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['stop_loss_pct_hl', 'H to L: stop loss at', '% of the margin, below break-even; blank = same as both', 'number', { step: 0.5, min: 0 }],
    ['atr_stop_mult_hl', 'H to L: stop', '&times; ATR (ATR mode); blank = same as both', 'number', { step: 0.1, min: 0 }],
    ['target_mode_lh', 'L to H: target sized by', 'blank = same as both', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['profit_target_pct_lh', 'L to H: profit target', '% of the margin, from break-even; blank = same as both', 'number', { step: 0.5, min: 0 }],
    ['atr_target_mult_lh', 'L to H: target', '&times; ATR (ATR mode); blank = same as both', 'number', { step: 0.1, min: 0 }],
    ['stop_mode_lh', 'L to H: stop sized by', 'blank = same as both', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['stop_loss_pct_lh', 'L to H: stop loss at', '% of the margin, below break-even; blank = same as both', 'number', { step: 0.5, min: 0 }],
    ['atr_stop_mult_lh', 'L to H: stop', '&times; ATR (ATR mode); blank = same as both', 'number', { step: 0.1, min: 0 }],
  ] },
  { name: 'Book', fields: [
    ['min_book_size', 'Book size at least', 'contracts on the touch, both sides', 'number', { step: 1, min: 0 }],
    ['max_book_spread_ticks', 'Book no wider than', 'ticks', 'number', { step: 1, min: 0 }],
  ] },
  { name: 'Size & risk', fields: [
    ['quantity', 'Algo quantity', 'contracts per entry', 'number', { step: 1, min: 0 }],
    ['max_position', 'Maximum position', 'contracts; the hard ceiling', 'number', { step: 1, min: 0 }],
    ['max_trades_per_day', 'Trades per day', 'Algo entries; 0 = no limit', 'number', { step: 1, min: 0 }],
    ['max_losses_row', 'Losing trades in a row', 'pauses entries for the day; 0 = off', 'number', { step: 1, min: 0 }],
    ['daily_max_loss', 'Daily loss limit', 'money, closed plus open; stops entries for the day', 'number', { step: 50, min: 0 }],
  ] },
  { name: 'Execution', fields: [
    ['entry_order_type', 'Entry', '', 'select', { options: [['LIMIT', 'Limit'], ['MARKET', 'Market']] }],
    ['exit_order_type', 'Exit', '', 'select', { options: [['MARKET', 'Market'], ['LIMIT', 'Limit']] }],
    ['entry_limit_offset_ticks', 'Entry priced behind its touch', 'ticks', 'number', { step: 1, min: 0 }],
    ['exit_limit_offset_ticks', 'Exit priced behind its touch', 'ticks &mdash; patience going in and coming out are different decisions', 'number', { step: 1, min: 0 }],
    ['market_limit_ticks', 'Market orders as a limit', 'ticks through the best price, fill-or-cancel &mdash; an exchange rejects a bare market order outside its price band; 0 = true market', 'number', { step: 1, min: 0 }],
    ['entry_limit_timeout_sec', 'Entry unfilled after', 'seconds', 'number', { step: 1, min: 0 }],
    ['exit_limit_timeout_sec', 'Exit unfilled after', 'seconds', 'number', { step: 1, min: 0 }],
    ['entry_on_timeout', 'then the entry', '', 'select', { options: [['CANCEL', 'Cancel'], ['CROSS_AT_MARKET', 'Cross at market']] }],
    ['exit_on_timeout', 'then the exit', 'a missed entry is a trade not taken; a missed exit is a position you still hold', 'select', { options: [['CROSS_AT_MARKET', 'Cross at market'], ['CANCEL', 'Cancel']] }],
    ['repeg_dead_band_ticks', 'Re-peg dead band', 'ticks the touch must move before an amend &mdash; every amend costs queue position', 'number', { step: 1, min: 0 }],
    ['time_in_force', 'Time in force', '', 'select', { options: [['DAY', 'Day'], ['IOC', 'IOC'], ['GTC', 'GTC']] }],
    ['close_offset_mode', 'Closing flag', 'what this venue wants on a close. AUTO reads the venue; an unknown flag degrades to CLOSE, never to OPEN', 'select', { options: [['AUTO', 'Auto'], ['CLOSE', 'Close'], ['CLOSE_TODAY_FIRST', 'Close today first'], ['NONE', 'None (netting venue)']] }],
  ] },
  { name: 'Costs', fields: [
    ['commission_per_contract', 'Commission', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['exchange_fee_per_contract', 'Exchange fee', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['clearing_fee_per_contract', 'Clearing fee', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['slippage_budget_ticks', 'Slippage budget', 'ticks per side &mdash; a BUDGET. Analysis shows the measured figure beside it', 'number', { step: 0.1, min: 0 }],
  ] },
  { name: 'Display', fields: [
    ['name', 'Window title', '', 'text', { plain: true }],
    ['decimals', 'Decimals', 'how prices are printed on this window', 'number', { step: 1, min: 0, plain: true }],
    ['enabled', 'Trade this contract', 'switching it off needs a restart: its window, book and any position have nowhere to go mid-flight', 'check', { plain: true }],
  ] },
];

/* Which fields are NOT per-contract overrides but plain contract fields. A
 * blank one of these is not "use the desk default" — there is no desk
 * default for a window title. */
const CFG_PLAIN = new Set(['name', 'decimals', 'enabled']);

const cfgState = {};    // key -> { group, contract }

function configWindow(key) {
  let el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (el) { raise(el); return el; }
  const tpl = document.getElementById('config-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = '__config__' + key;
  el.dataset.contract = key;
  cfgState[key] = cfgState[key] || { group: CFG_GROUPS[0].name, contract: null };

  el.querySelector('.close').onclick = () => { el.remove(); renderTabs(); };
  el.querySelector('.cfg-revert').onclick = () => loadConfig(key);
  el.querySelector('.cfg-save').onclick = () => saveConfig(key);

  const tabs = el.querySelector('.cfg-tabs');
  CFG_GROUPS.forEach((g) => {
    const b = document.createElement('button');
    b.textContent = g.name;
    b.className = g.name === cfgState[key].group ? 'on' : '';
    b.onclick = () => {
      cfgState[key].group = g.name;
      tabs.querySelectorAll('button').forEach((x) =>
        x.classList.toggle('on', x.textContent === g.name));
      paintConfig(key);
    };
    tabs.appendChild(b);
  });

  el.onmousedown = () => raise(el);
  makeDraggable(el, '__config__' + key);
  const desk = document.getElementById('desktop');
  desk.appendChild(el);

  // A settings panel is a panel over the desk, not another tile in it:
  // opening one must not re-flow the windows the trader is reading. It
  // floats beside its own contract, out of the grid, and stays where it is
  // dragged like everything else.
  const place = state.places['__config__' + key];
  if (place) {
    placeWindow(el, place.x, place.y);
  } else {
    el.classList.add('floating');
    const owner = document.querySelector('.win[data-key="' + key + '"]');
    const box = desk.getBoundingClientRect();
    const from = owner ? owner.getBoundingClientRect() : box;
    const x = Math.max(8, Math.min(from.left - box.left + 24,
                                   desk.clientWidth - 470));
    el.style.left = x + 'px';
    el.style.top = (from.top - box.top + desk.scrollTop + 18) + 'px';
  }
  raise(el);
  loadConfig(key);
  return el;
}

async function loadConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (!el) return;
  try {
    const rows = await (await fetch('/api/contracts', { cache: 'no-store' })).json();
    const row = (rows || []).find((r) => r.key === key);
    if (!row) {
      el.querySelector('.cfg-note').textContent =
        'this contract is not in the configuration file';
      return;
    }
    cfgState[key].contract = row;
    paintConfig(key);
    el.querySelector('.cfg-note').textContent = '';
  } catch (e) {
    el.querySelector('.cfg-note').textContent = 'could not read the settings';
  }
}

function cfgField(field, label, hint, kind, opts, row) {
  const o = opts || {};
  const plain = CFG_PLAIN.has(field);
  // The saved OVERRIDE — blank means "use the desk default". Never the
  // effective value: filling the box with the default would turn every
  // fallback into an override the moment somebody pressed Save.
  const own = plain ? row[field] : (row[field] === undefined ? null : row[field]);
  const eff = row.effective ? row.effective[field] : undefined;
  const id = 'cf-' + field;
  let control;
  if (kind === 'check') {
    // The box shows what is IN FORCE: its own setting, or the desk's. A box
    // that read unticked for a desk default of ON would, on Save, write OFF —
    // a stop loss switched off by pressing Save on another field.
    const on = (own === null || own === undefined) ? eff === true : own === true;
    control = '<input type="checkbox" class="chk" id="' + id +
      '" data-field="' + field + '" data-was="' + (on ? '1' : '0') + '"' +
      (own === null || own === undefined ? ' data-default="1"' : '') +
      (on ? ' checked' : '') + '>';
  } else if (kind === 'select') {
    control = '<select id="' + id + '" data-field="' + field + '">' +
      (plain ? '' : '<option value="">&mdash; ' + (/_(hl|lh)$/.test(field)
        ? 'same as both' : 'desk default') + ' &mdash;</option>') +
      (o.options || []).map((op) =>
        '<option value="' + op[0] + '"' +
        (String(own) === op[0] ? ' selected' : '') + '>' + op[1] + '</option>').join('') +
      '</select>';
  } else {
    control = '<input type="' + (kind === 'text' ? 'text' : 'number') + '" id="' + id +
      '" data-field="' + field + '"' +
      (o.step !== undefined ? ' step="' + o.step + '"' : '') +
      (o.min !== undefined ? ' min="' + o.min + '"' : '') +
      ' value="' + (own === null || own === undefined ? '' : own) + '">';
  }
  // Unmeasured is not zero, and neither is blank: the grey figure says what
  // this contract will actually trade on.
  const perSide = /_(hl|lh)$/.test(field);
  const effText = (plain || kind === 'check') ? '' :
    (eff === null || eff === undefined ? (perSide ? 'same as both' : DASH) : String(eff));
  const isDefault = !plain && (own === null || own === undefined);
  return '<label class="f cf-row"><span>' + label +
    (hint ? ' <small>' + hint + '</small>' : '') + '</span>' +
    '<span class="cf-c">' + control +
    (effText ? '<span class="cf-eff' + (isDefault ? '' : ' own') + '" title="' +
      (isDefault ? 'the desk default' : 'this contract&#39;s own setting') +
      '">' + effText + '</span>' : '') + '</span></label>';
}

function paintConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  const row = cfgState[key] && cfgState[key].contract;
  if (!el || !row) return;
  el.querySelector('.title').textContent = row.name || key;
  el.querySelector('.cfg-key').textContent = row.symbol || key;
  const group = CFG_GROUPS.find((g) => g.name === cfgState[key].group)
    || CFG_GROUPS[0];
  el.querySelector('.cfg-body').innerHTML = '<div class="cb">' +
    group.fields.map((f) => cfgField(f[0], f[1], f[2], f[3], f[4], row)).join('') +
    '</div>';
}

async function saveConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (!el) return;
  const body = {};
  el.querySelectorAll('[data-field]').forEach((input) => {
    const field = input.dataset.field;
    if (input.type === 'checkbox') {
      // Untouched and still on the desk default: leave it on the default.
      if (input.dataset.default === '1' &&
          input.checked === (input.dataset.was === '1')) return;
      body[field] = input.checked;
    }
    else if (input.type === 'number') {
      // '' clears the override back to the desk default. 0 is a number.
      body[field] = input.value === '' ? '' : Number(input.value);
    } else body[field] = input.value;
  });
  const res = await fetch('/api/contracts/' + encodeURIComponent(key), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (!out.ok) {
    el.querySelector('.cfg-note').textContent = out.error || 'not saved';
    toast('REJECT', 'NOT SAVED', out.error || 'the settings were not saved', key);
    return;
  }
  cfgState[key].contract = Object.assign({}, cfgState[key].contract, body,
    { effective: out.effective });
  paintConfig(key);
  toast('ORDER', 'SAVED', 'the engine picks this up on its next pass', key);
  el.querySelector('.cfg-note').textContent = 'saved';
}

async function tick() {
  try {
    const res = await fetch('/api/snapshot', { cache: 'no-store' });
    const snap = await res.json();
    renderChrome(snap);
    window.__lastSnapshot = snap;
    const seen = new Set(['__positions__', '__analysis__']);
    (snap.contracts || []).filter((c) => EMBED === null || c.key === EMBED).forEach((c) => {
      seen.add(c.key);
      // A settings panel belongs to its contract and outlives a poll. The
      // sweep below removes windows for contracts that are gone; it must not
      // remove the panel of one that is still here.
      seen.add('__config__' + c.key);
      seen.add('__ladder__' + c.key);
      // The ladder first: on a fresh desk it sits to the LEFT of its Algo.
      renderLadder(c, snap.engine);
      renderContract(c);
    });
    renderPositions(snap);
    if (EMBED !== null) {
      // Order tests: the ladder and the Algo window first, the monitor below.
      const desk = document.getElementById('desktop');
      ['__positions__', '__analysis__'].forEach((k) => {
        const w = desk.querySelector('.win[data-key="' + k + '"]');
        if (w && w.nextElementSibling) desk.appendChild(w);
      });
    }
    if (!analysis.timer) {
      // History, not a market: a slow timer, off the desk's critical path.
      loadAnalysis();
      analysis.timer = setInterval(loadAnalysis, 15000);
    }
    document.querySelectorAll('.win').forEach((el) => {
      if (!seen.has(el.dataset.key)) el.remove();
    });
    renderTabs();
  } catch (e) {
    const banner = document.getElementById('engine-banner');
    banner.classList.remove('hidden');
    banner.classList.add('critical');
    banner.textContent = 'The screen cannot reach its own web process. ' +
      'These prices are not live.';
  }
}

function restartTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(tick, state.refresh);
}

/* -- wiring --------------------------------------------------------------- */

document.getElementById('kill').onclick = async () => {
  const ok = await ask('KILL ALL?',
    'Every algo is stood down and every working order of ours is cancelled. ' +
    'Open positions are NOT closed.', 'KILL ALL');
  if (ok) { await command('kill_all', '', { close_positions: false }); }
};

document.getElementById('master-toggle').onclick = async () => {
  const on = document.getElementById('master-toggle').classList.contains('act');
  await command('master_algo', '', { on: !on });
};
document.getElementById('execution-toggle').onclick = async () => {
  const live = document.getElementById('execution-toggle').classList.contains('live');
  if (live) {
    await command('execution', '', { mode: 'PAPER' });
    return;
  }
  // The engine writes the confirmation — the venue, the account, and
  // whether Stellar's positions could be read — so the screen cannot soften it.
  const asked = await command('execution', '', { mode: 'LIVE' });
  if (!asked || !asked.confirm) return;
  const ok = await ask('Send the Algo\'s orders to Stellar ' + sendWord() + '?', asked.text,
    'Send to ' + sendWord());
  if (!ok) return;
  const done = await command('execution', '', { mode: 'LIVE', confirm: true });
  if (done && done.ok) {
    toast('ORDER', sendWord(), 'the Algo now sends its orders to Stellar ' + sendWord() +
      (done.positions_waived ? ' — Stellar positions unread, on your word' : ''));
  }
};

document.getElementById('sound-toggle').onclick = (e) => {
  state.sound = !state.sound;
  e.target.textContent = 'Sound: ' + (state.sound ? 'on' : 'off');
};

document.getElementById('tidy').onclick = () => {
  state.places = {};
  localStorage.removeItem(LS + 'places');
  state.free = false;
  document.getElementById('desktop').classList.remove('free');
  document.querySelectorAll('.win').forEach((el) => {
    el.classList.remove('placed');
    el.style.left = ''; el.style.top = '';
    el.style.width = ''; el.style.height = '';
  });
};

/* Text size, for the whole terminal. Kept per browser: it is a matter of
 * the screen and the eyes in front of it, not of the desk. */
function setTextSize(scale) {
  const fs = Math.round(Math.min(2.2, Math.max(0.9, scale)) * 10) / 10;
  document.documentElement.style.setProperty('--fs', String(fs));
  try { localStorage.setItem('ft.fs', String(fs)); } catch (e) { /* forgets */ }
  const label = document.getElementById('text-size');
  if (label) label.textContent = 'Text ' + Math.round(fs * 100) + '%';
}
function textSize() {
  return parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--fs')) || 1.3;
}
document.getElementById('text-smaller').onclick = () => setTextSize(textSize() - 0.1);
document.getElementById('text-bigger').onclick = () => setTextSize(textSize() + 0.1);
setTextSize(textSize());

document.getElementById('add-panel').onclick = () => {
  const menu = document.getElementById('add-menu');
  menu.innerHTML = '';
  if (state.closed.size === 0) {
    const b = document.createElement('button');
    b.textContent = 'every window is already open';
    b.disabled = true;
    menu.appendChild(b);
  }
  state.closed.forEach((key) => {
    const b = document.createElement('button');
    b.textContent = key === '__positions__' ? 'Trading Monitor'
      : key === '__analysis__' ? 'Analysis' : key;
    b.onclick = () => {
      state.closed.delete(key);
      localStorage.setItem(LS + 'closed', JSON.stringify([...state.closed]));
      menu.classList.add('hidden');
      tick();
    };
    menu.appendChild(b);
  });
  menu.classList.toggle('hidden');
};

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.getElementById('modal').classList.add('hidden');
    document.getElementById('add-menu').classList.add('hidden');
  }
});

tick();
restartTimer();
