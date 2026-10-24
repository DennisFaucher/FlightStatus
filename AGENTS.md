# AGENTS.md

Guidance for agents working in this repository. Read this before changing code.

## What this is

A self-hosted flight tracker. Users add a flight number and a date; a background
worker refreshes the flight from FlightAware AeroAPI (with airframes.io as a
fallback) and emails the owner (plus optional CCs) when something meaningful
changes.

Deliberately small: one Python 3.12 / Flask service, no database, state in a
single JSON file. API budget is a first-class design constraint — do not add
calls or shorten refresh intervals without good reason.

## Architecture

```
app.py         Flask app: config, JSON storage, adaptive refresh worker,
               alerts wiring, auth, HTTP API, startup
providers.py   AeroAPI + airframes.io clients, flight selection, normalisation
alerts.py      Snapshot diffing, change detection, plain-text/HTML rendering
mailer.py      SMTP sending (STARTTLS / implicit TLS)
templates/     index.html (dashboard), login.html
static/        app.js (vanilla JS polling UI), style.css, favicon.svg
data/          Runtime state: flights.json + .worker.lock (git-ignored)
```

Request flow: browser → `app.py` routes → `load_store()` / `save_store()`.
Refresh flow: worker thread → `should_fetch()` → `perform_fetch()` →
`providers.lookup()` → `_apply_alerts()` → `mailer.send()`.

### The refresh worker

- Started as a daemon thread at import time (`_start_worker()` at the bottom of
  `app.py`), so it runs inside the web process. Two Gunicorn workers exist, but
  a `fcntl` lock on `data/.worker.lock` (`_acquire_worker_lock`) ensures only one
  refreshes/sends at a time.
- Ticks every 10s (`WORKER_INTERVAL`). Each tick: prune finished flights once
  per UTC day, fetch flights whose TTL expired, apply alerts, check the daily
  AeroAPI cap, then save only if something changed.
- `compute_ttl()` decides the per-flight interval from time-to-departure
  (`REFRESH_*_SECONDS`): daily when far out, down to 10 minutes when imminent or
  in the air. `compute_final()` marks arrived/cancelled/overdue flights as final.
- Failed fetches get exponential backoff via `FETCH_BACKOFF_SECONDS` and
  `next_fetch_ts`; the outer loop rate-limits tracebacks to once per 5 minutes.
  Keep both protections when touching fetch logic, or a poison flight will spin.
- AeroAPI usage is capped per UTC day by `AEROAPI_MAX_DAILY_CALLS`; the counter
  lives in the store (`aero_calls`). Hitting the cap sends one email per day
  (`aero_limit_notified`) and stops calls until the UTC day rolls over.
  `_respect_rate_limit()` enforces the minimum gap between calls.

### Alert pipeline (`alerts.py` + `_apply_alerts`)

- `alerts.snapshot(data)` captures only the fields that matter (status,
  cancelled/diverted, the six time fields, terminals/gates).
- `alerts.build_changes(prev, cur, data)` returns human-readable strings and
  deliberately ignores estimated-time jitter under `TIME_CHANGE_MINUTES` (5).
- The first snapshot is stored silently (no email). On send success, the
  snapshot advances; suppression relies on storing the *alerted* snapshot, not
  the latest data.
- Send failures are retried with `ALERT_RETRY_SECONDS` backoff; after
  `ALERT_MAX_RETRIES` (5) the flight's alerts pause for 24h.
- The "leaving soon" reminder is tracked separately by `alerts.reminder_for`
  and fires `ALERT_LEAD_MINUTES` before departure.
- If SMTP is unconfigured or there is no recipient, the result is `"skip"` and
  the snapshot still advances, so later configuration does not replay a backlog.
- `render_changes` / `render_reminder` / `render_notice` each produce
  `(subject, text, html)`; every user-visible string is HTML-escaped in
  `_render_html` / `_shell`.
- Change-email subjects lead with the most newsworthy change (`SUBJECT_ORDER`:
  status, cancellation/diversion, times, then gates). First-time gate/terminal
  assignments are phrased `Arrival terminal E` rather than
  `not assigned → E`; the body still shows the full before → after diff.

## Data model and invariants

`data/flights.json` holds `{"version", "flights": [...], "aero_calls": {},
"aero_limit_notified": {}}`. Each flight includes `id`, `ident`, `date`,
`owner`, `cc`, `data` (normalised provider dict), `alerts` (snapshot/retry
state), `final`, and timing/backoff fields. Saves are atomic (`.tmp` +
`os.replace`).

Invariants to preserve:

- **Owner scoping.** Every flight has an `owner`; `GET /api/flights` filters to
  the current user; delete/refresh are owner-only (404 / 403). Never return
  another user's flight.
- **Date semantics.** A flight's `date` is the **local date at the departure
  airport**, not UTC. AeroAPI windows are UTC-keyed, so `_date_window()` widens
  by a day either side and `_pick_flight()` / `_flight_date_matches()` select
  the leg whose origin-local date matches. Do not "simplify" this to UTC date
  comparison.
- **Provider normalisation.** All providers return the `_empty_flight()` shape
  with UTC ISO-8601 times; airport `timezone` is what the frontend and email
  renderer use for local display. Keep new fields in that shape.
- **Dedup.** Duplicate `ident + date` is rejected per owner (409); different
  users may track the same flight.
- **Final flights** stop being fetched and are pruned at day rollover
  (`last_prune_date`), never on the day they land.
- **AeroAPI counts.** Every AeroAPI request must go through code paths that
  increment `aero_calls` and respect the cap — including the add-flight
  validation call.
- **State is guarded by `STATE_LOCK`** in routes; keep load/modify/save inside
  the lock to avoid lost updates.

## Conventions

- Pure standard library + Flask + requests; no ORM, no frontend framework, no
  build step. The UI is server-rendered HTML plus `static/app.js` using
  `fetch`, polling every 15s.
- Configuration is environment-only, read once into module constants, with
  `_env_int()` for tolerant integer parsing. Add new settings to `.env.example`
  *and* the README configuration table.
- Logging via module loggers (`flightstatus.app`, `.providers`, `.mailer`).
  Expected upstream failures (rate limit, bad key, 503) are `ProviderError` /
  `MailError` and should log a warning, not a traceback.
- Keep comments explaining *why* (budget, date windows, backoff) rather than
  restating the code.
- Match existing style: 4-space indent, ~88 columns, double-quoted docstrings,
  small helpers, section banner comments in `app.py`.

## Running and verifying

Docker (the normal path):

```bash
cp .env.example .env      # AEROAPI_KEY, SECRET_KEY, optional MAIL_*
cp .users.example .users  # username:password[:email]
docker compose up -d --build
# http://localhost:40074  (container 8000, TZ=America/New_York)
```

Local without Docker:

```bash
python3 -m pip install -r requirements.txt
DATA_DIR=./data USERS_FILE=./.users SECRET_KEY=dev python3 app.py
```

Note that importing `app` starts the worker thread; avoid importing it casually
in ad-hoc scripts unless you intend live refreshes.

**There is no test suite, linter, or CI in this repo** — add one if the change
warrants it, but until then verify manually:

```bash
python3 -m py_compile app.py providers.py alerts.py mailer.py
curl -fsS http://localhost:40074/healthz   # {"status":"ok"}
```

Manual checks worth doing after a change: add a flight (invalid ident/date,
duplicate, unknown flight), refresh/delete it as a non-owner, inspect
`data/flights.json` after a refresh, and send the test email via
`POST /api/test-email` or the dashboard button.

## Gotchas

- `load_dotenv("/app/.env")` runs before the Flask app is created so env vars
  are available to `providers`/`mailer`. Docker mounts `.env` and `.users`
  read-only into the container.
- Flights less than ~10 days old and more than ~2 days ahead are outside
  AeroAPI's live window; future flights use `/schedules/{start}/{end}`, which
  has times but no gates/terminals.
- AeroAPI returns HTTP 401 for both bad keys and endpoints outside the
  subscription tier; `_aero_401_message()` inspects the body — keep that
  distinction.
- `.env`, `.users` and `data/` are secrets/state and are git-ignored. Never
  commit or log credentials. `mailer.send()` strips whitespace from the app
  password (Gmail prints it in groups).
- The README is user-facing documentation; when behaviour, configuration, or
  the HTTP API changes, update the README alongside the code.
