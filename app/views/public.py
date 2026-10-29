"""What every member of a unit sees: active callouts and each callout's responders by team."""
from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request

from .. import callouts as co
from ..db import get_db
from ..hooks import list_events
from ..record import build_record
from ..timeutil import tzinfo_for
from ..roster import get_row, grouped_by_team, list_rows
from ..units import COORDS_EXAMPLE, list_teams, visible_units
from ..urls import rel
from flask import current_app as _app


def fmt(iso):
    return _app.jinja_env.filters["localtime"](iso)
from .common import attach_unit_loading, back_to, unit_url

bp = Blueprint("public", __name__)
unit_bp = Blueprint("unit", __name__, url_prefix="/u/<slug>")
attach_unit_loading(unit_bp)


@bp.get("/")
def index():
    units = visible_units(get_db(), g.user)
    if units:
        return redirect(rel(f"u/{units[0]['slug']}/"))
    return render_template("index.html", units=units)


def _is_me(row) -> bool:
    return bool(row["tak_username"]) and row["tak_username"].lower() == g.user.username.lower()


def _my_status(db, callout_id: int, rows) -> str | None:
    """The current user's status on a callout, if they have a row and have tapped."""
    responses = co.responses_map(db, callout_id)
    for r in rows:
        if _is_me(r) and r["id"] in responses:
            return responses[r["id"]]["status"]
    return None


@unit_bp.get("/")
def home():
    """Active callouts and the button to start one. Nothing else."""
    db = get_db()
    unit_id = g.unit["id"]
    opens = co.open_callouts(db, unit_id)
    rows = list_rows(db, unit_id, active_only=True)
    return render_template(
        "home.html",
        nav_active="home",
        opens=opens,
        counts={c["id"]: co.team_counts(db, c["id"])["total"] for c in opens},
        my_status={c["id"]: _my_status(db, c["id"], rows) for c in opens},
        on_roster=any(_is_me(r) for r in rows),
        version=co.version(db, unit_id),
    )


@unit_bp.get("/version")
def unit_version():
    """Polled by the home page: {"v": last event id for the unit}."""
    return jsonify(v=co.version(get_db(), g.unit["id"]))


@unit_bp.get("/callouts/<int:cid>/version")
def callout_version(cid):
    """Polled by an open callout page: {"v": last event id for this callout}."""
    _load_callout(cid)
    return jsonify(v=co.version(get_db(), g.unit["id"], cid))


@unit_bp.get("/callouts")
def history():
    db = get_db()
    items = co.list_history(db, g.unit["id"])
    return render_template("callouts.html", nav_active="history", items=items,
                           counts={c["id"]: co.team_counts(db, c["id"])["total"] for c in items})


@unit_bp.route("/callouts/new", methods=["GET", "POST"])
def callout_new():
    if not g.can_manage:
        abort(403, "Starting a callout needs the admin password here. Unlock Admin first.")
    db = get_db()
    cfg = current_app.config["APP"]
    form = {"label": "", "brief": "", "coords": "", "location_label": ""}
    error = None
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in form}
        try:
            callout = co.open_callout(db, cfg, g.unit, g.user.username, form["label"],
                                      form["coords"], form["location_label"], form["brief"])
        except co.CalloutError as exc:
            error = str(exc)
        else:
            return back_to(f"callouts/{callout['id']}", ok=f"Callout “{callout['label']}” started.")
    return render_template(
        "callout_new.html",
        nav_active="home",
        form=form,
        error=error,
        coords_example=COORDS_EXAMPLE,
        open_count=len(co.open_callouts(db, g.unit["id"])),
    ), (400 if error else 200)


STATUS_RANK = {"on_scene": 0, "en_route": 1, "acknowledged": 2, "cleared": 3, "unavailable": 4, "no_reply": 5, "not_contacted": 6, None: 6}


@unit_bp.get("/callouts/<int:cid>")
def callout_detail(cid):
    """One callout: your status buttons, then every team with each member's status."""
    db = get_db()
    callout = co.get_callout(db, g.unit["id"], cid)
    if callout is None:
        abort(404, "No such callout.")
    teams = list_teams(db, g.unit["id"])
    rows = [r for r in list_rows(db, g.unit["id"], active_only=True) if r["person_id"]]
    responses = co.responses_map(db, cid)
    counts = co.team_counts(db, cid)
    by_team = []
    for team, members in grouped_by_team(rows, teams):
        members = sorted(members, key=lambda r: (STATUS_RANK[responses[r["id"]]["status"] if r["id"] in responses else None],))
        tc = counts["teams"].get(team["id"] if team else None)
        by_team.append((team, members, tc))
    return render_template(
        "callout_detail.html",
        nav_active="home",
        callout=callout,
        by_team=by_team,
        total=counts["total"],
        my_rows=[r for r in rows if _is_me(r)],
        responses=responses,
        ask_eta=request.args.get("eta") == "me",
        eta_row=_eta_row_for_marker(rows, responses),
        can_mark=not callout["closed_at"],
        can_export=True,
        eta_choices=co.ETA_CHOICES,
        events=list_events(db, cid),
        coords_example=COORDS_EXAMPLE,
        version=co.version(db, g.unit["id"], cid),
    )


def _eta_row_for_marker(rows, responses):
    """?eta=<roster_id>: someone just set that person En route, so ask for their ETA."""
    rid = request.args.get("eta", type=int)
    if not rid:
        return None
    for r in rows:
        if r["id"] == rid and r["id"] in responses and responses[r["id"]]["status"] == "en_route":
            return r
    return None


def _load_callout(cid):
    callout = co.get_callout(get_db(), g.unit["id"], cid)
    if callout is None:
        abort(404, "No such callout.")
    return callout


@unit_bp.post("/callouts/<int:cid>/respond")
def respond(cid):
    db = get_db()
    callout = _load_callout(cid)
    row = get_row(db, g.unit["id"], request.form.get("roster_id", type=int) or 0)
    if row is None or not _is_me(row):
        abort(403, "You can only set your own status.")
    status = request.form.get("status", "")
    try:
        co.respond(db, g.unit, callout, row, status, g.user.username)
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    if status == "en_route":
        return back_to(f"callouts/{cid}", eta="me")   # the page then asks for an ETA
    return back_to(f"callouts/{cid}", ok=co.CONFIRMATIONS[status])


@unit_bp.post("/callouts/<int:cid>/status")
def set_status(cid):
    """Any member marks another's status, e.g. Acknowledged after phoning them."""
    db = get_db()
    callout = _load_callout(cid)
    row = get_row(db, g.unit["id"], request.form.get("roster_id", type=int) or 0)
    if row is None or not row["person_id"]:
        abort(404, "No such roster row.")
    status = request.form.get("status", "")
    try:
        co.respond(db, g.unit, callout, row, status, g.user.username)
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    if status == "en_route":
        return back_to(f"callouts/{cid}", eta=row["id"])   # ask the marker for that person's ETA
    return redirect(unit_url(f"callouts/{cid}", ok=f"{row['position']} {row['name']}: {co.STATUSES[status]}.") + f"#row-{row['id']}")


@unit_bp.post("/callouts/<int:cid>/eta")
def eta(cid):
    db = get_db()
    callout = _load_callout(cid)
    row = get_row(db, g.unit["id"], request.form.get("roster_id", type=int) or 0)
    if row is None:
        abort(404, "No such roster row.")
    try:
        eta_at = co.set_eta(db, g.unit, callout, row, request.form.get("minutes", "0"), g.user.username)
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    who = "You are" if _is_me(row) else f"{row['position']} {row['name']}:"
    anchor = "" if _is_me(row) else f"#row-{row['id']}"
    if eta_at is None:
        return redirect(unit_url(f"callouts/{cid}", ok=f"{who} en route.") + anchor)
    return redirect(unit_url(f"callouts/{cid}", ok=f"{who} en route, ETA {fmt(eta_at)}.") + anchor)


def _require_manage():
    if not g.can_manage:
        abort(403, "You are not allowed to start, close or extend callouts.")


@unit_bp.post("/callouts/<int:cid>/close")
def close(cid):
    _require_manage()
    callout = _load_callout(cid)
    try:
        co.close_callout(get_db(), g.unit, callout, g.user.username)
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    return back_to("", ok=f"Callout “{callout['label']}” closed.")


@unit_bp.post("/callouts/<int:cid>/extend")
def extend(cid):
    _require_manage()
    callout = _load_callout(cid)
    cfg = current_app.config["APP"]
    try:
        co.extend_callout(get_db(), cfg, g.unit, callout, g.user.username)
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    return back_to(f"callouts/{cid}", ok=f"Extended by {cfg.callout_extend_hours} hours.")


@unit_bp.get("/callouts/<int:cid>/record.pdf")
def record_pdf(cid):
    """The official record of a callout."""
    callout = _load_callout(cid)
    tz = tzinfo_for(current_app.config["APP"].tz)
    body = build_record(get_db(), g.unit, callout, tz, g.user.username)
    safe = "".join(ch if ch.isalnum() else "-" for ch in callout["label"]).strip("-")[:40] or "callout"
    name = f"{g.unit['slug']}-callout-{callout['id']}-{safe}.pdf"
    return Response(body, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@unit_bp.post("/callouts/<int:cid>/brief")
def brief(cid):
    _require_manage()
    callout = _load_callout(cid)
    try:
        co.set_brief(get_db(), g.unit, callout, g.user.username, request.form.get("brief", ""))
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    return back_to(f"callouts/{cid}", ok="Brief updated.")


@unit_bp.post("/callouts/<int:cid>/location")
def location(cid):
    _require_manage()
    callout = _load_callout(cid)
    try:
        co.set_location(get_db(), g.unit, callout, g.user.username,
                        request.form.get("coords"), request.form.get("location_label", ""))
    except co.CalloutError as exc:
        return back_to(f"callouts/{cid}", err=str(exc))
    return back_to(f"callouts/{cid}", ok="Location updated.")
