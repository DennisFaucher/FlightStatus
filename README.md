# Flight Status

A small self-hosted flight tracker. Add a flight number and a date, and the app
keeps an eye on it — live departure/arrival times, terminals and gates — and
emails you (and the people you share it with) when anything changes.

Built around the [FlightAware AeroAPI](https://www.flightaware.com/aeroapi)
personal plan, so refresh is deliberately lazy and adaptive to keep you inside
the free monthly credit.

<img width="944" height="468" alt="image" src="https://github.com/user-attachments/assets/dae8a465-e8b9-4bd0-a4df-985bfa0c0344" />

## Features

- **Track any flight by number and date** — IATA (`EI136`) or ICAO (`EIN136`)
  idents, resolved to the right leg and codeshare automatically.
- **Live details** — scheduled/estimated/actual departure and arrival times,
  terminals and gates, aircraft type and registration, and status
  (scheduled, departed, en route, landed, cancelled, diverted).
- **Email alerts** — sent when a flight's status changes, when its
  gate/terminal changes, and a "leaving soon" reminder before departure.
  Each flight's alerts go to the person who added it.
- **CC emails** — optional comma-separated addresses to CC on every alert for a flight.
- **Multi-user** — a simple file-based login; every tracked flight records its
  owner.
- **Per-user visibility** — each user sees only the flights they added; delete
  and refresh are owner-only.
- **Auto-cleanup** — completed flights (arrived, cancelled or overdue) are
  pruned at the next day's rollover, not before.
- **Budget guardrails** — an adaptive refresh schedule plus a hard daily call
  cap, and an email when the cap is reached.
- **Graceful fallback** — falls back to [airframes.io](https://airframes.io)
  when AeroAPI has no data.
- **Tiny footprint** — one Flask container, no database; state is a single JSON
  file.

## How it works

A background worker inside the web process re-queries flights on an adaptive
schedule: infrequently when a flight is days away, and every few minutes once it
is close to departure or in the air. A cross-process file lock ensures only one
worker refreshes and sends at a time, even with multiple Gunicorn workers — which
also keeps API usage predictable.

Flight data is fetched from AeroAPI (falling back to airframes.io), normalised to
a common shape, and stored alongside an alert snapshot so changes can be diffed
between refreshes. The same worker prunes completed flights once the UTC day
rolls over, so the dashboard stays clean without ever dropping a flight on the
day it landed.

Dates are interpreted as the **local date at the departure airport** — the way a
passenger thinks about "the flight on the 16th" — even though the upstream APIs
key their windows by UTC.

## Quick start

Requires Docker and Docker Compose.

```bash
git clone https://github.com/<you>/FlightStatus.git
cd FlightStatus

cp .env.example .env         # then fill in your keys (see Configuration)
cp .users.example .users     # then set your username/password/email

docker compose up -d --build
```

Then open <http://localhost:40074> and sign in.

## Configuration

All configuration lives in `.env` (copy it from `.env.example`). Only
`AEROAPI_KEY` and `SECRET_KEY` are really required; leave `MAIL_HOST` blank to
disable email alerts entirely.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AEROAPI_KEY` | — | FlightAware AeroAPI key (primary data source). |
| `AIRFRAMES_API_KEY` | — | Optional airframes.io key for the fallback source. |
| `SECRET_KEY` | — | Signs the session cookie. Use a long random string. |
| `REFRESH_*_SECONDS` | various | Adaptive refresh intervals by how close the flight is. |
| `AEROAPI_MIN_INTERVAL_SECONDS` | `7` | Minimum gap between two AeroAPI calls. |
| `AEROAPI_MAX_DAILY_CALLS` | `30` | Hard daily call cap (UTC day); protects your credit. |
| `MAIL_HOST` | — | SMTP host; blank disables alerts. |
| `MAIL_PORT` | `587` | SMTP port (STARTTLS; `465` uses SSL). |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | — | SMTP credentials (Gmail App Password works). |
| `MAIL_FROM` / `MAIL_FROM_NAME` | — | Visible From address and name. |
| `MAIL_TO` | — | Fallback recipient when a flight's owner has no email. |
| `ALERT_LEAD_MINUTES` | `120` | How long before departure to send the reminder. |
| `ALERT_RETRY_SECONDS` | `300` | Backoff between alert send retries. |

`docker-compose.yml` sets the container timezone (`TZ`), port (`40074:8000`),
and mounts `./data`, `./.users` and `./.env` into the container.

## Users

`.users` holds one account per line:

```
username:password:email
```

The email is optional and used as the alert recipient for flights that user
tracks. The file is bind-mounted read-only into the container and is listed in
`.gitignore` — keep it out of version control.

## Email alerts

When SMTP is configured, alerts are sent for:

- **Status changes** — scheduled → departed → en route → landed, cancellations,
  diversions.
- **Gate / terminal changes** — at either end of the flight.
- **Leaving soon** — a reminder `ALERT_LEAD_MINUTES` before departure.

Each flight can also list additional CC email addresses (comma-separated) when
added; these are included on all alert emails for that flight.

Duplicate notifications are suppressed by diffing against the previous snapshot.
For Gmail, create an [App Password](https://myaccount.google.com/apppasswords)
(requires 2-Step Verification); the spaces Google shows between the groups are
ignored.

## AeroAPI usage

The personal AeroAPI plan includes a $5/month credit and a limit of 10 queries
per minute. This app keeps you inside that:

- Adaptive per-flight refresh intervals (far flights are queried rarely).
- A minimum interval between calls (`AEROAPI_MIN_INTERVAL_SECONDS`).
- A hard daily cap (`AEROAPI_MAX_DAILY_CALLS`); when reached, the app stops
  calling for the rest of the UTC day and emails the admin once.

Live flight data is available roughly −10 days to +2 days from now; beyond that
the app uses the airline schedule endpoint, which provides times but not gates.

## HTTP API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Dashboard (requires login). |
| `GET` | `/login`, `/logout` | Session login/logout. |
| `GET` | `/healthz` | Liveness probe → `{"status":"ok"}`. |
| `GET` | `/api/flights` | List your flights + current data (only flights you added). |
| `POST` | `/api/flights` | Add a flight (`{"ident": "...", "date": "YYYY-MM-DD", "cc": "a@b.com,c@d.com"}`). |
| `DELETE` | `/api/flights/<id>` | Stop tracking a flight (owner only; 404 if not yours). |
| `POST` | `/api/flights/<id>/refresh` | Force an immediate refresh (owner only; 403 if not yours). |
| `POST` | `/api/test-email` | Send a test alert email. |

## Project layout

```
app.py             Flask app: auth, storage, refresh worker, API, alerts wiring
providers.py       Data sources (AeroAPI + airframes.io) and normalisation
alerts.py          Change detection and email rendering
mailer.py          SMTP sending
templates/         index.html, login.html
static/            app.js, style.css, favicon.svg
Dockerfile         python:3.12-slim + gunicorn
docker-compose.yml Port 40074 → 8000, ./data volume
.env.example       Documented configuration template
.users.example     Example accounts file
```

## Security notes

- `.env`, `.users` and `data/` are git-ignored — never commit them; they hold
  API keys, passwords and tracked-flight state.
- The session cookie is signed with `SECRET_KEY`; set it to a long random value.
- Terminate TLS in front of the app (reverse proxy) if exposing it beyond your
  LAN.
