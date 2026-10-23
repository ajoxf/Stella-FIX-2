'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  let current = null;
  let busy = false;
  const localTime = (value) => value ? new Date(value).toLocaleTimeString([], {hour12: false}) : '—';
  const stateClass = (state) => state === 'CONNECTED' || state === 'LOGGED_ON' ? 'good' : state === 'ERROR' ? 'error' : state === 'CONNECTING' ? 'wait' : '';
  const displayState = (state) => ({LOGGED_ON:'Connected', CONNECTED:'Connected', CONNECTING:'Connecting', DOWN:'Disconnected', DISCONNECTED:'Disconnected', ERROR:'Connection error'}[state] || state || 'Unknown');

  async function request(url, options) {
    const response = await fetch(url, {cache:'no-store', ...options});
    if (!response.ok) throw new Error(`Request failed (${response.status}). Check that FIX-Trader is running.`);
    return response.json();
  }

  function controls() {
    const engine = current?.engine || {};
    const connection = engine.fix_connection;
    const ready = engine.alive && connection && !busy;
    const sequenceMismatch = (connection?.sessions || []).some(session =>
      String(session.error || '').toLowerCase().includes('sequence mismatch'));
    $('connect').disabled = !ready || sequenceMismatch || ['LOGGED_ON','CONNECTING'].includes(connection?.state);
    $('reconnect').disabled = !ready || sequenceMismatch || connection?.state === 'CONNECTING';
    $('disconnect').disabled = !ready || connection?.state === 'DOWN';
    // Only with the Order Router down: the numbers it starts from next.
    $('reset_sequences').disabled = !ready || !['DOWN', 'ERROR'].includes(connection?.state);
  }

  function render(snapshot) {
    const previousState = current?.engine?.fix_connection?.state;
    current = snapshot;
    const engine = snapshot.engine || {};
    const connection = engine.fix_connection;
    // Served at /connection the top bar's badge is #environment; served at /
    // (a Stellar venue lands here) it is #env-badge. A missing one threw here,
    // and the page read "Cannot reach FIX-Trader" with the engine running.
    const envBadge = $('environment') || $('env-badge');
    if (envBadge) envBadge.textContent = engine.simulated ? 'SIMULATED' : engine.environment || 'UNKNOWN';
    $('engine-health').textContent = engine.alive ? 'Engine online · updates every second' : 'Engine offline';
    $('engine-health').className = 'health' + (engine.alive ? ' good' : '');
    const state = engine.alive ? connection?.state : 'ERROR';
    if (previousState === 'CONNECTING' && state === 'LOGGED_ON') {
      $('action-result').className = 'feedback';
      $('action-result').textContent = 'Both FIX sessions are connected.';
    }
    $('overall-state').textContent = !engine.alive ? 'Engine unavailable' : connection ? displayState(state) : engine.waiting_for_venue ? 'Waiting for a Stellar venue' : 'No FIX session configured';
    $('overall-detail').textContent = !engine.alive ? (engine.text || 'Start FIX-Trader to manage the connection.') : connection?.reconnect_in ? `Waiting ${connection.reconnect_in}s before reconnecting to Stellar…` : connection?.text || engine.text || 'Configure a Stellar venue and start the FIX engine.';
    document.querySelector('.summary').className = 'summary ' + stateClass(state);
    $('venue').textContent = connection?.venue || 'No venue';
    const cards = document.createDocumentFragment();
    for (const session of connection?.sessions || []) {
      const card = $('session-template').content.cloneNode(true);
      const put = (selector, value) => { card.querySelector(selector).textContent = value; };
      put('.session-name', session.name + ' FIX');
      put('.session-status', engine.alive ? displayState(session.status) : 'Status unavailable');
      card.querySelector('.session-status').className = 'session-status tag ' + (engine.alive ? stateClass(session.status) : 'error');
      put('.endpoint', `${session.host || 'Not configured'}:${session.port || '—'}`);
      put('.sender', session.sender_comp_id || '—'); put('.target', session.target_comp_id || '—');
      put('.credentials', session.password_set ? 'Configured' : 'Missing');
      put('.incoming', session.incoming_count); put('.outgoing', session.outgoing_count);
      put('.sequence', `${session.in_seq} / ${session.out_seq}`);
      put('.logon', localTime(session.last_logon)); put('.heartbeat', localTime(session.last_heartbeat));
      put('.last-message', localTime(session.last_message));
      const error = card.querySelector('.session-error');
      error.hidden = !session.error; error.textContent = session.error || '';
      cards.append(card);
    }
    $('sessions').replaceChildren(cards);
    const rows = document.createDocumentFragment();
    for (const item of (connection?.activity || []).slice(0,40)) {
      const row = document.createElement('tr');
      for (const value of [localTime(item.time), item.session, item.direction === 'IN' ? 'Received' : 'Sent', item.type, item.sequence]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      rows.append(row);
    }
    if (!rows.childNodes.length) {
      const row = document.createElement('tr'); const cell = document.createElement('td');
      cell.colSpan = 5; cell.textContent = 'No session activity yet. Connect FIX to start.'; row.append(cell); rows.append(row);
    }
    $('activity').replaceChildren(rows);
    $('updated').textContent = `Updated ${localTime(snapshot.ts)}`;
    controls();
  }

  async function poll() {
    try { render(await request('/api/snapshot')); }
    catch (error) {
      current = null;
      $('engine-health').textContent = 'Engine unavailable'; $('engine-health').className = 'health';
      $('overall-state').textContent = 'Cannot reach FIX-Trader'; $('overall-detail').textContent = error.message;
      document.querySelector('.summary').className = 'summary error';
      document.querySelectorAll('.session-status').forEach(el => {el.textContent = 'Status unavailable'; el.className = 'session-status tag error';});
      controls();
    }
    setTimeout(poll, 1000);
  }

  async function command(action) {
    if (busy || !current?.engine?.fix_connection) return;
    busy = true; controls();
    $('action-result').className = 'feedback'; $('action-result').textContent = 'Sending request to the FIX engine…';
    try {
      const queued = await request('/api/command', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:`fix_${action}`, args:{venue:current.engine.fix_connection.venue}})});
      if (!queued.ok) throw new Error(queued.error || 'Connection request was refused.');
      const deadline = Date.now() + 40000;
      let result;
      while (Date.now() < deadline) {
        result = await request(`/api/result/${encodeURIComponent(queued.id)}`);
        if (!result.pending) break;
        await new Promise(resolve => setTimeout(resolve, 200));
      }
      if (!result || result.pending) throw new Error('The engine has not answered. Check its status before retrying.');
      if (!result.ok) throw new Error(result.error || result.rows?.[0]?.detail || 'Request failed.');
      $('action-result').textContent = action === 'disconnect' ? 'FIX sessions disconnected.' : action === 'reconnect' ? 'Reconnect scheduled. The sessions will reconnect after a 10-second pause.' : action === 'reset_sequences' ? 'Order Router sequence numbers reset: the next logon starts from 1.' : 'Connection requested. Waiting for Stellar to acknowledge logon.';
      render(await request('/api/snapshot'));
    } catch (error) { $('action-result').textContent = error.message; $('action-result').className = 'feedback error'; }
    finally {busy = false; controls();}
  }
  for (const action of ['connect','disconnect','reconnect']) $(action).addEventListener('click', () => command(action));
  // Resetting the numbers is right ONLY when Stellar has reset its side: a
  // second press within five seconds confirms it — no native confirm().
  let resetArmed = 0;
  $('reset_sequences').addEventListener('click', () => {
    if (Date.now() - resetArmed < 5000) { resetArmed = 0; $('reset_sequences').textContent = 'Reset sequence numbers'; command('reset_sequences'); return; }
    resetArmed = Date.now();
    $('reset_sequences').textContent = 'Press again to reset';
    $('action-result').className = 'feedback';
    $('action-result').textContent = 'Only when Stellar confirms it has reset its side of the Order Router: the next logon then starts from 1. Press again within 5 seconds.';
    setTimeout(() => { if (resetArmed) { resetArmed = 0; $('reset_sequences').textContent = 'Reset sequence numbers'; } }, 5000);
  });
  poll();
})();
