"""Identity from the proxy, never from the app.

The reverse proxy authenticates every request and adds two headers:
``X-authentik-username`` and ``X-authentik-groups`` (pipe-separated), plus
``X-authentik-name`` / ``X-authentik-email`` used to fill in people. This
module turns them into a ``User`` and answers "may this user see or administer
this unit?". There is no login page and no user table. The one password is
the optional ``ADMIN_PASSWORD`` that locks the team-building pages; unlocking
sets a signed session cookie (see ``admin_unlocked``).

``AUTH_MODE=dev`` substitutes a fixed username and group list for local work.
It only ever answers requests addressed to localhost.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field

from flask import abort, current_app, g, request, session
from werkzeug.security import check_password_hash

log = logging.getLogger("calloutroster.auth")

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass(frozen=True)
class User:
    username: str
    groups: frozenset = field(default_factory=frozenset)
    name: str = ""
    email: str = ""

    def in_group(self, name: str) -> bool:
        return bool(name) and name in self.groups


def parse_groups(raw: str | None) -> frozenset:
    return frozenset(p.strip() for p in (raw or "").split("|") if p.strip())


def is_local_host(host_header: str) -> bool:
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [::1]:8080
        host = host.split("]")[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in LOCAL_HOSTS


def user_from_request() -> User | None:
    cfg = current_app.config["APP"]
    if cfg.auth_mode == "dev":
        username, groups = cfg.dev_username, parse_groups(cfg.dev_groups)
    else:
        username = (request.headers.get(cfg.user_header) or "").strip()
        groups = parse_groups(request.headers.get(cfg.groups_header))
    if not username:
        return None
    name = (request.headers.get(cfg.name_header) or "").strip() if cfg.name_header else ""
    email = (request.headers.get(cfg.email_header) or "").strip() if cfg.email_header else ""
    return User(username=username, groups=groups, name=name, email=email)


def csrf_token() -> str:
    """Stateless anti-CSRF token: HMAC of the username under the app secret."""
    cfg = current_app.config["APP"]
    user = g.get("user")
    if user is None:
        return ""
    return hmac.new(cfg.secret_key.encode(), user.username.encode(), hashlib.sha256).hexdigest()[:40]


def check_csrf() -> None:
    sent = request.form.get("_csrf", "")
    if not sent or not hmac.compare_digest(sent, csrf_token()):
        abort(400, "The form token was missing or stale. Reload the page and try again.")


def authenticate_request() -> None:
    """``before_request`` hook. Public paths: /healthz and bundled static files."""
    if request.path == "/healthz" or request.path.startswith("/static/"):
        return
    cfg = current_app.config["APP"]
    if cfg.auth_mode == "dev" and not is_local_host(request.host):
        abort(403, "AUTH_MODE=dev only answers requests addressed to localhost.")
    user = user_from_request()
    if user is None:
        abort(401, f"Not authenticated: the {cfg.user_header} header is missing.")
    g.user = user
    if request.method == "POST":
        check_csrf()


# ---- unit access -----------------------------------------------------------

def is_member(unit, user: User) -> bool:
    """One Authentik group per unit: being in it is the whole access rule."""
    return user.in_group(unit["member_group"])


def effective_open_role(unit, cfg) -> str:
    return unit["callout_open_role"] or cfg.callout_open_role


def can_manage_callouts(unit, user: User, cfg) -> bool:
    """May open, close early, extend or brief a callout (one setting covers all).

    Open role ``member`` (default): every member. ``admin``: members who have
    unlocked the admin pages (or everyone, when no ADMIN_PASSWORD is set).
    """
    if not is_member(unit, user):
        return False
    return effective_open_role(unit, cfg) == "member" or admin_unlocked()


# ---- admin password lock -------------------------------------------------------

MAX_FAILURES = 5
FAILURE_WINDOW = 600   # seconds; MAX_FAILURES wrong passwords in this window lock the user out for it
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def admin_lock_enabled() -> bool:
    return bool(current_app.config["APP"].admin_password_hash)


def _password_fingerprint() -> str:
    """Changing ADMIN_PASSWORD invalidates every unlock cookie."""
    return hashlib.sha256(current_app.config["APP"].admin_password_hash.encode()).hexdigest()[:16]


def admin_unlocked() -> bool:
    if not admin_lock_enabled():
        return True
    user = g.get("user")
    return (user is not None
            and session.get("admin_user") == user.username
            and session.get("admin_pw") == _password_fingerprint()
            and float(session.get("admin_until", 0)) > time.time())


def locked_out(username: str) -> bool:
    now = time.time()
    with _failures_lock:
        recent = [t for t in _failures.get(username, []) if now - t < FAILURE_WINDOW]
        _failures[username] = recent
        return len(recent) >= MAX_FAILURES


def try_unlock(password: str) -> bool:
    """Check the admin password for the current user and, if right, unlock."""
    cfg = current_app.config["APP"]
    username = g.user.username
    if locked_out(username):
        log.warning("admin unlock refused for %s: too many wrong passwords", username)
        return False
    if not check_password_hash(cfg.admin_password_hash, password or ""):
        with _failures_lock:
            _failures.setdefault(username, []).append(time.time())
        log.warning("admin unlock failed for %s", username)
        return False
    with _failures_lock:
        _failures.pop(username, None)
    session.permanent = True
    session.update(admin_user=username, admin_pw=_password_fingerprint(),
                   admin_until=time.time() + cfg.admin_unlock_hours * 3600)
    log.info("admin unlocked by %s for %sh", username, cfg.admin_unlock_hours)
    return True


def lock_admin() -> None:
    for key in ("admin_user", "admin_pw", "admin_until"):
        session.pop(key, None)
