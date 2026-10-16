'use strict';

const POLL_MS = 15000;
let pollTimer = null;

function el(id) { return document.getElementById(id); }

function localDateInput() {
  const d = new Date();
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function fmtTime(iso, tz) {
  if (!iso) return '—';
  try {
    return new Intl.DateTimeFormat([], {
      timeZone: tz || undefined, hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(new Date(iso));
  } catch (e) {
    return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  }
}

function fmtDay(iso, tz) {
  if (!iso) return '';
  try {
    return new Intl.DateTimeFormat([], {
      timeZone: tz || undefined, weekday: 'short', day: 'numeric', month: 'short',
    }).format(new Date(iso));
  } catch (e) {
    return new Date(iso).toLocaleDateString();
  }
}

function minsBetween(a, b) {
  return Math.round((new Date(a) - new Date(b)) / 60000);
}

function relative(iso, nowIso) {
  if (!iso) return '';
  const diff = (new Date(iso) - new Date(nowIso)) / 60000;
  const abs = Math.abs(diff);
  let text;
  if (abs < 1) text = 'now';
  else if (abs < 60) text = `${Math.round(abs)} min`;
  else if (abs < 60 * 36) text = `${Math.round(abs / 60)} h`;
  else text = `${Math.round(abs / 1440)} d`;
  if (text === 'now') return 'now';
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

function statusClass(status) {
  const s = (status || '').toLowerCase();
  if (!s) return 'scheduled';
  if (s.includes('cancel') || s.includes('divert')) return 'bad';
  if (s.includes('landed') || s.includes('arrived')) return 'done';
  if (s.includes('en route') || s.includes('enroute') || s.includes('in-flight') || s.includes('active') || s.includes('airborne')) return 'active';
  if (s.includes('delay') || s.includes('hold') || s.includes('return')) return 'warn';
  return 'scheduled';
}

function endpointHtml(label, airport, data, prefix, nowIso) {
  const kind = prefix === 'out' ? 'origin' : 'destination';
  const scheduled = data['scheduled_' + prefix];
  const estimated = data['estimated_' + prefix];
  const actual = data['actual_' + prefix];
  const tz = (airport && airport.timezone) || null;
  const display = actual || estimated || scheduled;
  const displayKind = actual ? 'Actual' : estimated ? 'Estimated' : 'Scheduled';

  const code = (airport && (airport.iata || airport.code)) || '—';
  const name = (airport && airport.city) || (airport && airport.name) || '';

  let schedLine = '';
  if (display && scheduled && display !== scheduled) {
    schedLine = `<div class="sched">Scheduled ${fmtTime(scheduled, tz)}</div>`;
  }

  let deltaHtml = '';
  if (display && scheduled) {
    const d = minsBetween(display, scheduled);
    if (d >= 2) deltaHtml = `<span class="delta late">+${d} min</span>`;
    else if (d <= -2) deltaHtml = `<span class="delta early">${d} min</span>`;
  }

  const terminal = data['terminal_' + kind];
  const gate = data['gate_' + kind];
  const metaBits = [];
  if (terminal) metaBits.push(`<span>Terminal <strong>${esc(terminal)}</strong></span>`);
  if (gate) metaBits.push(`<span>Gate <strong>${esc(gate)}</strong></span>`);
  const meta = metaBits.length ? `<div class="meta">${metaBits.join('')}</div>` : '';

  return `<div class="endpoint">
    <div class="label">${label}</div>
    <div class="airport">${esc(code)}${name ? ' · ' + esc(name) : ''}</div>
    <div class="big-time">${fmtTime(display, tz)} ${deltaHtml}</div>
    <div class="sched">${displayKind}${display ? ' · ' + relative(display, nowIso) : ''}${display ? ' · ' + fmtDay(display, tz) : ''}</div>
    ${schedLine}
    ${meta}
  </div>`;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function cardHtml(f, nowIso) {
  const d = f.data || {};
  const origin = d.origin || {};
  const dest = d.destination || {};
  const route = (origin.iata || origin.code || '???') + ' → ' + (dest.iata || dest.code || '???');
  const ident = d.ident || f.ident;
  const status = d.status || (d.cancelled ? 'Cancelled' : f.error ? 'No data' : 'Scheduled');

  const progress = (typeof d.progress_percent === 'number' && d.progress_percent > 0 && d.progress_percent < 100)
    ? `<div class="progress"><div style="width:${Math.min(100, d.progress_percent)}%"></div></div>` : '';

  const updated = f.last_fetched ? relative(f.last_fetched, nowIso) : 'not yet';
  const source = d.source ? `source: ${esc(d.source)}` : '';
  const err = f.error ? `<span class="card-error">${esc(f.error)}</span>` : '';

  return `<article class="card" data-id="${f.id}">
    <div class="card-head">
      <span class="card-ident">${esc(ident)}</span>
      <span class="badge ${statusClass(status)}">${esc(status)}</span>
      ${d.airline ? `<span class="card-airline">${esc(d.airline)}</span>` : ''}
      <span class="card-route">${esc(route)}</span>
    </div>
    <div class="times">
      ${endpointHtml('Departure', origin, d, 'out', nowIso)}
      ${endpointHtml('Arrival', dest, d, 'in', nowIso)}
    </div>
    ${progress}
    <div class="card-foot">
      ${d.aircraft_type ? `<span>${esc(d.aircraft_type)}</span>` : ''}
      ${d.registration ? `<span>${esc(d.registration)}</span>` : ''}
      <span>${source}</span>
      <span>updated ${updated}</span>
      ${err}
      <span class="spacer"></span>
      <button type="button" data-action="refresh">Refresh</button>
      <button type="button" class="danger" data-action="delete">Remove</button>
    </div>
  </article>`;
}

function render(data) {
  const container = el('flights');
  const empty = el('empty');
  const flights = data.flights || [];

  if (!flights.length) {
    container.innerHTML = '';
    empty.classList.remove('hidden');
  } else {
    empty.classList.add('hidden');
    container.innerHTML = flights.map((f) => cardHtml(f, data.now)).join('');
  }

  const banner = el('banner');
  if (!data.aero_configured) {
    banner.className = 'banner error';
    banner.textContent = 'AeroAPI key is not configured. Add AEROAPI_KEY to .env to fetch live status.';
    banner.classList.remove('hidden');
  } else if (data.aero_calls_today >= data.aero_max_daily) {
    banner.className = 'banner error';
    banner.textContent = `Daily AeroAPI call limit reached (${data.aero_calls_today}/${data.aero_max_daily}). Refreshing resumes tomorrow.`;
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }

  el('usage').textContent = data.aero_configured
    ? `AeroAPI calls today: ${data.aero_calls_today}/${data.aero_max_daily}`
    : 'AeroAPI not configured';

  const alertsEl = el('alerts');
  const testBtn = el('test-email');
  if (data.alerts_enabled && data.alerts_to) {
    alertsEl.textContent = `Alerts → ${data.alerts_to}`;
    testBtn.classList.remove('hidden');
  } else if (data.alerts_enabled) {
    alertsEl.textContent = 'Alerts: no recipient set';
    testBtn.classList.add('hidden');
  } else {
    alertsEl.textContent = 'Alerts disabled';
    testBtn.classList.add('hidden');
  }
}

async function fetchFlights() {
  try {
    const res = await fetch('/api/flights', { headers: { Accept: 'application/json' } });
    if (res.status === 401) { window.location = '/login'; return; }
    render(await res.json());
  } catch (e) {
    /* keep the last good render on transient errors */
  }
}

function showMessage(text, ok) {
  const m = el('message');
  m.textContent = text;
  m.className = 'message ' + (ok ? 'ok' : 'error');
  m.classList.remove('hidden');
  clearTimeout(m._t);
  m._t = setTimeout(() => m.classList.add('hidden'), 5000);
}

async function addFlight(ev) {
  ev.preventDefault();
  const ident = el('ident').value.trim();
  const date = el('date').value;
  const btn = ev.target.querySelector('button[type="submit"]');
  btn.disabled = true;
  try {
    const res = await fetch('/api/flights', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ident, date }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { showMessage(body.error || 'Could not add flight', false); return; }
    el('ident').value = '';
    showMessage('Flight added. Status will appear shortly.', true);
    fetchFlights();
  } finally {
    btn.disabled = false;
  }
}

async function onCardClick(ev) {
  const btn = ev.target.closest('button[data-action]');
  if (!btn) return;
  const card = btn.closest('.card');
  const id = card.dataset.id;
  const action = btn.dataset.action;
  btn.disabled = true;
  if (action === 'delete') {
    await fetch(`/api/flights/${id}`, { method: 'DELETE' });
  } else {
    await fetch(`/api/flights/${id}/refresh`, { method: 'POST' });
    showMessage('Refresh requested.', true);
  }
  fetchFlights();
}

async function sendTestEmail() {
  const btn = el('test-email');
  btn.disabled = true;
  try {
    const res = await fetch('/api/test-email', { method: 'POST' });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) { showMessage(body.error || 'Could not send test email', false); return; }
    showMessage(`Test email sent to ${body.to}`, true);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  el('date').value = localDateInput();
  el('add-form').addEventListener('submit', addFlight);
  el('flights').addEventListener('click', onCardClick);
  el('test-email').addEventListener('click', sendTestEmail);
  fetchFlights();
  pollTimer = setInterval(fetchFlights, POLL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') fetchFlights();
  });
});
