"""Callouts and member responses."""
from __future__ import annotations

import logging
import threading
import time

from . import hooks
from .db import transaction
from datetime import timedelta

from .timeutil import from_iso, plus_hours, to_iso, utcnow
from .units import UnitError, parse_coords

log = logging.getLogger("calloutroster.callouts")

# In flow order. A member with no response row is shown as "not_contacted": nobody has
# reached them yet. "no_reply" is for a lead who phoned and got no answer.
# "acknowledged" = saw the callout; says nothing about movement yet.
STATUSES = {
    "not_contacted": "Not contacted",
    "no_reply": "No reply",
    "acknowledged": "Acknowledged",
    "en_route": "En route",
    "on_scene": "On scene",
    "cleared": "Cleared",
    "unavailable": "Unavailable",
}
STATUS_TONES = {"not_contacted": "muted", "no_reply": "muted", "acknowledged": "warn", "en_route": "accent", "on_scene": "green", "cleared": "muted", "unavailable": "danger"}
# What a member can set on their own line; the first two statuses are for leads marking others.
SELF_STATUSES = ("acknowledged", "en_route", "on_scene", "cleared", "unavailable")
DEFAULT_STATUS = "not_contacted"
# Statuses that count as "coming" for the team tallies and the TAK marker.
COMING = ("acknowledged", "en_route", "on_scene")
# While a member holds one of these on an open callout they are spoken for: no other
# callout may set their status until they are cleared (or unavailable) on the first.
COMMITTED = COMING
ETA_CHOICES = (90, 60, 45, 30, 15, 5)
CONFIRMATIONS = {
    "not_contacted": "Marked not contacted.",
    "no_reply": "Marked no reply.",
    "acknowledged": "Acknowledged. Tap En route when you start moving.",
    "en_route": "You are en route.",
    "on_scene": "Marked on scene.",
    "cleared": "Marked cleared.",
    "unavailable": "Marked unavailable.",
}
MAX_LABEL = 60
MAX_BRIEF = 4000


class CalloutError(ValueError):
    pass


def open_callouts(db, unit_id: int):
    return db.execute(
        "SELECT * FROM callouts WHERE unit_id = ? AND closed_at IS NULL ORDER BY opened_at DESC, id DESC", (unit_id,)
    ).fetchall()


def get_callout(db, unit_id: int, callout_id: int):
    return db.execute("SELECT * FROM callouts WHERE unit_id = ? AND id = ?", (unit_id, callout_id)).fetchone()


def list_history(db, unit_id: int, limit: int = 100):
    return db.execute(
        "SELECT * FROM callouts WHERE unit_id = ? ORDER BY (closed_at IS NOT NULL), opened_at DESC, id DESC LIMIT ?",
        (unit_id, limit),
    ).fetchall()


def responses_map(db, callout_id: int) -> dict:
    rows = db.execute("SELECT * FROM responses WHERE callout_id = ?", (callout_id,)).fetchall()
    return {r["roster_id"]: r for r in rows}


def team_counts(db, callout_id: int) -> dict:
    """{'teams': {team_id|None: {'name', 'active', status: n...}}, 'total': {...}}"""
    callout = db.execute("SELECT unit_id FROM callouts WHERE id = ?", (callout_id,)).fetchone()
    if callout is None:
        return {"teams": {}, "total": _zero("All", 0)}
    rows = db.execute(
        """
        SELECT r.team_id, t.name AS team_name, t.sort_order AS team_order, resp.status
        FROM roster r
        LEFT JOIN teams t ON t.id = r.team_id
        LEFT JOIN responses resp ON resp.roster_id = r.id AND resp.callout_id = ?
        WHERE r.unit_id = ? AND r.active = 1 AND r.person_id IS NOT NULL
        ORDER BY (t.sort_order IS NULL), t.sort_order
        """,
        (callout_id, callout["unit_id"]),
    ).fetchall()
    teams: dict = {}
    total = _zero("All", 0)
    for r in rows:
        key = r["team_id"]
        bucket = teams.setdefault(key, _zero(r["team_name"] or "Unassigned", 0))
        bucket["active"] += 1
        total["active"] += 1
        if r["status"] in STATUSES:
            bucket[r["status"]] += 1
            total[r["status"]] += 1
    for b in [total, *teams.values()]:
        b["coming"] = sum(b[key] for key in COMING)
        # No response row at all counts as not contacted, the same as an explicit mark.
        b["not_contacted"] += b["active"] - sum(b[key] for key in STATUSES)
    return {"teams": teams, "total": total}


def version(db, unit_id: int, callout_id: int | None = None) -> int:
    """The id of the latest event for a callout (or the whole unit): phones poll this
    and only re-fetch the page when it moves. Every change records an event."""
    if callout_id is None:
        row = db.execute("SELECT MAX(id) AS v FROM events WHERE unit_id = ?", (unit_id,)).fetchone()
    else:
        row = db.execute("SELECT MAX(id) AS v FROM events WHERE callout_id = ?", (callout_id,)).fetchone()
    return row["v"] or 0


def _zero(name, active):
    return {"name": name, "active": active, "coming": 0, **{key: 0 for key in STATUSES}}


def clean_brief(brief: str) -> str:
    brief = (brief or "").replace("\r\n", "\n").strip()
    if len(brief) > MAX_BRIEF:
        raise CalloutError(f"Keep the brief under {MAX_BRIEF} characters.")
    return brief


def open_callout(db, cfg, unit, actor: str, label: str, coords=None, location_label: str = "",
                 brief: str = ""):
    label = " ".join((label or "").split())
    brief = clean_brief(brief)
    if not label:
        raise CalloutError("Give the callout a short label, e.g. 'Barricade Elm St'.")
    if len(label) > MAX_LABEL:
        raise CalloutError(f"Keep the label under {MAX_LABEL} characters.")
    try:
        lat, lon = parse_coords(coords)
    except UnitError as exc:
        raise CalloutError(str(exc)) from exc
    now = utcnow()
    with transaction(db):
        n_open = db.execute(
            "SELECT COUNT(*) FROM callouts WHERE unit_id = ? AND closed_at IS NULL", (unit["id"],)
        ).fetchone()[0]
        if n_open >= cfg.callout_max_open:
            raise CalloutError(
                f"{cfg.callout_max_open} callouts are already open. Close one before opening another."
            )
        cur = db.execute(
            "INSERT INTO callouts (unit_id, label, brief, opened_by, opened_at, expires_at, lat, lon, location_label)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (unit["id"], label, brief, actor, to_iso(now), plus_hours(to_iso(now), cfg.callout_duration_hours),
             lat, lon, (location_label or "").strip() or None),
        )
        callout = get_callout(db, unit["id"], cur.lastrowid)
        hooks.tak_hook(db, hooks.EVENT_OPENED, unit, callout, team_counts(db, callout["id"]), actor)
    return callout


def close_callout(db, unit, callout, actor: str, reason: str = "manual"):
    if callout["closed_at"]:
        raise CalloutError("That callout is already closed.")
    with transaction(db):
        db.execute(
            "UPDATE callouts SET closed_at = ?, closed_by = ?, close_reason = ? WHERE id = ? AND closed_at IS NULL",
            (to_iso(utcnow()), actor, reason, callout["id"]),
        )
        callout = get_callout(db, unit["id"], callout["id"])
        hooks.tak_hook(db, hooks.EVENT_CLOSED, unit, callout, team_counts(db, callout["id"]), actor,
                       {"reason": reason})
    return callout


def extend_callout(db, cfg, unit, callout, actor: str):
    if callout["closed_at"]:
        raise CalloutError("A closed callout cannot be extended.")
    # Extend from the later of "now" and the current expiry, so a callout that
    # is seconds from expiring still gets a full step.
    base = max(from_iso(callout["expires_at"]), utcnow())
    new_expiry = plus_hours(to_iso(base), cfg.callout_extend_hours)
    with transaction(db):
        db.execute("UPDATE callouts SET expires_at = ? WHERE id = ?", (new_expiry, callout["id"]))
        callout = get_callout(db, unit["id"], callout["id"])
        hooks.tak_hook(db, hooks.EVENT_EXTENDED, unit, callout, team_counts(db, callout["id"]), actor,
                       {"extended_hours": cfg.callout_extend_hours})
    return callout


def set_location(db, unit, callout, actor: str, coords, location_label: str = ""):
    if callout["closed_at"]:
        raise CalloutError("A closed callout cannot be moved.")
    try:
        lat, lon = parse_coords(coords)
    except UnitError as exc:
        raise CalloutError(str(exc)) from exc
    with transaction(db):
        db.execute(
            "UPDATE callouts SET lat = ?, lon = ?, location_label = ? WHERE id = ?",
            (lat, lon, (location_label or "").strip() or None, callout["id"]),
        )
        callout = get_callout(db, unit["id"], callout["id"])
        hooks.tak_hook(db, hooks.EVENT_LOCATION, unit, callout, team_counts(db, callout["id"]), actor)
    return callout


def set_brief(db, unit, callout, actor: str, brief: str):
    if callout["closed_at"]:
        raise CalloutError("A closed callout cannot be edited.")
    brief = clean_brief(brief)
    with transaction(db):
        db.execute("UPDATE callouts SET brief = ? WHERE id = ?", (brief, callout["id"]))
        callout = get_callout(db, unit["id"], callout["id"])
        hooks.tak_hook(db, hooks.EVENT_BRIEF, unit, callout, team_counts(db, callout["id"]), actor)
    return callout


def committed_elsewhere(db, unit_id: int, callout_id: int) -> dict:
    """{roster_id: {'callout_id', 'label', 'status'}} for members who are acknowledged,
    en route or on scene on a different open callout of this unit."""
    rows = db.execute(
        """
        SELECT resp.roster_id, c.id AS callout_id, c.label, resp.status
        FROM responses resp JOIN callouts c ON c.id = resp.callout_id
        WHERE c.unit_id = ? AND c.closed_at IS NULL AND c.id != ? AND resp.status IN (?, ?, ?)
        ORDER BY resp.last_at DESC
        """,
        (unit_id, callout_id, *COMMITTED),
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["roster_id"], {"callout_id": r["callout_id"], "label": r["label"], "status": r["status"]})
    return out


def respond(db, unit, callout, roster_row, status: str, actor: str):
    """One tap, by the member or by a lead on their behalf.

    A later tap replaces the status; first and last tap times are kept, and
    ``set_by`` records who did it so a lead's phone-list marks are visible.
    A member committed to another open callout cannot be set here at all.
    """
    if status not in STATUSES:
        raise CalloutError("Unknown status.")
    if callout["closed_at"]:
        raise CalloutError("That callout has closed.")
    other = committed_elsewhere(db, unit["id"], callout["id"]).get(roster_row["id"])
    if other:
        raise CalloutError(f"{roster_row['position']} is {STATUSES[other['status']].lower()} at "
                           f"\u201c{other['label']}\u201d. Clear them there first.")
    now = to_iso(utcnow())
    with transaction(db):
        # An ETA only makes sense while en route; any other status clears it.
        db.execute(
            """
            INSERT INTO responses (callout_id, roster_id, status, first_at, last_at, set_by) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (callout_id, roster_id) DO UPDATE SET
                status = excluded.status, last_at = excluded.last_at, set_by = excluded.set_by,
                eta_at = CASE WHEN excluded.status = 'en_route' THEN eta_at ELSE NULL END
            """,
            (callout["id"], roster_row["id"], status, now, now, actor),
        )
        hooks.tak_hook(db, hooks.EVENT_RESPONSE, unit, callout, team_counts(db, callout["id"]), actor,
                       {"response": {"roster_id": roster_row["id"], "position": roster_row["position"],
                                     "team": roster_row["team_name"], "status": status,
                                     "set_by": actor, "self": actor.lower() == (roster_row["tak_username"] or "").lower()}})


def set_eta(db, unit, callout, roster_row, minutes, actor: str):
    """Record (or clear, with minutes=0) an ETA for someone who is en route."""
    if callout["closed_at"]:
        raise CalloutError("That callout has closed.")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError) as exc:
        raise CalloutError("Pick one of the ETA options.") from exc
    if minutes != 0 and minutes not in ETA_CHOICES:
        raise CalloutError("Pick one of the ETA options.")
    resp = db.execute("SELECT * FROM responses WHERE callout_id = ? AND roster_id = ?",
                      (callout["id"], roster_row["id"])).fetchone()
    if resp is None or resp["status"] != "en_route":
        raise CalloutError("Set your status to En route first.")
    eta_at = to_iso(utcnow() + timedelta(minutes=minutes)) if minutes else None
    with transaction(db):
        db.execute("UPDATE responses SET eta_at = ? WHERE id = ?", (eta_at, resp["id"]))
        hooks.tak_hook(db, hooks.EVENT_RESPONSE, unit, callout, team_counts(db, callout["id"]), actor,
                       {"response": {"roster_id": roster_row["id"], "position": roster_row["position"],
                                     "team": roster_row["team_name"], "status": "en_route", "eta_at": eta_at}})
    return eta_at


# ---- expiry ---------------------------------------------------------------------

def expire_due(db) -> int:
    """Close every callout whose expiry has passed. Safe to call from any thread."""
    now = to_iso(utcnow())
    due = db.execute(
        "SELECT c.*, u.id AS u_id FROM callouts c JOIN units u ON u.id = c.unit_id"
        " WHERE c.closed_at IS NULL AND c.expires_at <= ?",
        (now,),
    ).fetchall()
    closed = 0
    for c in due:
        unit = db.execute("SELECT * FROM units WHERE id = ?", (c["unit_id"],)).fetchone()
        try:
            close_callout(db, unit, c, actor="system", reason="expired")
            closed += 1
        except CalloutError:
            pass  # closed by someone else in the meantime
    if closed:
        log.info("expired %d callout(s)", closed)
    return closed


_throttle_lock = threading.Lock()
_last_check = 0.0


def expire_due_throttled(db, min_interval: float = 30.0) -> None:
    """Cheap safety net on each request, in case the background thread is not running."""
    global _last_check
    with _throttle_lock:
        if time.monotonic() - _last_check < min_interval:
            return
        _last_check = time.monotonic()
    expire_due(db)
