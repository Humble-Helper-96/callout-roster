"""The single internal hook the TAK integration will attach to.

Called whenever a callout opens, closes, is extended, gets a response, or has
its location changed. Today it logs a JSON line to stdout and stores the event
in the ``events`` table (which also feeds the callout history page). The
TAK Server maintainers will wire it to TAK Server later; the payload already
carries what a marker needs: unit, label, location, per-team counts and the
unit's TAK settings.
"""
from __future__ import annotations

import json
import logging

from .timeutil import to_iso, utcnow

log = logging.getLogger("calloutroster.tak")

EVENT_OPENED = "callout.opened"
EVENT_CLOSED = "callout.closed"
EVENT_EXTENDED = "callout.extended"
EVENT_RESPONSE = "callout.response"
EVENT_LOCATION = "callout.location_changed"
EVENT_BRIEF = "callout.brief_changed"


def tak_hook(db, event: str, unit, callout, counts: dict, actor: str | None = None, extra: dict | None = None) -> None:
    payload = {
        "event": event,
        "actor": actor,
        "unit": {
            "id": unit["id"], "slug": unit["slug"], "name": unit["name"],
            "tak_settings": json.loads(unit["tak_settings"] or "{}"),
        },
        "callout": {
            "id": callout["id"], "label": callout["label"], "brief": callout["brief"],
            "opened_by": callout["opened_by"], "opened_at": callout["opened_at"],
            "expires_at": callout["expires_at"], "closed_at": callout["closed_at"],
            "lat": callout["lat"], "lon": callout["lon"], "location_label": callout["location_label"],
            "roster_path": f"u/{unit['slug']}/",   # relative to the app root; the proxy knows the host
        },
        "counts": counts,
    }
    if extra:
        payload.update(extra)
    log.info("tak_hook %s", json.dumps(payload, separators=(",", ":"), default=str))
    db.execute(
        "INSERT INTO events (unit_id, callout_id, type, actor, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (unit["id"], callout["id"], event, actor, json.dumps(payload, default=str), to_iso(utcnow())),
    )


def list_events(db, callout_id: int):
    return db.execute("SELECT * FROM events WHERE callout_id = ? ORDER BY id", (callout_id,)).fetchall()
