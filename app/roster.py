"""Roster rows: one per position on a team, pointing at a person (or vacant)."""
from __future__ import annotations

import csv
import io
import re
import sqlite3

from .db import transaction
from .units import UnitError, _move, get_or_create_role, get_or_create_team

CSV_COLUMNS = ["position", "team", "username", "name", "phone", "role", "sort_order", "active"]
TRUE_WORDS = {"1", "true", "yes", "y", "active", "x"}
FALSE_WORDS = {"0", "false", "no", "n", "inactive", ""}


class RosterError(ValueError):
    pass


def tel_href(phone: str) -> str:
    """Digits (and a leading +) only, for a tap-to-call link."""
    phone = (phone or "").strip()
    plus = "+" if phone.startswith("+") else ""
    digits = re.sub(r"\D", "", phone)
    return f"tel:{plus}{digits}" if digits else ""


# Name, phone and username come from the person; team and role names are joined in.
_SELECT = """
SELECT r.*,
       p.username      AS tak_username,
       p.display_name  AS name,
       CASE WHEN p.phone_app <> '' THEN p.phone_app ELSE p.phone END AS work_phone,
       p.active        AS person_active,
       t.name          AS team_name,
       t.sort_order    AS team_order,
       ro.name         AS role
FROM roster r
LEFT JOIN people p  ON p.id  = r.person_id
LEFT JOIN teams  t  ON t.id  = r.team_id
LEFT JOIN roles  ro ON ro.id = r.role_id
"""
_ORDER = " ORDER BY (t.sort_order IS NULL), t.sort_order, r.sort_order, r.position"


def list_rows(db, unit_id: int, active_only=False):
    sql = _SELECT + " WHERE r.unit_id = ?" + (" AND r.active = 1" if active_only else "") + _ORDER
    return db.execute(sql, (unit_id,)).fetchall()


def get_row(db, unit_id: int, row_id: int):
    return db.execute(_SELECT + " WHERE r.unit_id = ? AND r.id = ?", (unit_id, row_id)).fetchone()


def rows_for_user(db, unit_id: int, username: str):
    return db.execute(
        _SELECT + " WHERE r.unit_id = ? AND r.active = 1 AND p.username = ?" + _ORDER, (unit_id, username)
    ).fetchall()


def grouped_by_team(rows, teams):
    """[(team_row_or_None, [rows])], in team order, 'Unassigned' (None) last, empty teams kept."""
    buckets = {t["id"]: [] for t in teams}
    loose = []
    for r in rows:
        (buckets.get(r["team_id"]) if r["team_id"] in buckets else loose).append(r)
    out = [(t, buckets[t["id"]]) for t in teams]
    if loose:
        out.append((None, loose))
    return out


def _fk(db, table: str, unit_id: int, raw, label: str):
    if raw in (None, "", "0", 0):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RosterError(f"Invalid {label}.") from exc
    if not db.execute(f"SELECT 1 FROM {table} WHERE id = ? AND unit_id = ?", (value, unit_id)).fetchone():
        raise RosterError(f"That {label} does not belong to this unit.")
    return value


def _clean(data: dict, db, unit_id: int, row_id: int | None = None) -> dict:
    position = (data.get("position") or "").strip()
    if not position:
        raise RosterError("Callsign is required (e.g. D-14).")
    person_id = None
    raw = data.get("person_id")
    if raw not in (None, "", "0", 0):
        try:
            person_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise RosterError("Invalid person.") from exc
        if not db.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone():
            raise RosterError("That person is not in the people list.")
        clash = db.execute(
            "SELECT position FROM roster WHERE unit_id = ? AND person_id = ? AND id IS NOT ?",
            (unit_id, person_id, row_id),
        ).fetchone()
        if clash:
            raise RosterError(f"That person already holds callsign {clash['position']} in this unit.")
    try:
        sort_order = int(str(data.get("sort_order") or "0").strip() or 0)
    except ValueError as exc:
        raise RosterError("Sort order must be a whole number.") from exc
    return {
        "person_id": person_id,
        "position": position,
        "team_id": _fk(db, "teams", unit_id, data.get("team_id"), "team"),
        "role_id": _fk(db, "roles", unit_id, data.get("role_id"), "role"),
        "sort_order": sort_order,
        "active": 1 if data.get("active") in (True, 1, "1", "on", "true", "yes") else 0,
    }


def add_row(db, unit_id: int, data: dict) -> int:
    row = _clean(data, db, unit_id)
    try:
        cur = db.execute(
            "INSERT INTO roster (unit_id, person_id, position, team_id, role_id, sort_order, active)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (unit_id, *row.values()),
        )
    except sqlite3.IntegrityError as exc:
        raise RosterError(f"Callsign '{row['position']}' already exists in this unit.") from exc
    return cur.lastrowid


def update_row(db, unit_id: int, row_id: int, data: dict) -> None:
    row = _clean(data, db, unit_id, row_id)
    sets = ", ".join(f"{k} = ?" for k in row)
    try:
        db.execute(f"UPDATE roster SET {sets} WHERE unit_id = ? AND id = ?", (*row.values(), unit_id, row_id))
    except sqlite3.IntegrityError as exc:
        raise RosterError(f"Callsign '{row['position']}' already exists in this unit.") from exc


def delete_row(db, unit_id: int, row_id: int) -> None:
    db.execute("DELETE FROM roster WHERE unit_id = ? AND id = ?", (unit_id, row_id))


def vacate_row(db, unit_id: int, row_id: int) -> None:
    """Keep the position, drop the person."""
    db.execute("UPDATE roster SET person_id = NULL WHERE unit_id = ? AND id = ?", (unit_id, row_id))


def set_active(db, unit_id: int, row_id: int, active: bool) -> None:
    db.execute("UPDATE roster SET active = ? WHERE unit_id = ? AND id = ?", (1 if active else 0, unit_id, row_id))


def move_row(db, unit_id: int, row_id: int, direction: str) -> None:
    row = get_row(db, unit_id, row_id)
    if row is None:
        return
    if row["team_id"] is None:
        _move(db, "roster", "unit_id = ? AND team_id IS NULL", (unit_id,), row_id, direction)
    else:
        _move(db, "roster", "unit_id = ? AND team_id = ?", (unit_id, row["team_id"]), row_id, direction)


# ---- CSV -----------------------------------------------------------------------

def export_csv(db, unit_id: int) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in list_rows(db, unit_id):
        w.writerow({
            "position": r["position"], "team": r["team_name"] or "", "username": r["tak_username"] or "",
            "name": r["name"] or "", "phone": r["work_phone"] or "", "role": r["role"] or "",
            "sort_order": r["sort_order"], "active": "yes" if r["active"] else "no",
        })
    return buf.getvalue()


def import_csv(db, unit_id: int, text: str, mode: str = "merge") -> dict:
    """Upsert rows by position. ``mode='replace'`` first deletes every row not in the file.

    ``username`` must match someone in the people list (blank = vacant). The
    name and phone columns are ignored on import: they belong to Authentik.
    Returns {'added', 'updated', 'deleted', 'errors'}; on any error nothing is written.
    """
    if mode not in ("merge", "replace"):
        raise RosterError("Import mode must be merge or replace.")
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RosterError("The file is empty.")
    headers = {h.strip().lower().replace(" ", "_"): h for h in reader.fieldnames if h}
    if "position" not in headers:
        raise RosterError("The CSV needs a 'position' column. Expected columns: " + ", ".join(CSV_COLUMNS))

    def cell(raw, key):
        h = headers.get(key)
        return (raw.get(h) or "").strip() if h else ""

    summary = {"added": 0, "updated": 0, "deleted": 0, "errors": []}
    seen = set()
    with transaction(db):
        existing = {r["position"].lower(): r for r in list_rows(db, unit_id)}
        for n, raw in enumerate(reader, start=2):
            position = cell(raw, "position")
            if not position:
                continue
            if position.lower() in seen:
                summary["errors"].append(f"line {n}: duplicate position '{position}' in file")
                continue
            seen.add(position.lower())
            cur = existing.get(position.lower())

            active_raw = cell(raw, "active").lower()
            if "active" not in headers:
                active = cur["active"] if cur else True
            elif active_raw in TRUE_WORDS:
                active = True
            elif active_raw in FALSE_WORDS:
                active = False
            else:
                summary["errors"].append(f"line {n}: active must be yes/no, got '{active_raw}'")
                continue

            person_id = cur["person_id"] if cur else None
            if "username" in headers:
                username = cell(raw, "username")
                if username:
                    person = db.execute("SELECT id FROM people WHERE username = ?", (username,)).fetchone()
                    if person is None:
                        summary["errors"].append(f"line {n}: '{username}' is not in the people list")
                        continue
                    person_id = person["id"]
                else:
                    person_id = None

            team = get_or_create_team(db, unit_id, cell(raw, "team")) if "team" in headers else None
            role = get_or_create_role(db, unit_id, cell(raw, "role")) if "role" in headers else None
            data = {
                "position": position,
                "person_id": person_id,
                "team_id": (team["id"] if team else None) if "team" in headers else (cur["team_id"] if cur else None),
                "role_id": (role["id"] if role else None) if "role" in headers else (cur["role_id"] if cur else None),
                "sort_order": cell(raw, "sort_order") if "sort_order" in headers else str(cur["sort_order"] if cur else 0),
                "active": active,
            }
            try:
                if cur:
                    update_row(db, unit_id, cur["id"], data)
                    summary["updated"] += 1
                else:
                    add_row(db, unit_id, data)
                    summary["added"] += 1
            except RosterError as exc:
                summary["errors"].append(f"line {n}: {exc}")
        if mode == "replace":
            for pos, r in existing.items():
                if pos not in seen:
                    delete_row(db, unit_id, r["id"])
                    summary["deleted"] += 1
        if summary["errors"]:
            raise RosterImportFailed(summary)
    return summary


class RosterImportFailed(RosterError):
    def __init__(self, summary):
        self.summary = summary
        super().__init__("; ".join(summary["errors"][:5]) + (" …" if len(summary["errors"]) > 5 else ""))
