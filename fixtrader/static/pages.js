/* Shared by the Settings and Exchanges pages: the modal, the toasts, and the
 * environment badge. Kept out of app.js because the desk and the pages are
 * separate documents and neither should load the other's machinery.
 */
'use strict';

const DASH = '—';

function toast(kind, title, message, sub) {
  const host = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = '<div class="hd"><span class="kind"></span>' +
    '<span class="tm"></span><span class="x">&times;</span></div>' +
    '<div class="msg"></div><div class="sub"></div>';
  el.querySelector('.kind').textContent = title;
  el.querySelector('.tm').textContent =
    new Date().toLocaleTimeString([], { hour12: false });
  el.querySelector('.msg').textContent = message;
  el.querySelector('.sub').textContent = sub || '';
  el.querySelector('.x').onclick = () => el.remove();
  host.prepend(el);
  // A failure that vanishes in three seconds is one the operator misses.
  if (kind !== 'REJECT') setTimeout(() => el.remove(), 9000);
  while (host.children.length > 4) host.lastChild.remove();
}

/* The one modal. No native confirm() / alert() / prompt(), ever. */
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
    no.onclick = () => done(false);          // unanswered means NO
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.getElementById('modal').classList.add('hidden');
});

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-store' });
  return res.json();
}

async function postJSON(url, body, method) {
  const res = await fetch(url, {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* an empty body is fine */ }
  return { status: res.status, data };
}

/* The environment badge, on every page. A live screen must never be
 * mistakable for a test one, or the reverse. */
async function paintEnvBadge() {
  const badge = document.getElementById('env-badge');
  if (!badge) return;
  try {
    const snap = await getJSON('/api/snapshot');
    const engine = snap.engine || {};
    badge.textContent = engine.environment || DASH;
    badge.className = 'env' + (engine.environment === 'PROD' ? ' prod'
      : engine.simulated ? ' sim' : '');
  } catch (e) {
    badge.textContent = DASH;
  }
}
paintEnvBadge();
setInterval(paintEnvBadge, 5000);
