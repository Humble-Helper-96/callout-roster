"""SQLite access and self-applying schema migrations.

One database file lives in DATA_DIR. The schema is versioned with SQLite's
``user_version`` pragma; on start every migration above the stored version
runs inside a transaction, so a fresh file and an older file both end up at
the current schema with no manual step.

The database holds real data: a migration changes tables in place (ALTER,
or copy into a new table and rename), never drops and recreates them.
Before migrating an existing file, a copy is saved next to it as
``callout_roster.sqlite3.bak-v<old version>``.
"""
from __future__ import annotations

import logging
import sqlite3

from flask import current_app, g

DB_FILENAME = "callout_roster.sqlite3"

log = logging.getLogger("calloutroster.db")

MIGRATIONS: list[str] = [
    # ---- v1: initial schema ------------------------------------------------
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE units (
        id                INTEGER PRIMARY KEY,
        slug              TEXT    NOT NULL UNIQUE,
        name              TEXT    NOT NULL,
        member_group      TEXT    NOT NULL,
        admin_group       TEXT    NOT NULL,
        callout_open_role TEXT,                        -- NULL = app default
        tak_settings      TEXT    NOT NULL DEFAULT '{}',
        active            INTEGER NOT NULL DEFAULT 1,
        created_at        TEXT    NOT NULL
    );

    -- Everyone the app knows about. Filled by the Authentik sync, by people
    -- signing in through the proxy, or by hand. Identity = Authentik username.
    CREATE TABLE people (
        id            INTEGER PRIMARY KEY,
        username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
        display_name  TEXT    NOT NULL DEFAULT '',
        email         TEXT    NOT NULL DEFAULT '',
        phone         TEXT    NOT NULL DEFAULT '',      -- as synced from Authentik
        phone_app     TEXT    NOT NULL DEFAULT '',      -- typed in the app; wins over the synced one
        authentik_pk  INTEGER,
        active        INTEGER NOT NULL DEFAULT 1,      -- 0 = no longer in any synced group
        source        TEXT    NOT NULL DEFAULT 'manual', -- authentik | header | manual
        first_seen_at TEXT    NOT NULL,
        last_seen_at  TEXT    NOT NULL
    );

    CREATE TABLE people_groups (
        person_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
        group_name TEXT    NOT NULL,
        PRIMARY KEY (person_id, group_name)
    );
    CREATE INDEX people_groups_name ON people_groups(group_name);

    CREATE TABLE teams (
        id         INTEGER PRIMARY KEY,
        unit_id    INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        name       TEXT    NOT NULL,
        color      TEXT    NOT NULL DEFAULT '',     -- #rrggbb for headers; blank = accent
        sort_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (unit_id, name)
    );

    CREATE TABLE roles (
        id         INTEGER PRIMARY KEY,
        unit_id    INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        name       TEXT    NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        UNIQUE (unit_id, name)
    );

    -- One row per position on a team. person_id NULL = vacant slot.
    CREATE TABLE roster (
        id         INTEGER PRIMARY KEY,
        unit_id    INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        person_id  INTEGER REFERENCES people(id) ON DELETE SET NULL,
        position   TEXT    NOT NULL,
        team_id    INTEGER REFERENCES teams(id) ON DELETE SET NULL,
        role_id    INTEGER REFERENCES roles(id) ON DELETE SET NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active     INTEGER NOT NULL DEFAULT 1,
        UNIQUE (unit_id, position)
    );
    CREATE INDEX roster_unit_person ON roster(unit_id, person_id);

    CREATE TABLE callouts (
        id             INTEGER PRIMARY KEY,
        unit_id        INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
        label          TEXT    NOT NULL,
        brief          TEXT    NOT NULL DEFAULT '', -- tactical brief: free-text synopsis
        opened_by      TEXT    NOT NULL,
        opened_at      TEXT    NOT NULL,
        expires_at     TEXT    NOT NULL,
        closed_at      TEXT,
        closed_by      TEXT,
        close_reason   TEXT,                          -- 'manual' | 'expired'
        lat            REAL,
        lon            REAL,
        location_label TEXT
    );
    CREATE INDEX callouts_unit_open ON callouts(unit_id, closed_at);

    CREATE TABLE responses (
        id         INTEGER PRIMARY KEY,
        callout_id INTEGER NOT NULL REFERENCES callouts(id) ON DELETE CASCADE,
        roster_id  INTEGER NOT NULL REFERENCES roster(id)   ON DELETE CASCADE,
        status     TEXT    NOT NULL,                  -- not_contacted | no_reply | acknowledged | en_route | on_scene | cleared | unavailable
        first_at   TEXT    NOT NULL,
        last_at    TEXT    NOT NULL,
        eta_at     TEXT,                              -- only meaningful while en_route
        set_by     TEXT    NOT NULL DEFAULT '',       -- username who last set it (self or a lead)
        UNIQUE (callout_id, roster_id)
    );

    CREATE TABLE events (
        id         INTEGER PRIMARY KEY,
        unit_id    INTEGER NOT NULL,
        callout_id INTEGER,
        type       TEXT    NOT NULL,
        actor      TEXT,
        payload    TEXT    NOT NULL DEFAULT '{}',
        created_at TEXT    NOT NULL
    );
    CREATE INDEX events_callout ON events(callout_id, id);
    """,
    # ---- v2: one Authentik group per unit; admin pages use ADMIN_PASSWORD -----
    """
    ALTER TABLE units DROP COLUMN admin_group;
    """,
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; explicit BEGIN where needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def backup_to(conn: sqlite3.Connection, dest: str) -> None:
    """Consistent copy of a live (WAL-mode) database via SQLite's backup API."""
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if 0 < version < len(MIGRATIONS):
            dest = f"{path}.bak-v{version}"
            backup_to(conn, dest)
            log.warning("database schema v%d -> v%d; saved a copy as %s", version, len(MIGRATIONS), dest)
        for target, sql in enumerate(MIGRATIONS, start=1):
            if target <= version:
                continue
            # executescript() commits any pending transaction first, so the
            # BEGIN/COMMIT pair has to live inside the script itself.
            try:
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {target};\nCOMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


class transaction:
    """``with transaction(db):`` runs the block atomically."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def get_meta(db, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db, key: str, value: str) -> None:
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
               (key, value))
