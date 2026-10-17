"""Flight Status — a small self-hosted flight tracker.

Users add a flight number and a date; the app keeps departure/arrival times and
terminals up to date using FlightAware AeroAPI (with airframes.io as a fallback).

Refresh is deliberately lazy and adaptive so that a personal AeroAPI plan
($5/month, 10 queries/minute) is not exhausted.
"""

import fcntl
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

from dotenv import load_dotenv

load_dotenv("/app/.env", override=False)
load_dotenv(".env", override=False)

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import alerts
import mailer
import providers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("flightstatus.app")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
DATA_DIR = os.environ.get("DATA_DIR", "/data")
USERS_FILE = os.environ.get("USERS_FILE", "/app/.users")
SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-default-change-me")
PORT = int(os.environ.get("PORT", "8000"))
STORAGE = os.path.join(DATA_DIR, "flights.json")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TTL_FAR = _env_int("REFRESH_FAR_SECONDS", 86400)
TTL_NEAR = _env_int("REFRESH_NEAR_SECONDS", 21600)
TTL_SOON = _env_int("REFRESH_SOON_SECONDS", 7200)
TTL_IMMINENT = _env_int("REFRESH_IMMINENT_SECONDS", 3600)
TTL_ACTIVE = _env_int("REFRESH_ACTIVE_SECONDS", 600)
TTL_STALE = _env_int("REFRESH_STALE_SECONDS", 1800)
TTL_UNKNOWN = _env_int("REFRESH_UNKNOWN_SECONDS", 3600)
AERO_MIN_INTERVAL = _env_int("AEROAPI_MIN_INTERVAL_SECONDS", 7)
AERO_MAX_DAILY = _env_int("AEROAPI_MAX_DAILY_CALLS", 30)
WORKER_INTERVAL = 10

ALERT_LEAD_MINUTES = _env_int("ALERT_LEAD_MINUTES", 120)
ALERT_RETRY_SECONDS = _env_int("ALERT_RETRY_SECONDS", 300)
ALERT_MAX_RETRIES = 5
WORKER_LOCK_FILE = os.path.join(DATA_DIR, ".worker.lock")

IDENT_RE = re.compile(r"^[A-Z0-9]{3,8}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

STATE_LOCK = threading.RLock()
_rate_lock = threading.Lock()
_last_call_ts = 0.0


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _empty_store():
    return {"version": 1, "flights": [], "aero_calls": {}, "aero_limit_notified": {}}


def load_store():
    try:
        with open(STORAGE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return _empty_store()
    data.setdefault("flights", [])
    data.setdefault("aero_calls", {})
    data.setdefault("aero_limit_notified", {})
    return data


def save_store(store):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORAGE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, sort_keys=True)
    os.replace(tmp, STORAGE)


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Refresh scheduling
# --------------------------------------------------------------------------- #
def compute_ttl(data, now_ts):
    """Seconds before this flight should be re-queried, or None to stop."""
    if not data:
        return TTL_UNKNOWN
    if data.get("cancelled") or data.get("actual_in"):
        return None
    dep = _parse_ts(data.get("estimated_out") or data.get("scheduled_out"))
    arr = _parse_ts(
        data.get("actual_in") or data.get("estimated_in") or data.get("scheduled_in")
    )
    if dep is None:
        return TTL_UNKNOWN
    if now_ts >= dep - 3600:
        if arr and now_ts >= arr:
            return TTL_STALE
        return TTL_ACTIVE
    if now_ts >= dep - 3 * 3600:
        return TTL_IMMINENT
    if now_ts >= dep - 12 * 3600:
        return TTL_SOON
    if now_ts >= dep - 48 * 3600:
        return TTL_NEAR
    return TTL_FAR


def compute_final(data, now_ts):
    if not data:
        return False
    if data.get("cancelled") or data.get("actual_in"):
        return True
    scheduled_in = _parse_ts(data.get("scheduled_in"))
    if scheduled_in and now_ts > scheduled_in + 6 * 3600:
        return True
    return False


def should_fetch(flight, now_ts):
    if flight.get("refresh_requested"):
        return True
    if flight.get("final"):
        return False
    last_fetched = flight.get("last_fetched_ts") or 0
    if not flight.get("data"):
        return now_ts - (flight.get("last_attempt_ts") or 0) >= 300
    ttl = compute_ttl(flight["data"], now_ts)
    if ttl is None:
        return False
    return now_ts - last_fetched >= ttl


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _aero_calls_today(store):
    return int(store.get("aero_calls", {}).get(_today(), 0))


def _respect_rate_limit():
    global _last_call_ts
    with _rate_lock:
        wait = AERO_MIN_INTERVAL - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()


def _not_found_message(ident, date_str):
    return (
        f"No {ident} flight found departing {date_str}. "
        "Double-check the flight number and the departure date "
        "(a flight's date is the local date at its departure airport)."
    )


def perform_fetch(store, flight):
    """Query providers for one flight and update it in place."""
    use_aero = bool(providers.aero_key())
    if use_aero:
        if _aero_calls_today(store) >= AERO_MAX_DAILY:
            flight["error"] = (
                f"Daily AeroAPI limit of {AERO_MAX_DAILY} calls reached; "
                "will resume tomorrow"
            )
            flight["last_attempt_ts"] = time.time()
            flight["refresh_requested"] = False
            return
        _respect_rate_limit()
        calls = store.setdefault("aero_calls", {})
        calls[_today()] = calls.get(_today(), 0) + 1

    try:
        data = providers.lookup(flight["ident"], flight["date"])
        flight["data"] = data
        if data:
            flight["error"] = None
        else:
            flight["error"] = _not_found_message(flight["ident"], flight["date"])
    except providers.ProviderError as exc:
        flight["error"] = str(exc)
        data = flight.get("data")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("unexpected error fetching %s", flight.get("ident"))
        flight["error"] = f"Lookup failed: {exc}"
        data = flight.get("data")

    now_ts = time.time()
    flight["last_fetched_ts"] = now_ts
    flight["last_fetched"] = datetime.now(timezone.utc).isoformat()
    flight["last_attempt_ts"] = now_ts
    flight["refresh_requested"] = False
    if flight.get("data"):
        flight["final"] = compute_final(flight["data"], now_ts)


def _notify_aero_limit(store, now_ts):
    """Email once per UTC day when the daily AeroAPI allowance is exhausted."""
    if _aero_calls_today(store) < AERO_MAX_DAILY:
        return False
    today = _today()
    notified = store.setdefault("aero_limit_notified", {})
    if notified.get(today):
        return False
    if now_ts < float(store.get("aero_limit_notify_retry_at") or 0):
        return False

    stale = sorted({f.get("ident") for f in store.get("flights", []) if f.get("error")})
    bullets = [f"Calls used: {AERO_MAX_DAILY} of {AERO_MAX_DAILY}"]
    bullets += [f"May be stale: {ident}" for ident in stale]
    footer = (
        "The counter resets at 00:00 UTC and refreshing resumes automatically. "
        "Raise AEROAPI_MAX_DAILY_CALLS in .env for more headroom."
    )
    subject, text, html_body = alerts.render_notice(
        f"AeroAPI daily limit reached ({AERO_MAX_DAILY} calls)",
        [
            f"Flight Status has used its full AeroAPI allowance for the UTC day {today}.",
        ],
        bullets,
        footer,
    )

    recipient = mailer.default_recipient()
    if not mailer.configured() or not recipient:
        notified[today] = now_ts
        store.pop("aero_limit_notify_retry_at", None)
        log.warning("AeroAPI daily limit reached but alerts are not configured")
        return True

    try:
        mailer.send(recipient, subject, text, html_body)
    except mailer.MailError as exc:
        log.warning("could not send AeroAPI limit notice: %s", exc)
        store["aero_limit_notify_retry_at"] = now_ts + 600
        return True

    notified[today] = now_ts
    store.pop("aero_limit_notify_retry_at", None)
    for day in [d for d in notified if d != today]:
        notified.pop(day, None)
    return True


_worker_lock_handle = None


def _acquire_worker_lock():
    """Cross-process lock so only one gunicorn worker refreshes/sends at a time."""
    global _worker_lock_handle
    os.makedirs(DATA_DIR, exist_ok=True)
    if _worker_lock_handle is None:
        _worker_lock_handle = open(WORKER_LOCK_FILE, "w")
    try:
        fcntl.flock(_worker_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _worker():
    while True:
        try:
            if _acquire_worker_lock():
                with STATE_LOCK:
                    store = load_store()
                    now_ts = time.time()
                    changed = False
                    today = _today()
                    if store.get("last_prune_date") != today:
                        before = len(store["flights"])
                        store["flights"] = [
                            f for f in store["flights"] if not f.get("final")
                        ]
                        if len(store["flights"]) != before:
                            changed = True
                        store["last_prune_date"] = today
                        changed = True
                    for flight in store["flights"]:
                        if should_fetch(flight, now_ts):
                            perform_fetch(store, flight)
                            changed = True
                        if _apply_alerts(flight, now_ts):
                            changed = True
                    if _notify_aero_limit(store, now_ts):
                        changed = True
                    if changed:
                        save_store(store)
        except Exception:  # pragma: no cover - keep the worker alive
            log.exception("refresh worker error")
        time.sleep(WORKER_INTERVAL)


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def resolve_recipient(flight):
    """Email address that should receive alerts for this flight."""
    users = load_users()
    owner = flight.get("owner")
    if owner and owner in users:
        address = users[owner].get("email")
        if address:
            return address
    return mailer.default_recipient()


def _mail_ready(flight):
    if flight.get("alerts_disabled"):
        return None
    if not mailer.configured():
        return None
    return resolve_recipient(flight)


def _send_alert(flight, rendered):
    subject, text, html_body = rendered
    recipient = _mail_ready(flight)
    if not recipient:
        return "skip"
    try:
        mailer.send(recipient, subject, text, html_body)
        return "sent"
    except mailer.MailError as exc:
        log.warning("alert for %s not sent: %s", flight.get("ident"), exc)
        return "retry"


def _alert_backoff(state, now_ts):
    """Return the timestamp before which no send should be attempted."""
    return float(state.get("retry_at") or 0)


def _record_failure(state, now_ts):
    state["fail_count"] = int(state.get("fail_count") or 0) + 1
    if state["fail_count"] >= ALERT_MAX_RETRIES:
        state["retry_at"] = now_ts + 86400
        state["fail_count"] = 0
        log.error(
            "giving up on alerts for %s for 24h after repeated send failures",
            state.get("ident"),
        )
    else:
        state["retry_at"] = now_ts + ALERT_RETRY_SECONDS


def _apply_alerts(flight, now_ts):
    """Diff the current data against the last alerted state; email on change."""
    data = flight.get("data")
    if not data:
        return False

    state = flight.setdefault("alerts", {})
    state.setdefault("ident", flight.get("ident"))
    changed = False

    snapshot = alerts.snapshot(data)
    previous = state.get("snapshot")

    if previous is None:
        # First time we have data: remember it without emailing.
        state["snapshot"] = snapshot
        return True

    changes = alerts.build_changes(previous, snapshot, data)
    if changes and now_ts >= _alert_backoff(state, now_ts):
        result = _send_alert(flight, alerts.render_changes(flight, data, changes))
        if result in ("sent", "skip"):
            # "skip" = alerts not configured / no recipient: move on quietly so
            # configuring SMTP later doesn't replay a backlog of old changes.
            state["snapshot"] = snapshot
            state["fail_count"] = 0
            state.pop("retry_at", None)
            changed = True
        elif result == "retry":
            _record_failure(state, now_ts)
            changed = True

    if _apply_reminder(flight, data, state, now_ts):
        changed = True
    return changed


def _apply_reminder(flight, data, state, now_ts):
    if flight.get("final") or data.get("actual_out") or data.get("cancelled"):
        return False
    dep_value = data.get("estimated_out") or data.get("scheduled_out")
    dep_ts = _parse_ts(dep_value)
    if not dep_value or not dep_ts:
        return False
    if state.get("reminder_for") == dep_value:
        return False
    if now_ts < dep_ts - ALERT_LEAD_MINUTES * 60:
        return False
    if now_ts < _alert_backoff(state, now_ts):
        return False
    result = _send_alert(
        flight, alerts.render_reminder(flight, data, dep_value, now_ts)
    )
    if result == "sent":
        state["reminder_for"] = dep_value
        state["fail_count"] = 0
        state.pop("retry_at", None)
        return True
    if result == "skip":
        return False
    if result == "retry":
        _record_failure(state, now_ts)
        return True
    return False


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def load_users():
    """Parse .users — one account per line as username:password[:email]."""
    users = {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                username, password, email = (line.split(":", 2) + ["", ""])[:3]
                users[username.strip()] = {
                    "password": password.strip(),
                    "email": email.strip(),
                }
    except FileNotFoundError:
        log.warning("users file %s not found", USERS_FILE)
    return users


def current_user():
    return session.get("user")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        users = load_users()
        account = users.get(username)
        if account and secrets.compare_digest(account["password"], password):
            session["user"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_ident(raw):
    return re.sub(r"\s+", "", (raw or "").upper())


def public_flight(flight):
    return {
        "id": flight["id"],
        "ident": flight["ident"],
        "date": flight["date"],
        "added_at": flight.get("added_at"),
        "last_fetched": flight.get("last_fetched"),
        "error": flight.get("error"),
        "final": bool(flight.get("final")),
        "refresh_requested": bool(flight.get("refresh_requested")),
        "data": flight.get("data"),
    }


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template("index.html", user=current_user())


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/api/flights")
@login_required
def api_flights():
    with STATE_LOCK:
        store = load_store()
        current = current_user()
        flights = [
            public_flight(f) for f in store["flights"] if f.get("owner") == current
        ]
        calls_today = _aero_calls_today(store)
    flights.sort(key=lambda f: (f["date"], (f.get("data") or {}).get("scheduled_out") or ""))
    return jsonify(
        {
            "flights": flights,
            "now": datetime.now(timezone.utc).isoformat(),
            "aero_configured": bool(providers.aero_key()),
            "airframes_configured": bool(providers.airframes_key()),
            "aero_calls_today": calls_today,
            "aero_max_daily": AERO_MAX_DAILY,
            "alerts_enabled": mailer.configured(),
            "alerts_to": (load_users().get(current_user()) or {}).get("email")
            or mailer.default_recipient(),
            "alerts_lead_minutes": ALERT_LEAD_MINUTES,
        }
    )


@app.route("/api/test-email", methods=["POST"])
@login_required
def api_test_email():
    if not mailer.configured():
        return (
            jsonify(
                {
                    "error": "SMTP is not configured — set MAIL_HOST, "
                    "MAIL_USERNAME and MAIL_PASSWORD in .env"
                }
            ),
            400,
        )
    recipient = (load_users().get(current_user()) or {}).get("email") or mailer.default_recipient()
    if not recipient:
        return jsonify({"error": "No recipient address configured"}), 400
    try:
        mailer.send(
            recipient,
            "[Flight Status] test email",
            "This is a test alert from your Flight Status tracker.\n"
            "If you can read this, email alerts are working.",
            "<p>This is a test alert from your Flight Status tracker.<br>"
            "If you can read this, email alerts are working.</p>",
        )
    except mailer.MailError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"ok": True, "to": recipient})


@app.route("/api/flights", methods=["POST"])
@login_required
def api_add_flight():
    payload = request.get_json(silent=True) or {}
    ident = normalize_ident(payload.get("ident"))
    date_str = (payload.get("date") or "").strip()

    if not IDENT_RE.match(ident) or not any(c.isdigit() for c in ident):
        return jsonify({"error": "Enter a flight number such as UA123 or UAL123"}), 400
    if not DATE_RE.match(date_str):
        return jsonify({"error": "Enter a valid date (YYYY-MM-DD)"}), 400
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Enter a valid date (YYYY-MM-DD)"}), 400

    with STATE_LOCK:
        store = load_store()
        for f in store["flights"]:
            if f["ident"] == ident and f["date"] == date_str:
                return jsonify({"error": "That flight is already in the list"}), 409
        flight = {
            "id": uuid.uuid4().hex,
            "ident": ident,
            "date": date_str,
            "owner": current_user(),
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_fetched": None,
            "last_fetched_ts": None,
            "last_attempt_ts": 0,
            "final": False,
            "refresh_requested": True,
            "data": None,
            "error": None,
        }

        # Verify the flight actually departs on that date before storing it,
        # so a typo is rejected immediately instead of showing a wrong flight.
        if providers.aero_key() and _aero_calls_today(store) < AERO_MAX_DAILY:
            _respect_rate_limit()
            calls = store.setdefault("aero_calls", {})
            calls[_today()] = calls.get(_today(), 0) + 1
            try:
                data = providers.lookup(ident, date_str)
            except providers.ProviderError as exc:
                return jsonify({"error": str(exc)}), 502
            if not data:
                return jsonify({"error": _not_found_message(ident, date_str)}), 404
            now_ts = time.time()
            flight["data"] = data
            flight["final"] = compute_final(data, now_ts)
            flight["last_fetched"] = datetime.now(timezone.utc).isoformat()
            flight["last_fetched_ts"] = now_ts
            flight["last_attempt_ts"] = now_ts
            flight["refresh_requested"] = False

        store["flights"].append(flight)
        save_store(store)
        result = public_flight(flight)
    return jsonify({"flight": result}), 201


@app.route("/api/flights/<flight_id>", methods=["DELETE"])
@login_required
def api_delete_flight(flight_id):
    with STATE_LOCK:
        store = load_store()
        before = len(store["flights"])
        current = current_user()
        store["flights"] = [
            f for f in store["flights"] if not (f["id"] == flight_id and f.get("owner") == current)
        ]
        if len(store["flights"]) == before:
            return jsonify({"error": "Flight not found"}), 404
        save_store(store)
    return jsonify({"ok": True})


@app.route("/api/flights/<flight_id>/refresh", methods=["POST"])
@login_required
def api_refresh_flight(flight_id):
    with STATE_LOCK:
        store = load_store()
        for f in store["flights"]:
            if f["id"] == flight_id:
                if f.get("owner") and f.get("owner") != current_user():
                    return jsonify({"error": "Not authorized"}), 403
                f["refresh_requested"] = True
                f["final"] = False
                save_store(store)
                return jsonify({"ok": True})
    return jsonify({"error": "Flight not found"}), 404


def _start_worker():
    thread = threading.Thread(target=_worker, name="refresh-worker", daemon=True)
    thread.start()
    log.info("refresh worker started (interval %ss)", WORKER_INTERVAL)


_start_worker()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
