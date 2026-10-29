"""People: everyone the app knows, keyed by Authentik username.

Three ways a person gets here, in order of authority:

* ``authentik`` — pulled from the Authentik REST API by :func:`sync_from_authentik`
  (name, email, phone, group membership). Runs on a timer and on demand.
* ``header``    — created the first time someone signs in through the proxy,
  from the identity headers (name and email fill in only while still empty). Lets team building work before (or without)
  an API token; the sync upgrades the record when it runs.
* ``manual``    — typed in by an admin on the People page.

Roster rows point at a person; the person's username is what the response
buttons match against the ``X-authentik-username`` header.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .db import set_meta, transaction
from .timeutil import to_iso, utcnow

log = logging.getLogger("calloutroster.people")


class PeopleError(ValueError):
    pass


# ---- queries --------------------------------------------------------------------

# phone_effective: the number typed in the app wins over the synced one.
_SELECT = """
SELECT p.*,
       CASE WHEN p.phone_app <> '' THEN p.phone_app ELSE p.phone END AS phone_effective,
       (SELECT group_concat(group_name, '|') FROM people_groups g WHERE g.person_id = p.id) AS groups
FROM people p
"""


def list_people(db):
    return db.execute(_SELECT + " ORDER BY p.active DESC, p.display_name, p.username").fetchall()


def get_person(db, person_id: int):
    return db.execute(_SELECT + " WHERE p.id = ?", (person_id,)).fetchone()


def get_person_by_username(db, username: str):
    return db.execute(_SELECT + " WHERE p.username = ?", (username,)).fetchone()


def people_for_unit(db, unit):
    """People in the unit's member group."""
    return db.execute(
        _SELECT + " WHERE p.id IN (SELECT person_id FROM people_groups WHERE group_name = ?)"
        " ORDER BY p.active DESC, p.display_name, p.username",
        (unit["member_group"],),
    ).fetchall()


def available_for_unit(db, unit):
    """Unit people who are active and not yet on this unit's roster."""
    return db.execute(
        _SELECT + " WHERE p.active = 1"
        " AND p.id IN (SELECT person_id FROM people_groups WHERE group_name = ?)"
        " AND p.id NOT IN (SELECT person_id FROM roster WHERE unit_id = ? AND person_id IS NOT NULL)"
        " ORDER BY p.display_name, p.username",
        (unit["member_group"], unit["id"]),
    ).fetchall()


# ---- writes -----------------------------------------------------------------------

def upsert_person(db, *, username: str, display_name: str = "", email: str = "", phone: str = "",
                  authentik_pk: int | None = None, active: bool = True, source: str,
                  groups: list[str] | None = None) -> int:
    """Create or update by username. Group list, when given, replaces the stored one.

    A record from a lower-authority source never overwrites authentik data
    with blanks: header/manual updates only fill fields that are empty.
    """
    username = (username or "").strip()
    if not username:
        raise PeopleError("Username is required.")
    now = to_iso(utcnow())
    row = db.execute("SELECT * FROM people WHERE username = ?", (username,)).fetchone()
    if row is None:
        cur = db.execute(
            "INSERT INTO people (username, display_name, email, phone, authentik_pk, active, source, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (username, display_name or username, email, phone, authentik_pk, 1 if active else 0, source, now, now),
        )
        pid = cur.lastrowid
    else:
        pid = row["id"]
        if source == "authentik" or row["source"] != "authentik":
            fields = {
                "display_name": display_name or row["display_name"],
                "email": email or row["email"],
                "phone": phone or row["phone"],
                "authentik_pk": authentik_pk if authentik_pk is not None else row["authentik_pk"],
                "active": 1 if active else 0,
                "source": "authentik" if source == "authentik" else row["source"],
                "last_seen_at": now,
            }
        else:  # header/manual touch on an authentik-sourced person: just mark seen, keep active
            fields = {"last_seen_at": now, "active": 1 if active else row["active"]}
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE people SET {sets} WHERE id = ?", (*fields.values(), pid))
    if groups is not None:
        db.execute("DELETE FROM people_groups WHERE person_id = ?", (pid,))
        db.executemany("INSERT OR IGNORE INTO people_groups (person_id, group_name) VALUES (?, ?)",
                       [(pid, g) for g in groups if g])
    return pid


def touch_from_header(db, user) -> None:
    """Called on every authenticated request: learn or refresh the person.

    The proxy's name / email headers only fill gaps: a name that is blank or
    still just the username, an empty email. They never overwrite a name from
    the Authentik sync or one typed in the app.
    """
    pid = upsert_person(db, username=user.username, source="header", active=True, groups=sorted(user.groups))
    if user.name:
        db.execute("UPDATE people SET display_name = ? WHERE id = ? AND (display_name = '' OR display_name = username)",
                   (user.name, pid))
    if user.email:
        db.execute("UPDATE people SET email = ? WHERE id = ? AND email = ''", (user.email, pid))


def add_manual_person(db, username: str, display_name: str, phone: str, groups: list[str]) -> int:
    username = (username or "").strip()
    if not username or " " in username:
        raise PeopleError("Username is required and cannot contain spaces.")
    if db.execute("SELECT 1 FROM people WHERE username = ?", (username,)).fetchone():
        raise PeopleError(f"'{username}' is already in the people list.")
    pid = upsert_person(db, username=username, display_name=(display_name or "").strip(), source="manual", groups=groups)
    db.execute("UPDATE people SET phone_app = ? WHERE id = ?", ((phone or "").strip(), pid))
    return pid


def update_person(db, person_id: int, display_name: str | None, phone: str) -> None:
    """Edit in the app. Phone goes into ``phone_app`` so a sync cannot undo it;
    a blank phone clears the override and the synced number shows again.
    Name is only editable for people who did not come from Authentik."""
    person = db.execute("SELECT source FROM people WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        raise PeopleError("No such person.")
    db.execute("UPDATE people SET phone_app = ? WHERE id = ?", ((phone or "").strip(), person_id))
    if display_name is not None and person["source"] != "authentik":
        db.execute("UPDATE people SET display_name = ? WHERE id = ?", ((display_name or "").strip(), person_id))


def set_person_active(db, person_id: int, active: bool) -> None:
    db.execute("UPDATE people SET active = ? WHERE id = ?", (1 if active else 0, person_id))


# ---- Authentik sync -------------------------------------------------------------------

def authentik_configured(cfg) -> bool:
    return bool(cfg.authentik_url and cfg.authentik_token)


def fetch_group_members(cfg, group_name: str) -> list[dict]:
    """All users in an Authentik group, via GET /api/v3/core/users/?groups_by_name=…

    Returns the raw user dicts (username, name, email, is_active, attributes, …).
    Raises PeopleError with a readable message on any HTTP or parsing failure.
    """
    users: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"groups_by_name": group_name, "page": page, "page_size": 200})
        req = urllib.request.Request(
            f"{cfg.authentik_url}/api/v3/core/users/?{query}",
            headers={"Authorization": f"Bearer {cfg.authentik_token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PeopleError(f"Authentik answered HTTP {exc.code} for group '{group_name}'.") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            raise PeopleError(f"Could not reach Authentik at {cfg.authentik_url}: {exc}") from exc
        users.extend(body.get("results", []))
        nxt = (body.get("pagination") or {}).get("next") or 0
        if not nxt or nxt <= page:
            break
        page = nxt
    return users


def _get_json(cfg, path: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{cfg.authentik_url}{path}?{query}",
        headers={"Authorization": f"Bearer {cfg.authentik_token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PeopleError(f"Authentik answered HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise PeopleError(f"Could not reach Authentik at {cfg.authentik_url}: {exc}") from exc


def sync_from_authentik(db, cfg, units, fetch=fetch_group_members) -> dict:
    """Pull each active unit's member group and update the people table.

    A group that fails (e.g. HTTP 400 for a name Authentik does not know) is
    logged and skipped; the others still sync. Nobody is deactivated after a
    partial sync, since a failed lookup says nothing about who left. Only when
    every group fails does this raise.

    ``fetch`` is injectable for tests. Returns a summary dict and records it in
    ``meta`` so the People page can show when the last sync ran.
    """
    if not authentik_configured(cfg):
        raise PeopleError("Authentik sync is not configured (AUTHENTIK_URL / AUTHENTIK_TOKEN).")
    groups = sorted({u["member_group"] for u in units if u["active"] and u["member_group"]})
    by_username: dict[str, tuple[dict, set]] = {}
    failed: list[dict] = []
    for group in groups:
        try:
            members = fetch(cfg, group)
        except PeopleError as exc:
            log.warning("authentik sync: skipping group %r: %s", group, exc)
            failed.append({"group": group, "error": str(exc)})
            continue
        for user in members:
            username = (user.get("username") or "").strip()
            if not username:
                continue
            entry = by_username.setdefault(username, (user, set()))
            entry[1].add(group)
    if groups and len(failed) == len(groups):
        raise PeopleError("; ".join(f["error"] for f in failed))
    now = to_iso(utcnow())
    with transaction(db):
        seen_ids = []
        for username, (user, user_groups) in by_username.items():
            attrs = user.get("attributes") or {}
            phone = attrs.get(cfg.authentik_phone_attribute) or ""
            pid = upsert_person(
                db, username=username, display_name=user.get("name") or "", email=user.get("email") or "",
                phone=str(phone), authentik_pk=user.get("pk"), active=bool(user.get("is_active", True)),
                source="authentik", groups=sorted(user_groups),
            )
            seen_ids.append(pid)
        # Authentik-sourced people who are no longer in any synced group drop out.
        gone = 0
        if not failed:
            marks = ",".join("?" * len(seen_ids)) or "NULL"
            gone = db.execute(
                f"UPDATE people SET active = 0 WHERE source = 'authentik' AND active = 1 AND id NOT IN ({marks})",
                seen_ids,
            ).rowcount
        summary = {"at": now, "groups": groups, "people": len(by_username), "deactivated": gone,
                   "failed": failed, "error": None}
        set_meta(db, "authentik_last_sync", json.dumps(summary))
    log.info("authentik sync: %d people across %d groups, %d deactivated, %d groups skipped",
             len(by_username), len(groups), gone, len(failed))
    return summary


def record_sync_error(db, message: str) -> None:
    set_meta(db, "authentik_last_sync", json.dumps({"at": to_iso(utcnow()), "error": message}))
