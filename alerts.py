"""Detect meaningful changes in a flight and render the alert emails.

A change is deliberately only emailed when it matters: gate/terminal moves,
status flips, cancellations/diversions, and times that shift by more than a
few minutes. Reminders ("leaving soon") are handled separately.
"""

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# (field, human label, side)
TIME_FIELDS = (
    ("scheduled_out", "Scheduled departure", "origin"),
    ("estimated_out", "Estimated departure", "origin"),
    ("actual_out", "Actual departure", "origin"),
    ("scheduled_in", "Scheduled arrival", "destination"),
    ("estimated_in", "Estimated arrival", "destination"),
    ("actual_in", "Actual arrival", "destination"),
)

# (field, human label)
GATE_FIELDS = (
    ("terminal_origin", "Departure terminal"),
    ("gate_origin", "Departure gate"),
    ("terminal_destination", "Arrival terminal"),
    ("gate_destination", "Arrival gate"),
)

SNAPSHOT_FIELDS = ("status", "cancelled", "diverted") + tuple(
    f for f, _, _ in TIME_FIELDS
) + tuple(f for f, _ in GATE_FIELDS)

# Which change to lead the subject line with: a diversion or reroute first,
# then status, cancellations and diversions, then times, and gate/terminal
# moves last.
SUBJECT_ORDER = (
    ("Diverted", "Destination", "Landed", "Arrival", "Departure", "Status", "Cancellation")
    + tuple(label for _, label, _ in TIME_FIELDS)
    + tuple(label for _, label in GATE_FIELDS)
)


def _simplify_subject(change, data):
    """Simplify change to a short subject phrase."""
    data = data or {}
    
    # Diversion
    if change.startswith("Diverted"):
        return "🔴 Diverted"
    
    label, sep, rest = change.partition(":")
    if not sep:
        return change
    
    old, arrow, new = rest.strip().partition(" → ")
    
    # Departure/arrival times
    if label == "Actual departure" or label == "Estimated departure" or label == "Scheduled departure":
        if "Actual departure" in change or data.get("actual_out"):
            actual = _parse(data.get("actual_out"))
            sched = _parse(data.get("scheduled_out"))
            if actual and sched:
                delta_min = (actual - sched).total_seconds() / 60
                if delta_min <= 2:
                    return "🟢 On-Time Departure"
                elif delta_min < 0:
                    return "🟢 Early Departure"
                else:
                    return "🟡 Late Departure"
            return "🟡 Departure"
        elif arrow and old and new:
            if "earlier" in new:
                return "🟢 Early Departure"
            elif "later" in new:
                return "🟡 Late Departure"
            else:
                return "🟡 Departure"
        return "🟡 Departure"
    
    if label == "Actual arrival" or label == "Estimated arrival" or label == "Scheduled arrival":
        if "Actual arrival" in change or data.get("actual_in"):
            actual = _parse(data.get("actual_in"))
            sched = _parse(data.get("scheduled_in"))
            if actual and sched:
                delta_min = (actual - sched).total_seconds() / 60
                if delta_min <= 2:
                    return "🟢 On-Time Arrival"
                elif delta_min < 0:
                    return "🟢 Early Arrival"
                else:
                    return "🟡 Late Arrival"
            return "🟢 Landed"
        elif arrow:
            if "earlier" in new:
                return "🟢 Early Arrival"
            elif "later" in new:
                return "🟡 Late Arrival"
            else:
                return "🟡 Arrival"
        return "🟡 Arrival"
    
    # Status-based
    if label == "Status":
        status_lower = new.lower() if new else old.lower()
        old_lower = old.lower() if old else ""
        if "landed" in status_lower or "arrived" in status_lower:
            actual = _parse(data.get("actual_in"))
            sched = _parse(data.get("scheduled_in"))
            if actual and sched:
                delta_min = (actual - sched).total_seconds() / 60
                if delta_min <= 2:
                    return "🟢 On-Time Arrival"
                elif delta_min < 0:
                    return "🟢 Early Arrival"
                else:
                    return "🟡 Late Arrival"
            return "🟢 Landed"
        if "depart" in status_lower and "landed" not in status_lower and "arriv" not in status_lower:
            actual = _parse(data.get("actual_out"))
            sched = _parse(data.get("scheduled_out"))
            if actual and sched:
                delta_min = (actual - sched).total_seconds() / 60
                if delta_min <= 2:
                    return "🟢 On-Time Departure"
                elif delta_min < 0:
                    return "🟢 Early Departure"
                else:
                    return "🟡 Late Departure"
            return "🟢 Departure"
        if any(x in status_lower for x in ['en route', 'airborne', 'in flight', 'taxiing', 'left gate']):
            if 'delay' in status_lower or 'late' in status_lower:
                return "🟡 En Route / Delayed"
            return "🟢 En Route / On Time"
        if 'delay' in status_lower or 'late' in status_lower:
            return "🟡 Delayed"
        if old_lower == 'scheduled' and status_lower and 'cancel' not in status_lower and 'divert' not in status_lower:
            return "🟢 Status Update"
    
    # Cancellation
    if label == "Cancellation":
        if new and "yes" in new.lower():
            return "🔴 Cancelled"
        return change
    
    # Gate/terminal assignments - neutral/positive
    if arrow and old == "not assigned" and new and new != "not assigned":
        return f"🟢 {label} {new}"
    if arrow and old and new:
        if label.lower().startswith('gate') or label.lower().startswith('terminal'):
            if old == 'not assigned' or not old or old.strip() == '':
                return f"🟢 {label} Assigned"
            return f"🟡 {label} Changed"  # Moving between gates is inconvenient
        if 'delay' in new.lower() or 'late' in new.lower() or 'cancel' in new.lower() or 'divert' in new.lower():
            return f"🟡 {label} Changed"
        return f"🟢 {label} Changed"
    
    return change

# Ignore estimated-time jitter below this (minutes).
TIME_CHANGE_MINUTES = 5
LATE_THRESHOLD_MINUTES = 15


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _tz_of(data, side):
    ref = data.get(side) if isinstance(data.get(side), dict) else {}
    return (ref or {}).get("timezone")


def _fmt_time(value, tz_name):
    dt = _parse(value)
    if not dt:
        return "—"
    if tz_name:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    stamp = dt.strftime("%b %d, %H:%M")
    return f"{stamp} {dt.tzname()}" if dt.tzname() else stamp


def airport_code(ref):
    """Shortest stable code for an airport ref (IATA, else ICAO)."""
    if not isinstance(ref, dict):
        return ""
    return (ref.get("iata") or ref.get("code") or ref.get("icao") or "").upper()


def route(data):
    return (airport_code(data.get("origin")), airport_code(data.get("destination")))


def snapshot(data):
    data = data or {}
    snap = {key: data.get(key) for key in SNAPSHOT_FIELDS}
    # Nested airport objects are flattened to a code: a changed arrival airport
    # is how a diversion shows up when the upstream `diverted` flag stays false.
    snap["destination"] = airport_code(data.get("destination"))
    return snap


def build_changes(prev, cur, data):
    """Return a list of human-readable change strings (empty when nothing matters)."""
    data = data or {}
    tz_out = _tz_of(data, "origin")
    tz_in = _tz_of(data, "destination")
    changes = []

    # A diversion is usually published as a new destination airport (AeroAPI's
    # `diverted` flag can stay false), so compare the flattened airport codes.
    # Only alert once the flight has left: before departure this is just a
    # schedule change.
    old_dest = (prev.get("destination") or "").strip()
    new_dest = (cur.get("destination") or "").strip()
    dest_changed = bool(old_dest and new_dest and old_dest != new_dest)
    if dest_changed:
        if data.get("actual_out") or data.get("diverted"):
            changes.append(f"Diverted to {new_dest} (was {old_dest})")
        else:
            changes.append(f"Destination: {old_dest} → {new_dest}")

    old_status = prev.get("status") or ""
    new_status = cur.get("status") or ""
    if old_status != new_status:
        changes.append(f"Status: {old_status or 'unknown'} → {new_status or 'unknown'}")

    for field, label in GATE_FIELDS:
        old = (prev.get(field) or "").strip()
        new = (cur.get(field) or "").strip()
        if old.lower() == new.lower():
            continue
        changes.append(
            f"{label}: {old if old and old.lower() != 'none' else 'not assigned'} → "
            f"{new if new and new.lower() != 'none' else 'not assigned'}"
        )

    for field, label, side in TIME_FIELDS:
        old, new = prev.get(field), cur.get(field)
        if old == new:
            continue
        tz = tz_out if side == "origin" else tz_in
        if old and new:
            old_dt, new_dt = _parse(old), _parse(new)
            if old_dt and new_dt:
                delta = abs((new_dt - old_dt).total_seconds())
                if delta < TIME_CHANGE_MINUTES * 60:
                    continue
                minutes = int(round(delta / 60))
                pretty = f"{minutes} min" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"
                changes.append(
                    f"{label}: {_fmt_time(old, tz)} → {_fmt_time(new, tz)} ({pretty} later)"
                    if new_dt > old_dt
                    else f"{label}: {_fmt_time(old, tz)} → {_fmt_time(new, tz)} ({pretty} earlier)"
                )
                continue
        if not old and not new:
            continue
        changes.append(
            f"{label}: {'not available' if not old else _fmt_time(old, tz)} → "
            f"{'not available' if not new else _fmt_time(new, tz)}"
        )

    if bool(prev.get("cancelled")) != bool(cur.get("cancelled")):
        changes.append("Cancellation: yes" if cur.get("cancelled") else "Cancellation: no")
    if not dest_changed and bool(prev.get("diverted")) != bool(cur.get("diverted")):
        changes.append("Diverted: yes" if cur.get("diverted") else "Diverted: no")

    return changes


def late_minutes(data):
    dep = _parse(data.get("estimated_out") or data.get("actual_out"))
    sched = _parse(data.get("scheduled_out"))
    if not dep or not sched:
        return 0
    minutes = int(round((dep - sched).total_seconds() / 60))
    return minutes if minutes >= LATE_THRESHOLD_MINUTES else 0


def _ident(data, fallback):
    return data.get("ident") or fallback or ""


def _base_lines(data, flight):
    origin = (data.get("origin") or {}) if isinstance(data.get("origin"), dict) else {}
    dest = (
        data.get("destination") or {}
        if isinstance(data.get("destination"), dict)
        else {}
    )
    dep_name = origin.get("name") or origin.get("code") or "origin"
    arr_name = dest.get("name") or dest.get("code") or "destination"
    tz_out, tz_in = _tz_of(data, "origin"), _tz_of(data, "destination")
    origin_txt = f"{dep_name} ({_fmt_time(data.get('estimated_out') or data.get('scheduled_out'), tz_out)})"
    dest_txt = f"{arr_name} ({_fmt_time(data.get('estimated_in') or data.get('scheduled_in'), tz_in)})"
    gate_txt = (
        f"Terminal {_flat(data.get('terminal_origin'))} / Gate {_flat(data.get('gate_origin'))}"
        f" → Terminal {_flat(data.get('terminal_destination'))} / Gate {_flat(data.get('gate_destination'))}"
    )
    return [
        ("Flight", f"{_ident(data, flight.get('ident'))} on {flight.get('date')}"),
        ("Route", f"{origin_txt} → {dest_txt}"),
        ("Gates", gate_txt),
        ("Status", data.get("status") or "unknown"),
        ("Source", data.get("source") or "unknown"),
    ]


def _flat(value):
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return "—"
    return text


def _subject_phrase(change):
    """Phrase a change for a subject line.

    A terminal/gate that was published for the first time arrives as
    "Arrival terminal: not assigned → E". Leading a subject with the empty side
    reads as though the terminal is missing, so first assignments become
    "Arrival terminal E"; real reassignments keep the before → after form.
    """
    label, sep, rest = change.partition(":")
    if not sep:
        return change
    old, arrow, new = rest.strip().partition(" → ")
    if arrow and old == "not assigned" and new and new != "not assigned":
        return f"{label} {new}"
    return change


def _subject_change(changes):
    """Pick the most newsworthy change, phrased for the subject line."""
    for label in SUBJECT_ORDER:
        for change in changes:
            if change == label or change.startswith((label + ":", label + " ")):
                return _subject_phrase(change)
    return changes[0]


def render_changes(flight, data, changes):
    ident = _ident(data, flight.get("ident"))
    dep, arr = route(data)
    route_txt = f"{dep}→{arr}" if dep and arr else ""
    head = _simplify_subject(_subject_change(changes), data)
    subject = f"[Flight Status] {ident} {route_txt}: {head}".strip()
    subject = re.sub(r"\s+", " ", subject)

    lines = [f"{ident} ({flight.get('date')}) — change detected", ""]
    lines += [f"  • {c}" for c in changes]
    late = late_minutes(data)
    if late and not any(c.startswith("Estimated departure") or c.startswith("Actual departure") for c in changes):
        lines.append(f"  • Running about {late} min late")
    lines += [""] + [f"{label}: {value}" for label, value in _base_lines(data, flight)]
    lines += ["", "Open Flight Status to see live details."]

    text = "\n".join(lines)
    html_body = _render_html(subject, [("Change", c) for c in changes], data, flight)
    return subject, text, html_body


def render_reminder(flight, data, dep_value, now_ts):
    ident = _ident(data, flight.get("ident"))
    dep, arr = route(data)
    tz = _tz_of(data, "origin")
    dep_local = _fmt_time(dep_value, tz)

    dep_dt = _parse(dep_value)
    minutes = None
    if dep_dt:
        minutes = int(round((dep_dt.timestamp() - now_ts) / 60))
    lead_txt = _humanize_minutes(minutes) if minutes is not None else "soon"

    subject = f"[Flight Status] {ident} departs in {lead_txt} ({dep_local})"

    lines = [
        f"{ident} ({flight.get('date')}) is scheduled to depart in about "
        f"{lead_txt} — {dep_local}.",
        "",
    ]
    gate = _flat(data.get("gate_origin"))
    terminal = _flat(data.get("terminal_origin"))
    lines.append(f"Departing: {terminal} / {gate}")
    lines.append(f"Status: {data.get('status') or 'unknown'}")
    lines += [""] + [f"{label}: {value}" for label, value in _base_lines(data, flight)]
    lines += ["", "Open Flight Status to see live details."]

    text = "\n".join(lines)
    html_body = _render_html(
        subject,
        [("Reminder", f"Departs in about {lead_txt} ({dep_local})")],
        data,
        flight,
    )
    return subject, text, html_body


def _humanize_minutes(minutes):
    if minutes is None:
        return "soon"
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} hour" + ("s" if hours != 1 else "")
    return f"{hours}h {mins:02d}m"


def _render_html(title, bullets, data, flight):
    esc = html.escape
    rows = "".join(
        f'<tr><td style="padding:4px 10px 4px 0;color:#666;white-space:nowrap">'
        f"{esc(label)}:</td><td style='padding:4px 0'>{esc(str(value))}</td></tr>"
        for label, value in _base_lines(data, flight)
    )
    items = "".join(
        f'<li style="margin:4px 0">{esc(text)}</li>' for _, text in bullets
    )
    inner = (
        f'<ul style="margin:0 0 16px;padding-left:18px">{items}</ul>'
        f'<table style="border-collapse:collapse;font-size:13px">{rows}</table>'
    )
    return _shell(title, inner)


def render_notice(subject, paragraphs, bullets=None, footer=None):
    """A generic plain notice (used for account/system messages)."""
    esc = html.escape
    text_lines = list(paragraphs)
    if bullets:
        text_lines += [""] + [f"  • {b}" for b in bullets]
    if footer:
        text_lines += ["", footer]
    inner = ""
    if bullets:
        items = "".join(f'<li style="margin:4px 0">{esc(b)}</li>' for b in bullets)
        inner += f'<ul style="margin:0 0 16px;padding-left:18px">{items}</ul>'
    if footer:
        inner += f'<p style="margin:0;color:#6b7280;font-size:12px">{esc(footer)}</p>'
    return subject, "\n".join(text_lines), _shell(subject, inner)


def _shell(heading, inner):
    esc = html.escape
    return f"""<!doctype html>
<html><body style="font-family:Helvetica,Arial,sans-serif;font-size:14px;color:#111;
                 margin:0;padding:16px;background:#f6f7f9">
<div style="max-width:560px;margin:0 auto;background:#fff;border:1px solid #e3e5e8;
            border-radius:8px;padding:18px">
  <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#6b7280;
             margin-bottom:6px">Flight Status</div>
  <div style="font-size:17px;font-weight:600;margin-bottom:14px">{esc(heading)}</div>
  {inner}
  <div style="margin-top:16px;font-size:12px;color:#6b7280">
    Sent with love by Dennis and the Plex Flight Status tracker.
  </div>
</div></body></html>"""
