"""Flight data providers.

Primary source:  FlightAware AeroAPI (https://www.flightaware.com/aeroapi)
Fallback source: airframes.io (https://airframes.io)

Each provider returns a normalised dict (see _empty_flight / _map_* helpers) so
the rest of the app does not care where the data came from.  All times are
returned as UTC ISO-8601 strings; the frontend converts them to the relevant
airport's local time zone using the `timezone` field on origin/destination.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

try:
    import airportsdata

    _AIRPORTS = {}
    _AIRPORTS.update(airportsdata.load("ICAO"))
    _AIRPORTS.update(airportsdata.load("IATA"))
except Exception:  # pragma: no cover - airport metadata is a nice-to-have
    _AIRPORTS = {}

log = logging.getLogger("flightstatus.providers")

AERO_BASE = "https://aeroapi.flightaware.com/aeroapi"
AIRFRAMES_BASE = "https://api.airframes.io/v1"
HTTP_TIMEOUT = 25

IDENT_RE = re.compile(r"^([A-Z]{2,3})\s*(\d{1,4})$")


class ProviderError(Exception):
    """Raised when an upstream call fails in a way worth surfacing."""


def aero_key():
    return (os.environ.get("AEROAPI_KEY") or "").strip()


def airframes_key():
    return (os.environ.get("AIRFRAMES_API_KEY") or "").strip()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def airport_info(code):
    """Return {code,name,city,timezone,iata,icao} for an airport code."""
    result = {
        "code": code or None,
        "name": None,
        "city": None,
        "timezone": None,
        "iata": None,
        "icao": None,
    }
    if not code:
        return result
    rec = _AIRPORTS.get(code.upper())
    if rec:
        result["name"] = rec.get("name")
        result["city"] = rec.get("city")
        result["timezone"] = rec.get("tz")
        result["iata"] = rec.get("iata")
        result["icao"] = rec.get("icao")
    return result


def _airport_ref(ref):
    """Normalise an AeroAPI origin/destination value.

    `/flights` returns an object with code/name/city/timezone, while
    `/schedules` returns a plain airport code string.
    """
    if not ref:
        return airport_info(None)
    if isinstance(ref, str):
        return airport_info(ref)
    code = ref.get("code") or ref.get("code_icao") or ref.get("code_iata")
    info = airport_info(code)
    if ref.get("name"):
        info["name"] = ref["name"]
    if ref.get("city"):
        info["city"] = ref["city"]
    if ref.get("timezone"):
        info["timezone"] = ref["timezone"]
    if ref.get("code_iata"):
        info["iata"] = ref["code_iata"]
    if ref.get("code_icao"):
        info["icao"] = ref["code_icao"]
    info["code"] = code or info["code"]
    return info


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _local_date(value, tz_name):
    dt = _parse_dt(value)
    if not dt:
        return None
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return dt.date().isoformat()


def _empty_flight(source):
    return {
        "ident": None,
        "ident_icao": None,
        "ident_iata": None,
        "fa_flight_id": None,
        "aircraft_type": None,
        "registration": None,
        "airline": None,
        "origin": airport_info(None),
        "destination": airport_info(None),
        "scheduled_out": None,
        "estimated_out": None,
        "actual_out": None,
        "scheduled_off": None,
        "estimated_off": None,
        "actual_off": None,
        "scheduled_in": None,
        "estimated_in": None,
        "actual_in": None,
        "scheduled_on": None,
        "estimated_on": None,
        "actual_on": None,
        "terminal_origin": None,
        "gate_origin": None,
        "terminal_destination": None,
        "gate_destination": None,
        "status": None,
        "cancelled": False,
        "diverted": False,
        "blocked": False,
        "progress_percent": None,
        "source": source,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# AeroAPI
# --------------------------------------------------------------------------- #
def _aero_401_message(resp):
    """Turn AeroAPI's 401 body into something actionable.

    AeroAPI returns HTTP 401 both for a bad key *and* for endpoints that are not
    part of your subscription tier, so the body needs to be inspected.
    """
    detail = ""
    try:
        detail = (resp.json() or {}).get("detail") or ""
    except ValueError:
        detail = resp.text[:200]
    if "Standard and Premium" in detail or "Historical data" in detail:
        return (
            "Your AeroAPI plan (Base) doesn't include this endpoint — "
            "historical data needs Standard/Premium."
        )
    if "Invalid API key" in detail:
        return "AeroAPI rejected the key (401 Invalid API key)"
    return f"AeroAPI rejected the key (401 Unauthorized){': ' + detail if detail else ''}"


def _aero_get(path, params):
    key = aero_key()
    if not key:
        raise ProviderError("AEROAPI_KEY is not configured")
    try:
        resp = requests.get(
            AERO_BASE + path,
            params=params,
            headers={"x-apikey": key, "Accept": "application/json; charset=UTF-8"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise ProviderError(f"AeroAPI request failed: {exc}") from exc

    if resp.status_code == 401:
        raise ProviderError(_aero_401_message(resp))
    if resp.status_code == 403:
        raise ProviderError("AeroAPI key lacks permission (403 Forbidden)")
    if resp.status_code == 429:
        raise ProviderError("AeroAPI rate limit reached (429)")
    if resp.status_code >= 400:
        raise ProviderError(f"AeroAPI error {resp.status_code}: {resp.text[:180]}")
    return resp.json()


def _map_fa_flight(fl):
    out = _empty_flight("aeroapi")
    out["ident"] = fl.get("ident")
    out["ident_icao"] = fl.get("ident_icao")
    out["ident_iata"] = fl.get("ident_iata")
    out["fa_flight_id"] = fl.get("fa_flight_id")
    out["aircraft_type"] = fl.get("aircraft_type")
    out["registration"] = fl.get("registration")
    out["origin"] = _airport_ref(fl.get("origin"))
    out["destination"] = _airport_ref(fl.get("destination"))
    for field in (
        "scheduled_out",
        "estimated_out",
        "actual_out",
        "scheduled_off",
        "estimated_off",
        "actual_off",
        "scheduled_in",
        "estimated_in",
        "actual_in",
        "scheduled_on",
        "estimated_on",
        "actual_on",
        "terminal_origin",
        "gate_origin",
        "terminal_destination",
        "gate_destination",
        "status",
        "progress_percent",
    ):
        out[field] = fl.get(field)
    out["cancelled"] = bool(fl.get("cancelled"))
    out["diverted"] = bool(fl.get("diverted"))
    out["blocked"] = bool(fl.get("blocked"))
    return out


def _map_schedule(s):
    out = _empty_flight("schedules")
    out["ident"] = s.get("ident")
    out["ident_icao"] = s.get("ident_icao")
    out["ident_iata"] = s.get("ident_iata")
    out["fa_flight_id"] = s.get("fa_flight_id")
    out["aircraft_type"] = s.get("aircraft_type")
    out["origin"] = airport_info(s.get("origin") or s.get("origin_icao") or s.get("origin_iata"))
    out["destination"] = airport_info(
        s.get("destination") or s.get("destination_icao") or s.get("destination_iata")
    )
    out["scheduled_out"] = s.get("scheduled_out")
    out["scheduled_in"] = s.get("scheduled_in")
    out["status"] = "Scheduled"
    return out


def _ident_matches(fl, ident):
    """True when any form of the flight's ident equals the requested one."""
    wanted = (ident or "").upper().replace(" ", "")
    if not wanted:
        return False
    for key in ("ident", "ident_icao", "ident_iata"):
        value = (fl.get(key) or "").upper().replace(" ", "")
        if value and value == wanted:
            return True
    return False


def _flight_date_matches(fl, date_str):
    """True when the flight departs on the requested local date.

    The date is compared in the *origin airport's* time zone, because that is
    how a passenger thinks about "the flight on the 16th" (e.g. a 21:15 EDT
    departure is still "the 16th", even though AeroAPI stores it as 01:15Z on
    the 17th).
    """
    tz_name = _airport_ref(fl.get("origin") or {}).get("timezone")
    local = _local_date(fl.get("scheduled_out"), tz_name)
    if local is not None:
        return local == date_str
    return (fl.get("scheduled_out") or "")[:10] == date_str


def _pick_flight(flights, ident, date_str):
    """Pick the flight departing on `date_str`, or None if there isn't one.

    AeroAPI can return several legs/codeshares for one search, so a naive
    "first result" pick can return the wrong flight (e.g. a JetBlue codeshare
    of an Aer Lingus flight).  Flights on other dates are discarded rather than
    substituted, so the caller can report "not found" instead of showing a
    different day's flight.
    """
    matches = [fl for fl in (flights or []) if _flight_date_matches(fl, date_str)]
    if not matches:
        return None

    def sort_key(fl):
        codeshare = 1 if fl.get("actual_ident") else 0
        return (0 if _ident_matches(fl, ident) else 1, codeshare,
                fl.get("scheduled_out") or "")

    return sorted(matches, key=sort_key)[0]


def _date_window(date_str, back=1, forward=2):
    """A [start, end) UTC date window around a local departure date.

    AeroAPI's /flights and /schedules windows are keyed on the UTC date, but the
    user picks the *local* date of departure. An evening departure in the
    Americas lands on the next UTC day (e.g. BOS 21:15 EDT == 01:15Z the
    following day), so widen by a day either side and let _pick_flight choose
    the local-date match.
    """
    target = datetime.strptime(date_str, "%Y-%m-%d")
    start = (target - timedelta(days=back)).date().isoformat()
    end = (target + timedelta(days=forward)).date().isoformat()
    return start, end


def _aero_by_ident(ident, date_str):
    """Look up a single dated flight via /flights/{ident} or /history/flights/{ident}."""
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    history = target.date() < (now - timedelta(days=10)).date()

    start_dt = target - timedelta(days=1)
    end_dt = target + timedelta(days=2)
    if not history:
        # /flights rejects bounds beyond -10 days / +2 days from now.
        start_dt = max(start_dt, now - timedelta(days=10))
        end_dt = min(end_dt, now + timedelta(days=2))

    path = f"/history/flights/{ident}" if history else f"/flights/{ident}"
    params = {
        "start": start_dt.date().isoformat(),
        "end": end_dt.date().isoformat(),
        "max_pages": 1,
    }
    data = _aero_get(path, params)
    chosen = _pick_flight(data.get("flights") or [], ident, date_str)
    return _map_fa_flight(chosen) if chosen else None


def _aero_by_schedule(ident, date_str):
    """Future flights: use the airline schedule endpoint (no terminals/gates)."""
    match = IDENT_RE.match(ident.upper().replace(" ", ""))
    if not match:
        return None
    airline, number = match.group(1), int(match.group(2))
    start, end = _date_window(date_str)
    data = _aero_get(
        f"/schedules/{start}/{end}",
        {"airline": airline, "flight_number": number, "max_pages": 1},
    )
    scheduled = data.get("scheduled") or []
    chosen = _pick_flight(scheduled, ident, date_str)
    return _map_schedule(chosen) if chosen else None


def aero_lookup(ident, date_str):
    """Return a normalised flight dict from AeroAPI, or None if not found."""
    target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if target.date() > (now + timedelta(days=2)).date():
        return _aero_by_schedule(ident, date_str)
    flight = _aero_by_ident(ident, date_str)
    if flight is None and target.date() >= now.date():
        # Near the +2 day edge an evening departure can fall outside /flights.
        flight = _aero_by_schedule(ident, date_str)
    return flight


# --------------------------------------------------------------------------- #
# airframes.io (fallback)
# --------------------------------------------------------------------------- #
def _airframes_get(path, params=None):
    headers = {"Accept": "application/json"}
    key = airframes_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = requests.get(
        AIRFRAMES_BASE + path, params=params, headers=headers, timeout=HTTP_TIMEOUT
    )
    if resp.status_code >= 400:
        raise ProviderError(f"airframes.io error {resp.status_code}")
    return resp.json()


def _map_airframes(f):
    out = _empty_flight("airframes")
    out["ident"] = f.get("flight") or f.get("flightIata") or f.get("flightIcao")
    out["ident_icao"] = f.get("flightIcao")
    out["ident_iata"] = f.get("flightIata")
    airframe = f.get("airframe") or {}
    out["registration"] = airframe.get("tail")
    out["aircraft_type"] = airframe.get("icaoType")
    airline = f.get("airline") or {}
    out["airline"] = airline.get("name")
    out["origin"] = airport_info(f.get("departingAirport"))
    out["destination"] = airport_info(f.get("destinationAirport"))
    out["scheduled_out"] = f.get("departureTimeScheduled")
    out["actual_out"] = f.get("departureTimeActual")
    out["gate_origin"] = f.get("departureGateActual") or f.get("departureGateScheduled")
    out["scheduled_in"] = f.get("arrivalTimeScheduled")
    out["actual_in"] = f.get("arrivalTimeActual")
    out["gate_destination"] = f.get("arrivalGateActual") or f.get("arrivalGateScheduled")
    out["status"] = f.get("status")
    return out


def airframes_lookup(ident, date_str):
    """Best-effort fallback: scan active airframes.io flights for the callsign."""
    ident = ident.upper().replace(" ", "")
    try:
        items = _airframes_get("/flights", {"limit": 100})
    except (ProviderError, requests.RequestException, ValueError) as exc:
        log.debug("airframes.io flights lookup failed: %s", exc)
        return None
    if not isinstance(items, list):
        return None
    match = next(
        (x for x in items if (x.get("flight") or "").upper().replace(" ", "") == ident),
        None,
    )
    if not match:
        return None
    try:
        detail = _airframes_get(f"/flights/{match['id']}")
    except (ProviderError, requests.RequestException, ValueError):
        detail = match
    return _map_airframes(detail if isinstance(detail, dict) else match)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def lookup(ident, date_str):
    """Query AeroAPI first, then fall back to airframes.io.

    Returns a normalised dict, or None if neither source knows the flight.
    Raises ProviderError only when AeroAPI itself failed (so the caller can
    surface configuration/rate-limit problems instead of silently showing
    "no data").
    """
    aero_error = None
    if aero_key():
        try:
            flight = aero_lookup(ident, date_str)
            if flight:
                return flight
            log.info("AeroAPI found no flight for %s on %s", ident, date_str)
        except ProviderError as exc:
            aero_error = exc
            log.warning("AeroAPI lookup failed for %s: %s", ident, exc)
    else:
        log.warning("AEROAPI_KEY not set, skipping AeroAPI")

    fallback = airframes_lookup(ident, date_str)
    if fallback:
        return fallback
    if aero_error:
        raise aero_error
    return None
