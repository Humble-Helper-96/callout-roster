"""Relative URL building.

Every link in the HTML is relative, so the app works
at the root or under any sub-path without a rebuild. ``rel('u/swat/')`` returns
a path that climbs from the *current request's* directory back to the app root
and then down to the target, e.g. ``../../u/swat/`` from ``/u/swat/callouts``.
Because it is computed from the real request path it also stays correct when a
page is reached with or without a trailing slash.
"""
from __future__ import annotations

from urllib.parse import urlencode

from flask import request


def rel(path: str = "", **query) -> str:
    current = request.path or "/"
    directory = current.rsplit("/", 1)[0]
    depth = len([seg for seg in directory.split("/") if seg])
    prefix = "../" * depth if depth else "./"
    out = prefix + path.lstrip("/")
    params = {k: v for k, v in query.items() if v is not None and v != ""}
    if params:
        out += "?" + urlencode(params)
    return out


class PrefixMiddleware:
    """Serve the app under BASE_PATH when the proxy forwards the full path.

    If the proxy strips the prefix before forwarding, requests arrive without
    it and pass straight through; relative links make both layouts work.
    """

    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if self.prefix and (path == self.prefix or path.startswith(self.prefix + "/")):
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.app(environ, start_response)
