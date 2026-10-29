"""Callout Roster application factory."""
from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta

from flask import Flask, g, render_template, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, callouts, people, scheduler
from .config import APP_VERSION, THEME_VERSION, Config, load_config
from .db import DB_FILENAME, close_db, connect, get_db, init_db
from .roster import tel_href
from .timeutil import fmt_clock, fmt_local, fmt_local_full, fmt_remaining, fmt_until, tzinfo_for
from .units import ensure_default_unit
from .urls import PrefixMiddleware, rel


def create_app(config: Config | None = None, run_scheduler: bool = True) -> Flask:
    cfg = config or load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )
    log = logging.getLogger("calloutroster")

    app = Flask(__name__, static_folder="static", static_url_path="/static", template_folder="templates")
    app.url_map.strict_slashes = False
    app.config.update(
        APP=cfg,
        SECRET_KEY=cfg.secret_key,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        DB_PATH=os.path.join(cfg.data_dir, DB_FILENAME),
        # The session cookie only carries the admin unlock (see auth.admin_unlocked).
        SESSION_COOKIE_NAME="callout_roster",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.auth_mode == "proxy",   # the proxy serves HTTPS; dev is plain localhost
        PERMANENT_SESSION_LIFETIME=timedelta(hours=max(cfg.admin_unlock_hours, 1)),
    )

    # ---- database ---------------------------------------------------------------
    os.makedirs(cfg.data_dir, exist_ok=True)
    init_db(app.config["DB_PATH"])
    db = connect(app.config["DB_PATH"])
    try:
        ensure_default_unit(db, cfg)
    finally:
        db.close()

    # ---- proxy awareness ----------------------------------------------------------
    app.wsgi_app = PrefixMiddleware(
        ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1), cfg.base_path
    )

    # ---- request lifecycle ----------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": APP_VERSION}

    app.before_request(auth.authenticate_request)

    @app.before_request
    def _after_auth():
        if g.get("user") is None:
            return
        db = get_db()
        people.touch_from_header(db, g.user)   # the app learns people as they sign in
        callouts.expire_due_throttled(db)

    app.teardown_appcontext(close_db)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        if request.path == "/healthz":
            return exc
        return render_template("error.html", code=exc.code, name=exc.name, description=exc.description), exc.code

    # ---- views ------------------------------------------------------------------------
    from .views.public import bp as public_bp
    from .views.public import unit_bp
    from .views.unit_admin import bp as unit_admin_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(unit_bp)
    app.register_blueprint(unit_admin_bp)

    # ---- templates -----------------------------------------------------------------
    tz = tzinfo_for(cfg.tz)
    app.jinja_env.filters["localtime"] = lambda iso: fmt_local(iso, tz)
    app.jinja_env.filters["localfull"] = lambda iso: fmt_local_full(iso, tz)
    app.jinja_env.filters["remaining"] = fmt_remaining
    app.jinja_env.filters["until"] = fmt_until
    app.jinja_env.filters["clock"] = lambda iso: fmt_clock(iso, tz)
    app.jinja_env.filters["tel"] = tel_href
    app.jinja_env.globals.update(
        rel=rel,
        csrf_token=auth.csrf_token,
        STATUSES=callouts.STATUSES,
        SELF_STATUSES=callouts.SELF_STATUSES,
        DEFAULT_STATUS=callouts.DEFAULT_STATUS,
        STATUS_TONES=callouts.STATUS_TONES,
        APP_VERSION=APP_VERSION,
        THEME_VERSION=THEME_VERSION,
    )

    @app.context_processor
    def _inject():
        return {
            "user": g.get("user"),
            "unit": g.get("unit"),
            "access": g.get("access"),
            "can_manage": g.get("can_manage", False),
            "cfg": cfg,
            "msg_ok": request.args.get("ok"),
            "msg_err": request.args.get("err"),
        }

    # ---- background expiry ------------------------------------------------------------
    if run_scheduler and cfg.expiry_check_seconds > 0:
        scheduler.start(app.config["DB_PATH"], cfg.expiry_check_seconds)
    if run_scheduler and people.authentik_configured(cfg) and cfg.authentik_sync_minutes > 0:
        scheduler.start_authentik_sync(app.config["DB_PATH"], cfg)

    log.info(
        "callout roster v%s starting: auth_mode=%s base_path=%r data_dir=%s tz=%s",
        APP_VERSION, cfg.auth_mode, cfg.base_path, cfg.data_dir, cfg.tz,
    )
    if cfg.admin_password_hash and cfg.secret_key_random:
        log.warning("ADMIN_PASSWORD is set but SECRET_KEY is blank: every restart locks admin again. "
                    "Set SECRET_KEY in .env to keep unlocks across restarts.")
    if not cfg.admin_password_hash:
        log.info("ADMIN_PASSWORD is blank: the admin pages are open to every unit member")
    if cfg.auth_mode == "dev":
        log.warning("AUTH_MODE=dev: pretending every request is %s in groups %s (localhost only)",
                    cfg.dev_username, cfg.dev_groups)
    return app
