"""Environment-driven configuration.

Everything that differs between a laptop and the server is an
environment variable with a sensible default. See .env.example for the list.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace

from werkzeug.security import generate_password_hash

APP_VERSION = "0.6.0"
THEME_VERSION = "1.0"


def _int(env, key, default):
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{key} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    base_path: str
    data_dir: str
    tz: str
    auth_mode: str
    dev_username: str
    dev_groups: str
    user_header: str
    groups_header: str
    name_header: str
    email_header: str
    default_unit_slug: str
    default_unit_name: str
    default_unit_member_group: str
    authentik_url: str
    authentik_token: str
    authentik_sync_minutes: int
    authentik_phone_attribute: str
    admin_password_hash: str
    admin_unlock_hours: int
    callout_open_role: str
    callout_duration_hours: int
    callout_extend_hours: int
    callout_max_open: int
    expiry_check_seconds: int
    secret_key: str
    secret_key_random: bool
    log_level: str

    def with_(self, **changes) -> "Config":
        return replace(self, **changes)


PASSWORD_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def _admin_password_hash(raw: str) -> str:
    """ADMIN_PASSWORD may be plain text or a Werkzeug hash; either way keep only a hash."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith(PASSWORD_HASH_PREFIXES):
        return raw
    return generate_password_hash(raw)


def load_config(env=None) -> Config:
    env = os.environ if env is None else env

    auth_mode = env.get("AUTH_MODE", "proxy").strip().lower() or "proxy"
    if auth_mode not in ("proxy", "dev"):
        raise SystemExit("AUTH_MODE must be 'proxy' or 'dev'")

    host = env.get("HOST", "0.0.0.0").strip() or "0.0.0.0"
    if auth_mode == "dev":
        # Dev mode fakes the identity headers, so it must never be reachable
        # from anything but this machine. Bind to loopback and (in auth.py)
        # refuse requests whose Host header is not localhost.
        host = "127.0.0.1"

    base_path = env.get("BASE_PATH", "").strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/")

    open_role = env.get("CALLOUT_OPEN_ROLE", "member").strip().lower() or "member"
    if open_role not in ("member", "admin"):
        raise SystemExit("CALLOUT_OPEN_ROLE must be 'member' or 'admin'")

    return Config(
        host=host,
        port=_int(env, "PORT", 8080),
        base_path=base_path,
        data_dir=env.get("DATA_DIR", "./data").strip() or "./data",
        tz=env.get("TZ", "UTC").strip() or "UTC",
        auth_mode=auth_mode,
        dev_username=env.get("DEV_USERNAME", "devuser").strip() or "devuser",
        dev_groups=env.get("DEV_GROUPS", "Metro SWAT"),
        user_header=env.get("AUTH_USER_HEADER", "X-authentik-username").strip(),
        groups_header=env.get("AUTH_GROUPS_HEADER", "X-authentik-groups").strip(),
        # Optional: fill a person's name / email from the proxy when they sign
        # in and the app has none yet. Blank = ignore.
        name_header=env.get("AUTH_NAME_HEADER", "X-authentik-name").strip(),
        email_header=env.get("AUTH_EMAIL_HEADER", "X-authentik-email").strip(),
        default_unit_slug=env.get("DEFAULT_UNIT_SLUG", "swat").strip() or "swat",
        default_unit_name=env.get("DEFAULT_UNIT_NAME", "Metro SWAT").strip() or "Metro SWAT",
        default_unit_member_group=env.get("DEFAULT_UNIT_MEMBER_GROUP", "Metro SWAT").strip(),
        # Authentik REST API, for pulling group members into the people list.
        # Blank URL or token = no sync; people are then learned from the
        # proxy headers as they sign in, or added by hand.
        authentik_url=env.get("AUTHENTIK_URL", "").strip().rstrip("/"),
        authentik_token=env.get("AUTHENTIK_TOKEN", "").strip(),
        authentik_sync_minutes=_int(env, "AUTHENTIK_SYNC_MINUTES", 10),
        authentik_phone_attribute=env.get("AUTHENTIK_PHONE_ATTRIBUTE", "phone").strip() or "phone",
        # Being in a unit's member group is the only access rule. ADMIN_PASSWORD,
        # when set, additionally locks the team-building pages (/u/<slug>/admin/).
        admin_password_hash=_admin_password_hash(env.get("ADMIN_PASSWORD", "")),
        admin_unlock_hours=_int(env, "ADMIN_UNLOCK_HOURS", 12),
        callout_open_role=open_role,
        callout_duration_hours=_int(env, "CALLOUT_DURATION_HOURS", 72),
        callout_extend_hours=_int(env, "CALLOUT_EXTEND_HOURS", 24),
        callout_max_open=_int(env, "CALLOUT_MAX_OPEN", 2),
        expiry_check_seconds=_int(env, "EXPIRY_CHECK_SECONDS", 60),
        # Signs anti-CSRF form tokens and the admin-unlock cookie. A fresh random
        # key per start means open forms must be reloaded and admins unlock again.
        secret_key=env.get("SECRET_KEY", "").strip() or secrets.token_hex(32),
        secret_key_random=not env.get("SECRET_KEY", "").strip(),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
