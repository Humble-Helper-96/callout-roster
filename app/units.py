"""Units, their teams and their saved locations.

Everything hangs off a unit (Metro SWAT today; another team or agency tomorrow).
A unit owns a roster, callouts, TAK marker settings and one Authentik group:
being in that group is what gives access to the unit.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from .db import get_meta, set_meta, transaction
from .timeutil import to_iso, utcnow

log = logging.getLogger("calloutroster.units")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEFAULT_TEAMS = ("SWAT Command", "SWAT Red Team", "SWAT Blue Team", "CNT", "TSU")
DEFAULT_TEAM_COLORS = {"SWAT Command": "#3fb950", "SWAT Red Team": "#f85149", "SWAT Blue Team": "#4ea1ff",
                       "CNT": "#f4a261", "TSU": "#c8a97e"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_color(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if raw and not COLOR_RE.match(raw):
        raise UnitError("Colour must be a hex value like #3fb950.")
    return raw
DEFAULT_ROLES = ("Team Leader", "Assistant Team Leader", "Entry", "Breacher", "Sniper", "Medic")


class UnitError(ValueError):
    pass


def validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise UnitError("Slug must be 1-40 characters: lowercase letters, digits and hyphens.")
    return slug


def validate_tak_settings(raw: str) -> str:
    raw = (raw or "").strip() or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnitError(f"TAK settings must be valid JSON: {exc.msg} (line {exc.lineno}).") from exc
    if not isinstance(parsed, dict):
        raise UnitError("TAK settings must be a JSON object.")
    return json.dumps(parsed, indent=2, sort_keys=True)


def list_units(db, active_only=True):
    sql = "SELECT * FROM units" + (" WHERE active = 1" if active_only else "") + " ORDER BY name"
    return db.execute(sql).fetchall()


def get_unit(db, unit_id: int):
    return db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()


def get_unit_by_slug(db, slug: str):
    return db.execute("SELECT * FROM units WHERE slug = ?", (slug,)).fetchone()


def visible_units(db, user):
    """Units whose member group the user is in."""
    return [u for u in list_units(db) if user.in_group(u["member_group"])]


def create_unit(db, *, slug, name, member_group, callout_open_role=None,
                tak_settings="{}", teams=DEFAULT_TEAMS, roles=DEFAULT_ROLES) -> int:
    slug = validate_slug(slug)
    name = (name or "").strip()
    member_group = (member_group or "").strip()
    if not name or not member_group:
        raise UnitError("Name and member group are required.")
    if callout_open_role not in (None, "", "member", "admin"):
        raise UnitError("Callout open role must be member or admin.")
    tak_settings = validate_tak_settings(tak_settings)
    try:
        with transaction(db):
            cur = db.execute(
                "INSERT INTO units (slug, name, member_group, callout_open_role, tak_settings, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (slug, name, member_group, callout_open_role or None, tak_settings, to_iso(utcnow())),
            )
            unit_id = cur.lastrowid
            for i, team in enumerate(teams):
                db.execute("INSERT INTO teams (unit_id, name, color, sort_order) VALUES (?, ?, ?, ?)",
                           (unit_id, team, DEFAULT_TEAM_COLORS.get(team, ""), i))
            for i, role in enumerate(roles):
                db.execute("INSERT INTO roles (unit_id, name, sort_order) VALUES (?, ?, ?)", (unit_id, role, i))
    except sqlite3.IntegrityError as exc:
        raise UnitError(f"A unit with slug '{slug}' already exists.") from exc
    return unit_id


def update_unit(db, unit_id: int, **fields) -> None:
    allowed = {"slug", "name", "member_group", "callout_open_role", "tak_settings", "active"}
    bad = set(fields) - allowed
    if bad:
        raise UnitError(f"Unknown unit fields: {sorted(bad)}")
    if "slug" in fields:
        fields["slug"] = validate_slug(fields["slug"])
    if "tak_settings" in fields:
        fields["tak_settings"] = validate_tak_settings(fields["tak_settings"])
    if "callout_open_role" in fields:
        role = (fields["callout_open_role"] or "").strip() or None
        if role not in (None, "member", "admin"):
            raise UnitError("Callout open role must be member, admin or blank (inherit).")
        fields["callout_open_role"] = role
    for key in ("name", "member_group"):
        if key in fields and not (fields[key] or "").strip():
            raise UnitError(f"{key.replace('_', ' ').capitalize()} is required.")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    try:
        db.execute(f"UPDATE units SET {sets} WHERE id = ?", (*fields.values(), unit_id))
    except sqlite3.IntegrityError as exc:
        raise UnitError("That slug is already used by another unit.") from exc


DEFAULT_UNIT_FIELDS = (("slug", "default_unit_slug"), ("name", "default_unit_name"),
                       ("member_group", "default_unit_member_group"))


def ensure_default_unit(db, cfg) -> None:
    """Keep the default unit in step with DEFAULT_UNIT_* in the environment.

    Empty database: create it. Otherwise re-apply any DEFAULT_UNIT_* value
    that differs from the stored one and log the change, so editing .env and
    restarting is enough (no database wipe). The default unit is remembered
    in ``meta``; older databases fall back to the first unit created.
    """
    raw = get_meta(db, "default_unit_id")
    unit = get_unit(db, int(raw)) if raw else None
    if unit is None:
        unit = db.execute("SELECT * FROM units ORDER BY id LIMIT 1").fetchone()
    if unit is None:
        unit_id = create_unit(db, slug=cfg.default_unit_slug, name=cfg.default_unit_name,
                              member_group=cfg.default_unit_member_group)
        set_meta(db, "default_unit_id", str(unit_id))
        return
    if raw != str(unit["id"]):
        set_meta(db, "default_unit_id", str(unit["id"]))
    for column, attr in DEFAULT_UNIT_FIELDS:
        wanted = (getattr(cfg, attr) or "").strip()
        if not wanted or wanted == unit[column]:
            continue
        try:
            update_unit(db, unit["id"], **{column: wanted})
        except UnitError as exc:
            log.error("default unit: cannot change %s from %r to %r: %s", column, unit[column], wanted, exc)
            continue
        log.warning("default unit: %s changed from %r to %r (from %s)",
                    column, unit[column], wanted, attr.upper())
        if column == "slug":
            log.warning("default unit: its pages moved from u/%s/ to u/%s/", unit["slug"], wanted)


# ---- teams -------------------------------------------------------------------

def list_teams(db, unit_id: int):
    return db.execute(
        "SELECT * FROM teams WHERE unit_id = ? ORDER BY sort_order, name", (unit_id,)
    ).fetchall()


def get_team(db, unit_id: int, team_id: int):
    return db.execute("SELECT * FROM teams WHERE unit_id = ? AND id = ?", (unit_id, team_id)).fetchone()


def get_or_create_team(db, unit_id: int, name: str):
    name = (name or "").strip()
    if not name:
        return None
    row = db.execute("SELECT * FROM teams WHERE unit_id = ? AND name = ? COLLATE NOCASE", (unit_id, name)).fetchone()
    if row:
        return row
    return add_team(db, unit_id, name)


def add_team(db, unit_id: int, name: str):
    name = (name or "").strip()
    if not name:
        raise UnitError("Team name is required.")
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM teams WHERE unit_id = ?", (unit_id,)).fetchone()[0]
    try:
        cur = db.execute("INSERT INTO teams (unit_id, name, sort_order) VALUES (?, ?, ?)", (unit_id, name, nxt))
    except sqlite3.IntegrityError as exc:
        raise UnitError(f"A team named '{name}' already exists.") from exc
    return get_team(db, unit_id, cur.lastrowid)


def rename_team(db, unit_id: int, team_id: int, name: str, color: str | None = None) -> None:
    name = (name or "").strip()
    if not name:
        raise UnitError("Team name is required.")
    try:
        if color is None:
            db.execute("UPDATE teams SET name = ? WHERE unit_id = ? AND id = ?", (name, unit_id, team_id))
        else:
            db.execute("UPDATE teams SET name = ?, color = ? WHERE unit_id = ? AND id = ?",
                       (name, validate_color(color), unit_id, team_id))
    except sqlite3.IntegrityError as exc:
        raise UnitError(f"A team named '{name}' already exists.") from exc


def move_team(db, unit_id: int, team_id: int, direction: str) -> None:
    _move(db, "teams", "unit_id = ?", (unit_id,), team_id, direction)


def delete_team(db, unit_id: int, team_id: int) -> None:
    # Roster rows that pointed at the team become "Unassigned" (ON DELETE SET NULL).
    db.execute("DELETE FROM teams WHERE unit_id = ? AND id = ?", (unit_id, team_id))


def _move(db, table: str, scope_sql: str, scope_args: tuple, row_id: int, direction: str) -> None:
    """Renumber the scoped rows 0..n-1 then swap the row with its neighbour."""
    if direction not in ("up", "down"):
        raise UnitError("Direction must be up or down.")
    with transaction(db):
        rows = db.execute(
            f"SELECT id FROM {table} WHERE {scope_sql} ORDER BY sort_order, id", scope_args
        ).fetchall()
        ids = [r["id"] for r in rows]
        if row_id not in ids:
            return
        i = ids.index(row_id)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for order, rid in enumerate(ids):
            db.execute(f"UPDATE {table} SET sort_order = ? WHERE id = ?", (order, rid))


# ---- roles (per unit pick-list; change freely, incident by incident) -------

def list_roles(db, unit_id: int):
    return db.execute("SELECT * FROM roles WHERE unit_id = ? ORDER BY sort_order, name", (unit_id,)).fetchall()


def get_role(db, unit_id: int, role_id: int):
    return db.execute("SELECT * FROM roles WHERE unit_id = ? AND id = ?", (unit_id, role_id)).fetchone()


def get_or_create_role(db, unit_id: int, name: str):
    name = (name or "").strip()
    if not name:
        return None
    row = db.execute("SELECT * FROM roles WHERE unit_id = ? AND name = ? COLLATE NOCASE", (unit_id, name)).fetchone()
    return row or add_role(db, unit_id, name)


def add_role(db, unit_id: int, name: str):
    name = (name or "").strip()
    if not name:
        raise UnitError("Role name is required.")
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM roles WHERE unit_id = ?", (unit_id,)).fetchone()[0]
    try:
        cur = db.execute("INSERT INTO roles (unit_id, name, sort_order) VALUES (?, ?, ?)", (unit_id, name, nxt))
    except sqlite3.IntegrityError as exc:
        raise UnitError(f"A role named '{name}' already exists.") from exc
    return get_role(db, unit_id, cur.lastrowid)


def rename_role(db, unit_id: int, role_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise UnitError("Role name is required.")
    try:
        db.execute("UPDATE roles SET name = ? WHERE unit_id = ? AND id = ?", (name, unit_id, role_id))
    except sqlite3.IntegrityError as exc:
        raise UnitError(f"A role named '{name}' already exists.") from exc


def move_role(db, unit_id: int, role_id: int, direction: str) -> None:
    _move(db, "roles", "unit_id = ?", (unit_id,), role_id, direction)


def delete_role(db, unit_id: int, role_id: int) -> None:
    # Roster rows that had this role keep their position, with no role (ON DELETE SET NULL).
    db.execute("DELETE FROM roles WHERE unit_id = ? AND id = ?", (unit_id, role_id))


# ---- coordinates --------------------------------------------------------------

COORDS_EXAMPLE = "39.0997, -94.5786"


def parse_coords(text):
    """Parse a pasted "lat, lon" pair (as CloudTAK copies it) into floats.

    Accepts a comma, space or tab between the two numbers and ignores
    stray whitespace. Returns (None, None) for blank input; raises
    UnitError with a readable message otherwise.
    """
    raw = (text or "").strip().replace("\u00a0", " ")
    if not raw:
        return None, None
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    if len(parts) != 2:
        raise UnitError(f"Paste the coordinates as latitude, longitude — for example {COORDS_EXAMPLE}.")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise UnitError(f"Coordinates must be decimal degrees, for example {COORDS_EXAMPLE}.") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise UnitError("Latitude must be within ±90 and longitude within ±180 (latitude first).")
    return round(lat, 6), round(lon, 6)


def parse_latlon(lat_raw, lon_raw):
    """Separate lat/lon values (CSV, scripts); the forms use parse_coords."""
    lat_raw = (lat_raw or "").strip()
    lon_raw = (lon_raw or "").strip()
    if not lat_raw and not lon_raw:
        return None, None
    return parse_coords(f"{lat_raw}, {lon_raw}")


def add_location(db, unit_id: int, label: str, coords):
    label = (label or "").strip()
    if not label:
        raise UnitError("Location label is required.")
    lat, lon = parse_coords(coords)
    if lat is None:
        raise UnitError("Latitude and longitude are required for a saved location.")
    nxt = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM locations WHERE unit_id = ?", (unit_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO locations (unit_id, label, lat, lon, sort_order) VALUES (?, ?, ?, ?, ?)",
        (unit_id, label, lat, lon, nxt),
    )


def delete_location(db, unit_id: int, loc_id: int) -> None:
    db.execute("DELETE FROM locations WHERE unit_id = ? AND id = ?", (unit_id, loc_id))
