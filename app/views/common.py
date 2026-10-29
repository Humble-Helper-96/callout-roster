"""Shared unit loading for the ``/u/<slug>/…`` blueprints."""
from __future__ import annotations

from flask import abort, current_app, g, redirect

from ..auth import admin_unlocked, can_manage_callouts, is_member
from ..db import get_db
from ..units import get_unit_by_slug
from ..urls import rel


def attach_unit_loading(bp) -> None:
    @bp.url_value_preprocessor
    def _pull_slug(_endpoint, values):
        g.slug = values.pop("slug", None)

    @bp.before_request
    def _load_unit():
        unit = get_unit_by_slug(get_db(), g.get("slug") or "")
        if unit is None or not unit["active"]:
            abort(404, "No such unit.")
        if not is_member(unit, g.user):
            abort(403, "You are not a member of this unit.")
        g.unit = unit
        g.access = "member"
        g.admin_unlocked = admin_unlocked()
        g.can_manage = can_manage_callouts(unit, g.user, current_app.config["APP"])


def unit_url(path: str = "", **query) -> str:
    return rel(f"u/{g.unit['slug']}/{path}", **query)


def back_to(path: str = "", **query):
    return redirect(unit_url(path, **query))
