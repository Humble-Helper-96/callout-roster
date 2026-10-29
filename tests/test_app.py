import csv
import io
import re
from datetime import timedelta

from app import create_app
from app.callouts import expire_due, open_callouts
from app.db import connect
from app.people import available_for_unit, get_person_by_username, sync_from_authentik, upsert_person
from app.timeutil import from_iso, to_iso, tzinfo_for, utcnow
from app.units import get_unit_by_slug

from conftest import ADMIN, MEMBER, OUTSIDER, make_config, post, token


def person(app, username="u10114", name="Test Member", phone="555-0114", groups=("Metro SWAT",)):
    db = connect(app.config["DB_PATH"])
    try:
        return upsert_person(db, username=username, display_name=name, phone=phone, source="authentik",
                             groups=list(groups))
    finally:
        db.close()


def add_row(client, app, position="D-14", person_id=None, username="u10114", name="Test Member",
            team="", role="", active="on"):
    if person_id is None and username:
        person_id = person(app, username=username, name=name)
    db = connect(app.config["DB_PATH"])
    team_id = db.execute("SELECT id FROM teams WHERE name = ?", (team,)).fetchone()["id"] if team else ""
    role_id = db.execute("SELECT id FROM roles WHERE name = ?", (role,)).fetchone()["id"] if role else ""
    db.close()
    r = post(client, "/u/swat/admin/roster", ADMIN, position=position, person_id=person_id or "",
             team_id=team_id, role_id=role_id, sort_order="0", active=active)
    assert r.status_code == 302, r.data
    return r


def row_id(client, position="D-14"):
    html = client.get("/u/swat/admin/roster", headers=ADMIN).data.decode()
    m = re.search(r'data-roster-id="(\d+)" data-position="' + re.escape(position) + '"', html)
    return m.group(1)


def open_callout(client, label="Barricade Elm St", headers=MEMBER, **extra):
    return post(client, "/u/swat/callouts/new", headers, label=label, **extra)


# ---- the seven rules ------------------------------------------------------------

def test_healthz_needs_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_missing_username_header_is_401(client):
    assert client.get("/").status_code == 401
    assert client.get("/u/swat/").status_code == 401


def test_static_is_bundled_and_public(client):
    assert client.get("/static/theme.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_member_sees_home_and_roster(client):
    r = client.get("/", headers=MEMBER)
    assert r.status_code == 302 and r.headers["Location"].endswith("u/swat/")
    r = client.get("/u/swat/", headers=MEMBER)
    assert r.status_code == 200
    assert b"Metro SWAT" in r.data and b"No active callouts" in r.data
    assert client.get("/u/swat/roster", headers=MEMBER).status_code == 404


def test_outsider_is_403(client):
    assert client.get("/u/swat/", headers=OUTSIDER).status_code == 403
    assert client.get("/u/swat/admin/roster", headers=OUTSIDER).status_code == 403
    r = client.get("/", headers=OUTSIDER)
    assert r.status_code == 200 and b"No access" in r.data


def test_all_links_are_relative(client, app):
    add_row(client, app)
    for path, headers in [("/u/swat/", MEMBER), ("/u/swat/admin/roster", ADMIN),
                          ("/u/swat/admin/people", ADMIN), ("/u/swat/admin/teams", ADMIN)]:
        html = client.get(path, headers=headers).data.decode()
        for attr in re.findall(r'(?:href|src|action)="([^"]+)"', html):
            assert not attr.startswith("/") and not attr.startswith("http"), (path, attr)


def test_base_path_prefix_is_honoured(tmp_path):
    app = create_app(make_config(tmp_path, BASE_PATH="/roster"), run_scheduler=False)
    c = app.test_client()
    assert c.get("/roster/healthz").status_code == 200
    assert c.get("/roster/u/swat/", headers=MEMBER).status_code == 200
    assert c.get("/u/swat/", headers=MEMBER).status_code == 200


def test_dev_mode_only_answers_localhost(tmp_path):
    app = create_app(make_config(tmp_path, AUTH_MODE="dev", DEV_USERNAME="dev", DEV_GROUPS="Metro SWAT"), run_scheduler=False)
    assert app.config["APP"].host == "127.0.0.1"
    c = app.test_client()
    assert c.get("/u/swat/", base_url="http://localhost:8080").status_code == 200
    assert c.get("/u/swat/", base_url="http://roster.example.org").status_code == 403


def test_post_without_csrf_token_is_400(client):
    assert client.post("/u/swat/callouts/new", headers=MEMBER, data={"label": "x"}).status_code == 400


# ---- people -----------------------------------------------------------------------

def test_signing_in_adds_you_to_people(client, app):
    client.get("/u/swat/", headers=MEMBER)
    db = connect(app.config["DB_PATH"])
    p = get_person_by_username(db, "u10114")
    db.close()
    assert p is not None and p["source"] == "header" and p["groups"] == "Metro SWAT"
    html = client.get("/u/swat/admin/people", headers=ADMIN).data.decode()
    assert "u10114" in html and 'data-source="header"' in html and "sync is off" in html


def test_authentik_sync_fills_people_and_marks_leavers(tmp_path):
    cfg = make_config(tmp_path, AUTHENTIK_URL="http://authentik:9000", AUTHENTIK_TOKEN="t")
    app = create_app(cfg, run_scheduler=False)
    db = connect(app.config["DB_PATH"])
    upsert_person(db, username="oldtimer", display_name="Old Timer", source="authentik", groups=["Metro SWAT"])
    unit = get_unit_by_slug(db, "swat")

    fake = {
        "Metro SWAT": [
            {"pk": 7, "username": "u10114", "name": "Test Member", "email": "t@example.org", "is_active": True,
             "attributes": {"phone": "555-0114"}},
            {"pk": 8, "username": "u10020", "name": "Other Person", "is_active": True, "attributes": {}},
        ],
        "Metro SWAT Admins": [   # no longer a unit group, so never fetched
            {"pk": 9, "username": "admin-only", "name": "Admin Only", "is_active": True, "attributes": {}},
        ],
    }
    summary = sync_from_authentik(db, cfg, [unit], fetch=lambda _cfg, group: fake.get(group, []))
    assert summary["people"] == 2 and summary["deactivated"] == 1
    p = get_person_by_username(db, "u10114")
    assert p["display_name"] == "Test Member" and p["phone"] == "555-0114" and p["authentik_pk"] == 7
    assert p["groups"] == "Metro SWAT" and get_person_by_username(db, "admin-only") is None
    assert get_person_by_username(db, "oldtimer")["active"] == 0
    assert [p["username"] for p in available_for_unit(db, unit)] == ["u10020", "u10114"]
    db.close()

    c = app.test_client()
    html = c.get("/u/swat/admin/people", headers=ADMIN).data.decode()
    assert "Synced" in html and "left group" in html and "Sync now" in html


def test_sync_button_reports_unreachable_authentik(tmp_path):
    cfg = make_config(tmp_path, AUTHENTIK_URL="http://127.0.0.1:9", AUTHENTIK_TOKEN="t")
    app = create_app(cfg, run_scheduler=False)
    app.test_client_class = __import__("conftest").BrowserLikeClient
    c = app.test_client()
    r = post(c, "/u/swat/admin/people/sync", ADMIN)
    assert r.status_code == 302 and "Sync+failed" in r.headers["Location"]
    assert b"Last sync failed" in c.get("/u/swat/admin/people", headers=ADMIN).data


def test_manual_person_add(client):
    r = post(client, "/u/swat/admin/people", ADMIN, username="u19999", display_name="Hand Added", phone="555-0199")
    assert r.status_code == 302
    html = client.get("/u/swat/admin/roster", headers=ADMIN).data.decode()
    assert "Hand Added (u19999)" in html  # offered in the pick-list


# ---- team building ------------------------------------------------------------------

def test_lead_places_person_from_pick_list(client, app):
    add_row(client, app, team="SWAT Blue Team", role="Medic")
    html = client.get("/u/swat/admin/roster", headers=ADMIN).data.decode()
    assert "D-14" in html and "Test Member" in html and "Medic" in html and "555-0114" in html
    assert "Test Member (u10114)" not in html  # placed people leave the pick-list

    rid = row_id(client)
    db = connect(app.config["DB_PATH"])
    tl = db.execute("SELECT id FROM roles WHERE name = 'Team Leader'").fetchone()["id"]
    pid = get_person_by_username(db, "u10114")["id"]
    db.close()
    r = post(client, f"/u/swat/admin/roster/{rid}", ADMIN, position="D-14", person_id=pid, team_id="", role_id=tl,
             sort_order="0", active="on")
    assert r.status_code == 302
    assert b"Team Leader" in client.get("/u/swat/admin/roster", headers=ADMIN).data

    post(client, f"/u/swat/admin/roster/{rid}/vacate", ADMIN)
    html = client.get("/u/swat/admin/roster", headers=ADMIN).data.decode()
    assert "Vacant" in html and "Test Member (u10114)" in html  # back in the pick-list


def test_person_cannot_hold_two_positions(client, app):
    add_row(client, app)
    pid = get_person_by_username(connect(app.config["DB_PATH"]), "u10114")["id"]
    r = post(client, "/u/swat/admin/roster", ADMIN, position="D-15", person_id=pid, team_id="", role_id="",
             sort_order="0", active="on")
    assert "already+holds+callsign+D-14" in r.headers["Location"]


def test_duplicate_position_is_rejected(client, app):
    add_row(client, app)
    r = add_row(client, app, username="u10020", name="Other Person")
    assert "already" in r.headers["Location"]


def test_inactive_rows_hidden_from_roster_view(client, app):
    add_row(client, app, position="D-99", username="u10099", name="Hidden Person")
    rid = row_id(client, "D-99")
    post(client, f"/u/swat/admin/roster/{rid}/active", ADMIN, active="0")
    open_callout(client, "Check")
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    assert b"Hidden Person" not in client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data
    assert b"Hidden Person" in client.get("/u/swat/admin/roster", headers=ADMIN).data


def locked_app(tmp_path, **overrides):
    overrides.setdefault("ADMIN_PASSWORD", "correct horse")
    app = create_app(make_config(tmp_path, **overrides), run_scheduler=False)
    app.config["SESSION_COOKIE_SECURE"] = False   # the test client speaks plain http
    app.test_client_class = __import__("conftest").BrowserLikeClient
    return app


def test_admin_password_locks_admin_pages(tmp_path):
    from app import auth
    auth._failures.clear()
    app = locked_app(tmp_path)
    c = app.test_client()
    r = c.get("/u/swat/admin/people", headers=MEMBER)
    assert r.status_code == 302 and r.headers["Location"].endswith("admin/unlock?next=people")
    assert post(c, "/u/swat/admin/teams", MEMBER, name="Sneaky").status_code == 302   # redirected, not saved
    assert b"Sneaky" not in c.get("/u/swat/admin/teams", headers=MEMBER, follow_redirects=True).data
    assert c.get("/u/swat/", headers=MEMBER).status_code == 200                      # callouts stay open

    r = post(c, "/u/swat/admin/unlock", MEMBER, password="wrong", next="people")
    assert r.status_code == 400 and b"Wrong password" in r.data
    r = post(c, "/u/swat/admin/unlock", MEMBER, password="correct horse", next="people")
    assert r.status_code == 302 and "admin/people?ok=Admin+unlocked" in r.headers["Location"]
    assert c.get("/u/swat/admin/people", headers=MEMBER).status_code == 200
    # The unlock belongs to the username that entered the password.
    assert c.get("/u/swat/admin/people", headers=ADMIN).status_code == 302
    post(c, "/u/swat/admin/lock", MEMBER)
    assert c.get("/u/swat/admin/people", headers=MEMBER).status_code == 302


def test_admin_password_accepts_a_hash_and_rate_limits(tmp_path):
    from werkzeug.security import generate_password_hash
    from app import auth
    auth._failures.clear()
    app = locked_app(tmp_path, ADMIN_PASSWORD=generate_password_hash("s3cret"))
    c = app.test_client()
    for _ in range(auth.MAX_FAILURES):
        post(c, "/u/swat/admin/unlock", MEMBER, password="nope")
    r = post(c, "/u/swat/admin/unlock", MEMBER, password="s3cret")
    assert r.status_code == 400 and b"Too many wrong passwords" in r.data
    auth._failures.clear()
    assert post(c, "/u/swat/admin/unlock", MEMBER, password="s3cret").status_code == 302


def test_changing_the_password_relocks(tmp_path):
    from app import auth
    auth._failures.clear()
    app = locked_app(tmp_path)
    c = app.test_client()
    post(c, "/u/swat/admin/unlock", MEMBER, password="correct horse")
    assert c.get("/u/swat/admin/roster", headers=MEMBER).status_code == 200
    from werkzeug.security import generate_password_hash
    app.config["APP"] = app.config["APP"].with_(admin_password_hash=generate_password_hash("new"))
    assert c.get("/u/swat/admin/roster", headers=MEMBER).status_code == 302


def test_roles_are_editable(client):
    r = post(client, "/u/swat/admin/roles", ADMIN, name="Negotiator")
    assert r.status_code == 302
    html = client.get("/u/swat/admin/teams", headers=ADMIN).data.decode()
    assert "Negotiator" in html and "Breacher" in html
    rid = re.search(r'roles/(\d+)/rename">.*?value="Negotiator"', html, re.S).group(1)
    post(client, f"/u/swat/admin/roles/{rid}/rename", ADMIN, name="Crisis Negotiator")
    assert b"Crisis Negotiator" in client.get("/u/swat/admin/teams", headers=ADMIN).data


def test_csv_import_helper_still_used_by_seed(client, app):
    from app.roster import import_csv, RosterImportFailed
    person(app, username="u10020", name="Other Person", phone="555-0120")
    db = connect(app.config["DB_PATH"])
    summary = import_csv(db, 1, "position,team,username,role\nD-20,Gold,u10020,Entry\nD-21,Gold,,Entry\n")
    assert summary["added"] == 2
    try:
        import_csv(db, 1, "position,username\nD-22,nobody_here\n")
    except RosterImportFailed as exc:
        assert "not in the people list" in str(exc)
    else:
        raise AssertionError("unknown username should fail")
    db.close()


# ---- callouts -------------------------------------------------------------------

def test_member_opens_callout_and_responds(client, app):
    add_row(client, app, team="SWAT Blue Team")
    r = open_callout(client, coords="39.1031, -94.5800", location_label="HQ")
    assert r.status_code == 302, r.data
    home = client.get("/u/swat/", headers=MEMBER).data.decode()
    assert "Barricade Elm St" in home and "Tap to respond" in home and "HQ" in home
    cid = re.search(r"callouts/(\d+)", home).group(1)

    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "Your status" in html and "Not contacted" in html and "SWAT Blue Team" in html
    rid = re.search(r'name="roster_id" value="(\d+)"', html).group(1)
    r = post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=rid, status="acknowledged")
    assert r.status_code == 302 and r.headers["Location"].endswith(f"callouts/{cid}?ok=Acknowledged.+Tap+En+route+when+you+start+moving.")
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "Test Member" in html and 'aria-pressed="true"' in html and "SWAT Blue Team" in html
    assert 'href="tel:5550114"' in html
    assert re.search(r"<b>1<small>/\d+</small></b> coming", client.get("/u/swat/", headers=MEMBER).data.decode())

    # En route asks for an ETA; the chips turn minutes into a clock time.
    r = post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=rid, status="en_route")
    assert r.headers["Location"].endswith(f"callouts/{cid}?eta=me")
    html = client.get(f"/u/swat/callouts/{cid}?eta=me", headers=MEMBER).data.decode()
    assert "How long until you arrive" in html and 'value="90"' in html and 'value="5"' in html
    r = post(client, f"/u/swat/callouts/{cid}/eta", MEMBER, roster_id=rid, minutes="30")
    assert r.status_code == 302 and "ETA" in r.headers["Location"]
    db = connect(app.config["DB_PATH"])
    resp = db.execute("SELECT * FROM responses").fetchall()
    assert len(resp) == 1 and resp[0]["status"] == "en_route" and resp[0]["first_at"] <= resp[0]["last_at"]
    eta = from_iso(resp[0]["eta_at"])
    assert timedelta(minutes=29) < eta - utcnow() <= timedelta(minutes=30)
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "ETA" in html and eta.astimezone(tzinfo_for("UTC")).strftime("%H:%M") in html

    # Only the six offered values count, and arriving clears the ETA.
    r = post(client, f"/u/swat/callouts/{cid}/eta", MEMBER, roster_id=rid, minutes="7")
    assert "Pick+one" in r.headers["Location"]
    post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=rid, status="on_scene")
    row = db.execute("SELECT status, eta_at FROM responses").fetchone()
    assert row["status"] == "on_scene" and row["eta_at"] is None
    events = [e["type"] for e in db.execute("SELECT type FROM events ORDER BY id")]
    assert events == ["callout.opened"] + ["callout.response"] * 4
    db.close()


def test_cannot_respond_on_someone_elses_row(client, app):
    add_row(client, app, username="someone_else", name="Someone Else")
    open_callout(client)
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "not on this roster" in html and "Your status" not in html
    r = post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=row_id(client), status="acknowledged")
    assert r.status_code == 403


def test_third_callout_is_refused(client):
    assert open_callout(client, "One").status_code == 302
    assert open_callout(client, "Two").status_code == 302
    r = open_callout(client, "Three")
    assert r.status_code == 400 and b"already open" in r.data


def test_close_and_extend(client, app):
    open_callout(client, "Closable")
    db = connect(app.config["DB_PATH"])
    cid = db.execute("SELECT id, expires_at FROM callouts").fetchone()
    assert post(client, f"/u/swat/callouts/{cid['id']}/extend", MEMBER).status_code == 302
    new = db.execute("SELECT expires_at FROM callouts WHERE id = ?", (cid["id"],)).fetchone()["expires_at"]
    assert new > cid["expires_at"]
    assert post(client, f"/u/swat/callouts/{cid['id']}/close", MEMBER).status_code == 302
    row = db.execute("SELECT * FROM callouts WHERE id = ?", (cid["id"],)).fetchone()
    assert row["closed_at"] and row["close_reason"] == "manual" and row["closed_by"] == "u10114"
    db.close()
    assert b"No active callouts" in client.get("/u/swat/", headers=MEMBER).data
    assert b"Closable" in client.get("/u/swat/callouts", headers=MEMBER).data


def test_expired_callouts_close_on_their_own(client, app):
    open_callout(client, "Old one")
    db = connect(app.config["DB_PATH"])
    db.execute("UPDATE callouts SET expires_at = ?", (to_iso(utcnow() - timedelta(minutes=1)),))
    assert expire_due(db) == 1
    row = db.execute("SELECT * FROM callouts").fetchone()
    assert row["close_reason"] == "expired" and row["closed_by"] == "system"
    assert open_callouts(db, 1) == []
    assert db.execute("SELECT COUNT(*) FROM events WHERE type = 'callout.closed'").fetchone()[0] == 1
    db.close()


def test_open_role_admin_needs_the_unlock(tmp_path):
    from app import auth
    auth._failures.clear()
    app = locked_app(tmp_path, CALLOUT_OPEN_ROLE="admin")
    c = app.test_client()
    assert c.get("/u/swat/callouts/new", headers=MEMBER).status_code == 403
    assert b"Start a callout" not in c.get("/u/swat/", headers=MEMBER).data
    post(c, "/u/swat/admin/unlock", MEMBER, password="correct horse")
    assert c.get("/u/swat/callouts/new", headers=MEMBER).status_code == 200


def test_each_callout_keeps_its_own_responders(client, app):
    add_row(client, app)
    add_row(client, app, position="D-20", username="u10020", name="Other Person")
    other = {"X-authentik-username": "u10020", "X-authentik-groups": "Metro SWAT"}
    assert open_callout(client, "Alpha").status_code == 302
    assert open_callout(client, "Bravo").status_code == 302
    home = client.get("/u/swat/", headers=MEMBER).data.decode()
    bravo, alpha = re.findall(r"callouts/(\d+)", home)[:2]

    def my_row(cid, headers):
        html = client.get(f"/u/swat/callouts/{cid}", headers=headers).data.decode()
        return re.search(r'name="roster_id" value="(\d+)"', html).group(1)

    post(client, f"/u/swat/callouts/{alpha}/respond", MEMBER, roster_id=my_row(alpha, MEMBER), status="acknowledged")
    post(client, f"/u/swat/callouts/{bravo}/respond", other, roster_id=my_row(bravo, other), status="en_route")
    def replied(cid, name):
        html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
        row = re.search(r"<li[^>]*>(?:(?!</li>).)*" + re.escape(name) + r"(?:(?!</li>).)*</li>", html, re.S).group(0)
        return "Not contacted" not in row

    assert replied(alpha, "Test Member") and not replied(alpha, "Other Person")
    assert replied(bravo, "Other Person") and not replied(bravo, "Test Member")


# ---- units ------------------------------------------------------------------------

def test_lead_marks_members_after_phoning_them(client, app):
    add_row(client, app, team="SWAT Blue Team")
    add_row(client, app, position="D-20", username="u10020", name="Other Person", team="SWAT Red Team")
    open_callout(client, "Phone tree")
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)

    # Any member can mark someone else, and the row records who set it.
    html = client.get(f"/u/swat/callouts/{cid}", headers=ADMIN).data.decode()
    assert "mark-sheet" in html and html.count("data-roster-id=") == 2
    r = post(client, f"/u/swat/callouts/{cid}/status", ADMIN, roster_id=row_id(client, "D-20"), status="acknowledged")
    assert r.status_code == 302 and "Other+Person%3A+Acknowledged" in r.headers["Location"]
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    row = re.search(r"<li[^>]*>(?:(?!</li>).)*Other Person(?:(?!</li>).)*</li>", html, re.S).group(0)
    assert "Acknowledged" in row and "by u10001" not in row  # recorded, not shown
    assert re.search(r"<b>1<small>/\d+</small></b> coming", client.get("/u/swat/", headers=MEMBER).data.decode())

    # The member's own later tap takes over and the "by" marker goes away.
    rid = re.search(r'name="roster_id" value="(\d+)"', client.get(f"/u/swat/callouts/{cid}", headers={
        "X-authentik-username": "u10020", "X-authentik-groups": "Metro SWAT"}).data.decode()).group(1)
    post(client, f"/u/swat/callouts/{cid}/respond", {"X-authentik-username": "u10020", "X-authentik-groups": "Metro SWAT"},
         roster_id=rid, status="en_route")
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    row = re.search(r"<li[^>]*>(?:(?!</li>).)*Other Person(?:(?!</li>).)*</li>", html, re.S).group(0)
    assert "En route" in row


def test_tactical_brief_on_create_and_edit(client, app):
    r = open_callout(client, "Brief test", brief="Armed subject barricaded.\nStaging at 5th and C.")
    assert r.status_code == 302
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "Tactical brief" in html and "Armed subject barricaded." in html and "brief-sheet" in html

    r = post(client, f"/u/swat/callouts/{cid}/brief", MEMBER, brief="Subject in custody; stand down.")
    assert r.status_code == 302 and "Brief+updated" in r.headers["Location"]
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "stand down" in html and "Armed subject" not in html
    db = connect(app.config["DB_PATH"])
    assert db.execute("SELECT COUNT(*) FROM events WHERE type = 'callout.brief_changed'").fetchone()[0] == 1
    db.close()


def test_brief_edit_needs_manage_rights(tmp_path):
    from app import auth
    auth._failures.clear()
    app = locked_app(tmp_path, CALLOUT_OPEN_ROLE="admin")
    c = app.test_client()
    post(c, "/u/swat/admin/unlock", ADMIN, password="correct horse")
    assert open_callout(c, "Admin only", headers=ADMIN).status_code == 302
    cid = re.search(r"callouts/(\d+)", c.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    assert "brief-sheet" not in c.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert post(c, f"/u/swat/callouts/{cid}/brief", MEMBER, brief="nope").status_code == 403


def test_lead_sets_en_route_and_eta_for_member(client, app):
    add_row(client, app, position="D-20", username="u10020", name="Other Person", team="SWAT Red Team")
    open_callout(client, "ETA by lead")
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    rid = row_id(client, "D-20")
    r = post(client, f"/u/swat/callouts/{cid}/status", ADMIN, roster_id=rid, status="en_route")
    assert r.headers["Location"].endswith(f"callouts/{cid}?eta={rid}")
    html = client.get(f"/u/swat/callouts/{cid}?eta={rid}", headers=ADMIN).data.decode()
    assert "eta-sheet" in html and "ETA for" in html and "D-20 Other Person" in html
    r = post(client, f"/u/swat/callouts/{cid}/eta", ADMIN, roster_id=rid, minutes="45")
    assert r.status_code == 302 and "D-20+Other+Person%3A+en+route%2C+ETA" in r.headers["Location"]
    assert "ETA" in client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert post(client, f"/u/swat/callouts/{cid}/eta", MEMBER, roster_id=rid, minutes="45").status_code == 302


def test_coordinates_pasted_from_cloudtak(client, app):
    from app.units import parse_coords, UnitError
    assert parse_coords("39.0997, -94.5786") == (39.0997, -94.5786)
    assert parse_coords("  39.099737,-94.578616 ") == (39.099737, -94.578616)
    assert parse_coords("39.0997 -94.5786") == (39.0997, -94.5786)
    assert parse_coords("") == (None, None)
    for bad in ("39.0997", "north, west", "-94.5786, 39.0997", "1, 2, 3"):
        try:
            parse_coords(bad)
        except UnitError:
            pass
        else:
            raise AssertionError(bad)
    r = open_callout(client, "Pasted", coords="39.0997, -94.5786")
    assert r.status_code == 302
    r = open_callout(client, "Bad coords", coords="39.0997")
    assert r.status_code == 400 and b"39.0997, -94.5786" in r.data  # the example is in the message
    html = client.get("/u/swat/callouts/new", headers=MEMBER).data.decode()
    assert 'placeholder="39.0997, -94.5786"' in html and 'name="lat"' not in html


def test_pdf_record_export(client, app):
    add_row(client, app, team="SWAT Blue Team", role="Medic")
    open_callout(client, "Record test", coords="39.0997, -94.5786", location_label="HQ",
                 brief="Armed subject barricaded.")
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    rid = row_id(client)
    post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=rid, status="en_route")
    post(client, f"/u/swat/callouts/{cid}/eta", MEMBER, roster_id=rid, minutes="30")

    r = client.get(f"/u/swat/callouts/{cid}/record.pdf", headers=ADMIN)
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert r.headers["Content-Disposition"].endswith('callout-1-Record-test.pdf"')
    body = r.data
    assert body.startswith(b"%PDF-1.4") and body.rstrip().endswith(b"%%EOF")
    # Content streams are uncompressed, so the record text is greppable.
    for needle in (b"CALLOUT RECORD - Record test", b"Armed subject barricaded.", b"HQ \\(39.099700, -94.578600\\)",
                   b"Test Member", b"Medic", b"En route", b"callout.opened".replace(b"callout.", b"") , b"response"):
        assert needle in body, needle
    # Closed callouts can still be exported and say how they closed.
    post(client, f"/u/swat/callouts/{cid}/close", MEMBER)
    body = client.get(f"/u/swat/callouts/{cid}/record.pdf", headers=ADMIN).data
    assert b"closed by u10114" in body
    assert b"Export record" in client.get(f"/u/swat/callouts/{cid}", headers=ADMIN).data


def test_phone_editable_in_app_and_survives_sync(tmp_path):
    cfg = make_config(tmp_path, AUTHENTIK_URL="http://authentik:9000", AUTHENTIK_TOKEN="t")
    app = create_app(cfg, run_scheduler=False)
    app.test_client_class = __import__("conftest").BrowserLikeClient
    c = app.test_client()
    db = connect(app.config["DB_PATH"])
    unit = get_unit_by_slug(db, "swat")
    fake = {"Metro SWAT": [{"pk": 7, "username": "u10114", "name": "Test Member", "is_active": True,
                          "attributes": {"phone": "555-0114"}}]}
    sync_from_authentik(db, cfg, [unit], fetch=lambda _c, g: fake.get(g, []))
    pid = get_person_by_username(db, "u10114")["id"]

    r = post(c, f"/u/swat/admin/people/{pid}", ADMIN, phone="555-0199")
    assert r.status_code == 302 and "Saved" in r.headers["Location"]
    assert get_person_by_username(db, "u10114")["phone_effective"] == "555-0199"
    html = c.get("/u/swat/admin/people", headers=ADMIN).data.decode()
    assert "555-0199" in html and 'title="Overrides the Authentik number 555-0114"' in html
    html = c.get(f"/u/swat/admin/people?edit={pid}", headers=ADMIN).data.decode()   # no-JS fallback opens the sheet
    assert 'id="person-sheet" class="sheet" aria-labelledby="person-title" open' in html and "Authentik has 555-0114" in html

    sync_from_authentik(db, cfg, [unit], fetch=lambda _c, g: fake.get(g, []))   # sync does not undo it
    assert get_person_by_username(db, "u10114")["phone_effective"] == "555-0199"

    html = c.get(f"/u/swat/admin/people?edit={pid}", headers=ADMIN).data.decode()
    assert "Use Authentik number (555-0114)" in html
    r = post(c, f"/u/swat/admin/people/{pid}", ADMIN, phone="555-0199", revert="1")   # explicit revert button
    assert "using+the+Authentik+number" in r.headers["Location"]
    assert get_person_by_username(db, "u10114")["phone_effective"] == "555-0114"
    post(c, f"/u/swat/admin/people/{pid}", ADMIN, phone="555-0188")
    post(c, f"/u/swat/admin/people/{pid}", ADMIN, phone="")                    # clearing the box also falls back
    assert get_person_by_username(db, "u10114")["phone_effective"] == "555-0114"
    db.close()


def test_without_admin_password_members_do_everything(tmp_path):
    from app.config import load_config
    assert load_config({}).admin_password_hash == ""
    app = create_app(make_config(tmp_path), run_scheduler=False)
    app.test_client_class = __import__("conftest").BrowserLikeClient
    c = app.test_client()
    assert c.get("/u/swat/admin/roster", headers=MEMBER).status_code == 200
    assert b"open to all members" in c.get("/u/swat/admin/people", headers=MEMBER).data
    assert b'u/swat/admin/roster">Admin' in c.get("/u/swat/", headers=MEMBER).data
    pid = upsert_person(connect(app.config["DB_PATH"]), username="u10020", display_name="Other Person",
                        source="authentik", groups=["Metro SWAT"])
    assert post(c, "/u/swat/admin/roster", MEMBER, position="D-20", person_id=pid, team_id="", role_id="",
                sort_order="0", active="on").status_code == 302
    assert post(c, "/u/swat/callouts/new", MEMBER, label="Open").status_code == 302
    cid = re.search(r"callouts/(\d+)", c.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    html = c.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert "mark-sheet" in html and "Export record" in html and "brief-sheet" in html
    rid = re.search(r'data-roster-id="(\d+)"', html).group(1)
    assert post(c, f"/u/swat/callouts/{cid}/status", MEMBER, roster_id=rid, status="acknowledged").status_code == 302
    assert c.get(f"/u/swat/callouts/{cid}/record.pdf", headers=MEMBER).status_code == 200




def test_team_colours_on_headers(client, app):
    html = client.get("/u/swat/admin/teams", headers=ADMIN).data.decode()
    assert 'name="color" value="#3fb950"' in html and 'value="#c8a97e"' in html
    add_row(client, app, team="SWAT Red Team")
    open_callout(client, "Colours")
    cid = re.search(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode()).group(1)
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert 'style="--team:#f85149"' in html and 'style="--team:#f4a261"' in html
    teams_html = client.get("/u/swat/admin/teams", headers=ADMIN).data.decode()
    tid = re.search(r'teams/(\d+)/rename">(?:(?!</form>).)*value="SWAT Red Team"', teams_html, re.S).group(1)
    r = post(client, f"/u/swat/admin/teams/{tid}/rename", ADMIN, name="SWAT Red Team", color="#ff0000")
    assert r.status_code == 302
    assert 'style="--team:#ff0000"' in client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    r = post(client, f"/u/swat/admin/teams/{tid}/rename", ADMIN, name="SWAT Red Team", color="red")
    assert "hex" in r.headers["Location"]


def test_sync_skips_a_failing_group_and_keeps_going(tmp_path):
    from app.people import PeopleError
    from app.units import create_unit
    cfg = make_config(tmp_path, AUTHENTIK_URL="http://authentik:9000", AUTHENTIK_TOKEN="t")
    app = create_app(cfg, run_scheduler=False)
    db = connect(app.config["DB_PATH"])
    create_unit(db, slug="cnt", name="CNT", member_group="tak_Missing")
    upsert_person(db, username="oldtimer", display_name="Old Timer", source="authentik", groups=["Metro SWAT"])

    def fetch(_cfg, group):
        if group == "tak_Missing":
            raise PeopleError("Authentik answered HTTP 400 for group 'tak_Missing'.")
        return [{"pk": 7, "username": "u10114", "name": "Test Member", "is_active": True, "attributes": {}}]

    summary = sync_from_authentik(db, cfg, [dict(u) for u in db.execute("SELECT * FROM units")], fetch=fetch)
    assert summary["people"] == 1 and summary["failed"][0]["group"] == "tak_Missing"
    assert summary["deactivated"] == 0 and get_person_by_username(db, "oldtimer")["active"] == 1
    assert get_person_by_username(db, "u10114")["display_name"] == "Test Member"
    html = app.test_client().get("/u/swat/admin/people", headers=MEMBER).data.decode()
    assert "Skipped tak_Missing" in html

    def all_fail(_cfg, group):
        raise PeopleError(f"Authentik answered HTTP 400 for group '{group}'.")
    try:
        sync_from_authentik(db, cfg, [dict(u) for u in db.execute("SELECT * FROM units")], fetch=all_fail)
    except PeopleError as exc:
        assert "tak_Missing" in str(exc)
    else:
        raise AssertionError("a sync where every group fails should raise")
    db.close()


def test_default_unit_settings_follow_the_environment(tmp_path):
    import logging
    seen = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())
    logging.getLogger("calloutroster.units").addHandler(handler)
    app = create_app(make_config(tmp_path), run_scheduler=False)
    db = connect(app.config["DB_PATH"])
    upsert_person(db, username="u10114", display_name="Kept", source="authentik", groups=["tak_Metro SWAT"])
    db.close()
    app = create_app(make_config(tmp_path, DEFAULT_UNIT_MEMBER_GROUP="tak_Metro SWAT", DEFAULT_UNIT_NAME="SWAT",
                                 DEFAULT_UNIT_SLUG="metro-swat", LOG_LEVEL="WARNING"), run_scheduler=False)
    db = connect(app.config["DB_PATH"])
    unit = get_unit_by_slug(db, "metro-swat")
    assert unit is not None and unit["name"] == "SWAT" and unit["member_group"] == "tak_Metro SWAT"
    assert db.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 1
    assert get_person_by_username(db, "u10114")["display_name"] == "Kept"    # data survives
    db.close()
    logging.getLogger("calloutroster.units").removeHandler(handler)
    assert any("member_group changed from 'Metro SWAT' to 'tak_Metro SWAT'" in m for m in seen)
    member = {"X-authentik-username": "u10114", "X-authentik-groups": "tak_Metro SWAT"}
    assert app.test_client().get("/u/metro-swat/", headers=member).status_code == 200


def test_sign_in_fills_name_and_email_from_headers(client, app):
    db = connect(app.config["DB_PATH"])
    upsert_person(db, username="u10114", source="header", groups=["Metro SWAT"])   # learned before: name = username
    upsert_person(db, username="u10020", display_name="Typed Name", source="manual", groups=["Metro SWAT"])
    db.close()
    client.get("/u/swat/", headers=MEMBER | {"X-authentik-name": "Jane Doe", "X-authentik-email": "jd@example.org"})
    client.get("/u/swat/", headers={"X-authentik-username": "u10020", "X-authentik-groups": "Metro SWAT",
                                    "X-authentik-name": "Header Name"})
    client.get("/u/swat/", headers={"X-authentik-username": "new1", "X-authentik-groups": "Metro SWAT",
                                    "X-authentik-name": "New Person"})
    db = connect(app.config["DB_PATH"])
    p = get_person_by_username(db, "u10114")
    assert p["display_name"] == "Jane Doe" and p["email"] == "jd@example.org"
    assert get_person_by_username(db, "u10020")["display_name"] == "Typed Name"
    assert get_person_by_username(db, "new1")["display_name"] == "New Person"
    db.close()


def test_v1_database_upgrades_in_place(tmp_path):
    import os
    from app.db import DB_FILENAME, MIGRATIONS
    data = tmp_path / "data"
    data.mkdir()
    path = str(data / DB_FILENAME)
    db = connect(path)
    db.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    now = to_iso(utcnow())
    db.execute("INSERT INTO units (slug, name, member_group, admin_group, created_at)"
               " VALUES ('swat', 'Metro SWAT', 'tak_Metro SWAT', 'tak_Metro SWAT', ?)", (now,))
    db.execute("INSERT INTO teams (unit_id, name) VALUES (1, 'SWAT Red Team')")
    db.execute("INSERT INTO people (username, display_name, first_seen_at, last_seen_at)"
               " VALUES ('u10114', 'Real Person', ?, ?)", (now, now))
    db.execute("INSERT INTO people_groups VALUES (1, 'tak_Metro SWAT')")
    db.execute("INSERT INTO roster (unit_id, person_id, position, team_id) VALUES (1, 1, 'D-14', 1)")
    db.execute("INSERT INTO callouts (unit_id, label, opened_by, opened_at, expires_at) VALUES (1, 'Real', 'x', ?, ?)",
               (now, now))
    db.close()

    app = create_app(make_config(tmp_path, DEFAULT_UNIT_MEMBER_GROUP="tak_Metro SWAT"), run_scheduler=False)
    db = connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert "admin_group" not in [r["name"] for r in db.execute("PRAGMA table_info(units)")]
    assert db.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 1
    assert db.execute("SELECT position FROM roster").fetchone()["position"] == "D-14"
    assert db.execute("SELECT label FROM callouts").fetchone()["label"] == "Real"
    db.close()
    backup = connect(path + ".bak-v1")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    assert backup.execute("SELECT admin_group FROM units").fetchone()["admin_group"] == "tak_Metro SWAT"
    backup.close()
    member = {"X-authentik-username": "u10114", "X-authentik-groups": "tak_Metro SWAT"}
    assert b"Real" in app.test_client().get("/u/swat/", headers=member).data
    create_app(make_config(tmp_path, DEFAULT_UNIT_MEMBER_GROUP="tak_Metro SWAT"), run_scheduler=False)
    assert not os.path.exists(path + ".bak-v2")     # no copy when nothing is pending


def test_person_fields_are_searchable_pickers(client, app):
    person(app, username="u10020", name="Other Person")
    html = client.get("/u/swat/admin/roster", headers=ADMIN).data.decode()
    assert re.search(r'<select[^>]*name="person_id"[^>]*data-picker', html)   # app.js turns it into a search box
    assert "Other Person (u10020)</option>" in html                         # still a plain select without JS
    add_row(client, app, position="D-30", person_id=None, username="u10030", name="Third Person")
    html = client.get(f"/u/swat/admin/roster/{row_id(client, 'D-30')}", headers=ADMIN).data.decode()
    assert re.search(r'<select[^>]*name="person_id"[^>]*data-picker', html)


def test_eta_countdown():
    from app.timeutil import fmt_until
    now = utcnow()
    assert fmt_until(to_iso(now + timedelta(minutes=11)), now) == "in 11m"
    assert fmt_until(to_iso(now + timedelta(hours=1, minutes=2)), now) == "in 1h 02m"
    assert fmt_until(to_iso(now - timedelta(minutes=5)), now) == "late 5m"
    assert fmt_until(to_iso(now), now) == "now"
    assert fmt_until("") == ""


def test_home_screen_app(client):
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200 and r.mimetype == "application/manifest+json"
    assert r.json["start_url"] == "../" and r.json["display"] == "standalone"
    for icon in ("icon-180.png", "icon-192.png", "icon-512.png"):
        assert client.get(f"/static/{icon}").mimetype == "image/png"
    html = client.get("/u/swat/", headers=MEMBER).data.decode()
    assert 'rel="manifest" href="../../static/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon" href="../../static/icon-180.png"' in html
    assert 'name="theme-color"' in html and 'apple-mobile-web-app-capable' in html


def test_not_contacted_is_the_default_and_leads_can_mark_no_reply(client, app):
    add_row(client, app, team="SWAT Blue Team")
    add_row(client, app, position="D-20", username="u10020", name="Other Person", team="SWAT Blue Team")
    cid = re.search(r"callouts/(\d+)", open_callout(client, "Default status").headers["Location"]).group(1)
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    # Both lines show the default; the member's own card does not offer the lead-only statuses.
    assert html.count("Not contacted") >= 2
    card = html[html.index("Your status"):html.index('id="teams"')]
    assert 'value="not_contacted"' not in card and 'value="no_reply"' not in card and 'value="en_route"' in card
    sheet = html[html.index('id="mark-sheet"'):]
    assert 'value="not_contacted"' in sheet and 'value="no_reply"' in sheet
    assert '<b>2</b> not contacted' in html

    rid = row_id(client, "D-20")
    r = post(client, f"/u/swat/callouts/{cid}/status", ADMIN, roster_id=rid, status="no_reply")
    assert r.status_code == 302 and "No+reply" in r.headers["Location"]
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert '<b>1</b> not contacted' in html and '<b>1</b> no reply' in html
    row = re.search(r"<li[^>]*>(?:(?!</li>).)*Other Person(?:(?!</li>).)*</li>", html, re.S).group(0)
    assert "No reply" in row and "Not contacted" not in row
    # Back to the default by hand, and the home card agrees.
    post(client, f"/u/swat/callouts/{cid}/status", ADMIN, roster_id=rid, status="not_contacted")
    assert '<b>2</b> not contacted' in client.get("/u/swat/", headers=MEMBER).data.decode()


def test_live_version_moves_with_every_change(client, app):
    add_row(client, app, team="SWAT Blue Team")
    cid = re.search(r"callouts/(\d+)", open_callout(client, "Live").headers["Location"]).group(1)
    v0 = client.get(f"/u/swat/callouts/{cid}/version", headers=MEMBER).json["v"]
    u0 = client.get("/u/swat/version", headers=MEMBER).json["v"]
    assert v0 > 0 and u0 >= v0
    html = client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert f'data-version="{v0}"' in html and f'data-live-url="../../../u/swat/callouts/{cid}/version"' in html
    assert 'data-live="teams"' in html and 'data-live="glance"' in html and 'data-refresh' not in html
    home = client.get("/u/swat/", headers=MEMBER).data.decode()
    assert f'data-version="{u0}"' in home and 'data-live="callouts"' in home

    post(client, f"/u/swat/callouts/{cid}/respond", MEMBER, roster_id=row_id(client), status="acknowledged")
    v1 = client.get(f"/u/swat/callouts/{cid}/version", headers=MEMBER).json["v"]
    assert v1 > v0 and client.get("/u/swat/version", headers=MEMBER).json["v"] >= v1
    # Brief and location edits move it too, so other phones pick them up.
    post(client, f"/u/swat/callouts/{cid}/brief", ADMIN, brief="Updated brief.")
    v2 = client.get(f"/u/swat/callouts/{cid}/version", headers=MEMBER).json["v"]
    assert v2 > v1
    # Closed callouts render without the live attributes, which tells a polling page to stop.
    post(client, f"/u/swat/callouts/{cid}/close", ADMIN)
    assert "data-live-url" not in client.get(f"/u/swat/callouts/{cid}", headers=MEMBER).data.decode()
    assert client.get(f"/u/swat/callouts/{cid}/version", headers=OUTSIDER).status_code in (302, 403)
    assert client.get(f"/u/swat/callouts/999/version", headers=MEMBER).status_code == 404


def test_member_committed_on_one_callout_is_locked_on_the_other(client, app):
    add_row(client, app)
    add_row(client, app, position="D-20", username="u10020", name="Other Person")
    assert open_callout(client, "Alpha").status_code == 302
    assert open_callout(client, "Bravo").status_code == 302
    bravo, alpha = re.findall(r"callouts/(\d+)", client.get("/u/swat/", headers=MEMBER).data.decode())[:2]
    rid = row_id(client)
    post(client, f"/u/swat/callouts/{alpha}/respond", MEMBER, roster_id=rid, status="en_route")

    html = client.get(f"/u/swat/callouts/{bravo}", headers=MEMBER).data.decode()
    row = re.search(r"<li[^>]*>(?:(?!</li>).)*Test Member(?:(?!</li>).)*</li>", html, re.S).group(0)
    assert "elsewhere" in row and "en route @ Alpha" in row and "markable" not in row
    card = html[html.index("Your status"):html.index('id="teams"')]
    assert "at <a" in card and "Alpha" in card and card.count("disabled") == 5
    # Neither the member nor a lead can set a status on Bravo while committed to Alpha.
    r = post(client, f"/u/swat/callouts/{bravo}/respond", MEMBER, roster_id=rid, status="acknowledged")
    assert r.status_code == 302 and "Clear+them+there+first" in r.headers["Location"]
    post(client, f"/u/swat/callouts/{bravo}/status", ADMIN, roster_id=rid, status="on_scene")
    assert '<b>2</b> not contacted' in client.get(f"/u/swat/callouts/{bravo}", headers=MEMBER).data.decode()
    # The other person, not contacted on Alpha, is free on Bravo.
    post(client, f"/u/swat/callouts/{bravo}/status", ADMIN, roster_id=row_id(client, "D-20"), status="acknowledged")
    assert '<b>1</b> not contacted' in client.get(f"/u/swat/callouts/{bravo}", headers=MEMBER).data.decode()
    # Cleared on Alpha frees the member for Bravo.
    post(client, f"/u/swat/callouts/{alpha}/respond", MEMBER, roster_id=rid, status="cleared")
    html = client.get(f"/u/swat/callouts/{bravo}", headers=MEMBER).data.decode()
    assert "@ Alpha" not in html and "disabled" not in html[html.index("Your status"):html.index('id="teams"')]
    post(client, f"/u/swat/callouts/{bravo}/respond", MEMBER, roster_id=rid, status="acknowledged")
    assert '<b>0</b> not contacted' in client.get(f"/u/swat/callouts/{bravo}", headers=MEMBER).data.decode()
