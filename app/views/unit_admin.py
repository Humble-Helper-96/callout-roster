"""Per-unit team building: roster, people, teams and roles.

Every unit member may get here. When ADMIN_PASSWORD is set they unlock first;
the unlock lasts ADMIN_UNLOCK_HOURS in a signed session cookie.
"""
from __future__ import annotations

import json

from flask import Blueprint, abort, current_app, g, redirect, render_template, request

from .. import auth
from .. import people as pe
from .. import roster as ro
from .. import units as un
from ..db import get_db, get_meta
from ..urls import rel
from .common import attach_unit_loading

bp = Blueprint("unit_admin", __name__, url_prefix="/u/<slug>/admin")
attach_unit_loading(bp)

ADMIN_TABS = ("roster", "people", "teams")


@bp.before_request
def _require_unlock():
    if request.endpoint in ("unit_admin.unlock", "unit_admin.lock") or g.admin_unlocked:
        return None
    tab = (request.path.rstrip("/").split("/admin/", 1) + [""])[1].split("/", 1)[0]
    return redirect(aurl("unlock", next=tab if tab in ADMIN_TABS else None))


def aurl(path: str = "", **query) -> str:
    return rel(f"u/{g.unit['slug']}/admin/{path}", **query)


def _form_row() -> dict:
    return {k: request.form.get(k, "") for k in ("person_id", "position", "team_id", "role_id", "sort_order")} | {
        "active": "on" if request.form.get("active") else ""}


# ---- password lock -----------------------------------------------------------------

@bp.route("/unlock", methods=["GET", "POST"])
def unlock():
    nxt = request.values.get("next", "")
    nxt = nxt if nxt in ADMIN_TABS else "roster"
    if g.admin_unlocked:
        return redirect(aurl(nxt))
    error = None
    if request.method == "POST":
        if auth.try_unlock(request.form.get("password", "")):
            return redirect(aurl(nxt, ok="Admin unlocked."))
        error = ("Too many wrong passwords. Try again in a few minutes."
                 if auth.locked_out(g.user.username) else "Wrong password.")
    return render_template("admin/unlock.html", nav_active="admin", next=nxt, error=error,
                           hours=current_app.config["APP"].admin_unlock_hours), (400 if error else 200)


@bp.post("/lock")
def lock():
    auth.lock_admin()
    return redirect(rel(f"u/{g.unit['slug']}/", ok="Admin locked."))


# ---- roster -------------------------------------------------------------------------

@bp.get("/")
def home():
    return redirect(aurl("roster"))


@bp.get("/roster")
def roster_list():
    db = get_db()
    teams = un.list_teams(db, g.unit["id"])
    rows = ro.list_rows(db, g.unit["id"])
    return render_template("admin/roster.html", admin_tab="roster", teams=teams,
                           roles=un.list_roles(db, g.unit["id"]),
                           grouped=ro.grouped_by_team(rows, teams), total=len(rows),
                           available=pe.available_for_unit(db, g.unit))


@bp.post("/roster")
def roster_add():
    data = _form_row()
    data["active"] = "on"
    try:
        ro.add_row(get_db(), g.unit["id"], data)
    except ro.RosterError as exc:
        return redirect(aurl("roster", err=str(exc)))
    return redirect(aurl("roster", ok=f"Added {data['position'].strip()}."))


@bp.get("/roster/<int:rid>")
def roster_edit(rid):
    db = get_db()
    row = ro.get_row(db, g.unit["id"], rid)
    if row is None:
        abort(404, "No such roster row.")
    choices = list(pe.available_for_unit(db, g.unit))
    if row["person_id"]:
        choices.insert(0, pe.get_person(db, row["person_id"]))
    return render_template("admin/roster_edit.html", admin_tab="roster", row=row, choices=choices,
                           teams=un.list_teams(db, g.unit["id"]), roles=un.list_roles(db, g.unit["id"]))


@bp.post("/roster/<int:rid>")
def roster_save(rid):
    db = get_db()
    if ro.get_row(db, g.unit["id"], rid) is None:
        abort(404, "No such roster row.")
    try:
        ro.update_row(db, g.unit["id"], rid, _form_row())
    except ro.RosterError as exc:
        return redirect(aurl(f"roster/{rid}", err=str(exc)))
    return redirect(aurl("roster", ok="Saved."))


@bp.post("/roster/<int:rid>/delete")
def roster_delete(rid):
    ro.delete_row(get_db(), g.unit["id"], rid)
    return redirect(aurl("roster", ok="Position removed."))


@bp.post("/roster/<int:rid>/vacate")
def roster_vacate(rid):
    ro.vacate_row(get_db(), g.unit["id"], rid)
    return redirect(aurl("roster", ok="Position marked vacant."))


@bp.post("/roster/<int:rid>/active")
def roster_active(rid):
    active = request.form.get("active") == "1"
    ro.set_active(get_db(), g.unit["id"], rid, active)
    return redirect(aurl("roster", ok="Position reactivated." if active else "Position marked inactive."))


@bp.post("/roster/<int:rid>/move")
def roster_move(rid):
    try:
        ro.move_row(get_db(), g.unit["id"], rid, request.form.get("dir", ""))
    except un.UnitError as exc:
        return redirect(aurl("roster", err=str(exc)))
    return redirect(aurl("roster"))


# ---- people --------------------------------------------------------------------------

@bp.get("/people")
def people():
    db = get_db()
    cfg = current_app.config["APP"]
    raw = get_meta(db, "authentik_last_sync")
    on_roster = {r["person_id"] for r in ro.list_rows(db, g.unit["id"]) if r["person_id"]}
    edit_id = request.args.get("edit", type=int)
    return render_template("admin/people.html", admin_tab="people",
                           people=pe.people_for_unit(db, g.unit), on_roster=on_roster,
                           edit_person=pe.get_person(db, edit_id) if edit_id else None,
                           open_add=request.args.get("add") == "1",
                           sync_configured=pe.authentik_configured(cfg),
                           last_sync=json.loads(raw) if raw else None,
                           group_name=g.unit["member_group"])


@bp.post("/people/sync")
def people_sync():
    db = get_db()
    cfg = current_app.config["APP"]
    try:
        summary = pe.sync_from_authentik(db, cfg, un.list_units(db))
    except pe.PeopleError as exc:
        pe.record_sync_error(db, str(exc))
        return redirect(aurl("people", err=f"Sync failed: {exc}"))
    if summary["failed"]:
        skipped = ", ".join(f["group"] for f in summary["failed"])
        return redirect(aurl("people", err=f"Synced {summary['people']} people, but skipped {skipped}. "
                                          "Check the group name in Authentik."))
    return redirect(aurl("people", ok=f"Synced {summary['people']} people from Authentik."))


@bp.post("/people")
def people_add():
    try:
        pe.add_manual_person(get_db(), request.form.get("username", ""), request.form.get("display_name", ""),
                             request.form.get("phone", ""), [g.unit["member_group"]])
    except pe.PeopleError as exc:
        return redirect(aurl("people", err=str(exc)))
    return redirect(aurl("people", ok="Person added."))


@bp.post("/people/<int:pid>")
def people_save(pid):
    db = get_db()
    person = pe.get_person(db, pid)
    if person is None:
        abort(404, "No such person.")
    if request.form.get("revert") == "1":          # drop the app override, back to Authentik's number
        pe.update_person(db, pid, None, "")
        return redirect(aurl("people", ok=f"{person['display_name']}: using the Authentik number again."))
    pe.update_person(db, pid, request.form.get("display_name"), request.form.get("phone", ""))
    return redirect(aurl("people", ok="Saved."))


@bp.post("/people/<int:pid>/active")
def people_active(pid):
    pe.set_person_active(get_db(), pid, request.form.get("active") == "1")
    return redirect(aurl("people"))


# ---- teams and roles --------------------------------------------------------------------

@bp.get("/teams")
def teams():
    db = get_db()
    team_counts = {r["team_id"]: r["n"] for r in db.execute(
        "SELECT team_id, COUNT(*) AS n FROM roster WHERE unit_id = ? GROUP BY team_id", (g.unit["id"],))}
    role_counts = {r["role_id"]: r["n"] for r in db.execute(
        "SELECT role_id, COUNT(*) AS n FROM roster WHERE unit_id = ? GROUP BY role_id", (g.unit["id"],))}
    return render_template("admin/teams.html", admin_tab="teams",
                           teams=un.list_teams(db, g.unit["id"]), team_counts=team_counts,
                           roles=un.list_roles(db, g.unit["id"]), role_counts=role_counts)


@bp.post("/teams")
def team_add():
    try:
        un.add_team(get_db(), g.unit["id"], request.form.get("name", ""))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams", ok="Team added."))


@bp.post("/teams/<int:tid>/rename")
def team_rename(tid):
    try:
        un.rename_team(get_db(), g.unit["id"], tid, request.form.get("name", ""), request.form.get("color"))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams", ok="Team renamed."))


@bp.post("/teams/<int:tid>/move")
def team_move(tid):
    try:
        un.move_team(get_db(), g.unit["id"], tid, request.form.get("dir", ""))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams"))


@bp.post("/teams/<int:tid>/delete")
def team_delete(tid):
    un.delete_team(get_db(), g.unit["id"], tid)
    return redirect(aurl("teams", ok="Team deleted; its positions are now unassigned."))


@bp.post("/roles")
def role_add():
    try:
        un.add_role(get_db(), g.unit["id"], request.form.get("name", ""))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams", ok="Role added."))


@bp.post("/roles/<int:rid>/rename")
def role_rename(rid):
    try:
        un.rename_role(get_db(), g.unit["id"], rid, request.form.get("name", ""))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams", ok="Role renamed."))


@bp.post("/roles/<int:rid>/move")
def role_move(rid):
    try:
        un.move_role(get_db(), g.unit["id"], rid, request.form.get("dir", ""))
    except un.UnitError as exc:
        return redirect(aurl("teams", err=str(exc)))
    return redirect(aurl("teams"))


@bp.post("/roles/<int:rid>/delete")
def role_delete(rid):
    un.delete_role(get_db(), g.unit["id"], rid)
    return redirect(aurl("teams", ok="Role deleted; positions that had it now have none."))
