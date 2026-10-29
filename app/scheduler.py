"""Background thread that closes expired callouts even when nobody is looking."""
from __future__ import annotations

import logging
import threading
import time

from . import callouts, people, units
from .db import connect

log = logging.getLogger("calloutroster.scheduler")


def start(db_path: str, interval: int) -> threading.Thread:
    def run():
        log.info("expiry scheduler running every %ss", interval)
        while True:
            time.sleep(interval)
            try:
                db = connect(db_path)
                try:
                    callouts.expire_due(db)
                finally:
                    db.close()
            except Exception:  # keep the thread alive no matter what
                log.exception("expiry check failed")

    t = threading.Thread(target=run, name="callout-expiry", daemon=True)
    t.start()
    return t


def start_authentik_sync(db_path: str, cfg) -> threading.Thread:
    """Pull group members from Authentik now and every AUTHENTIK_SYNC_MINUTES."""
    interval = max(cfg.authentik_sync_minutes, 1) * 60

    def run_once():
        db = connect(db_path)
        try:
            try:
                people.sync_from_authentik(db, cfg, units.list_units(db))
            except people.PeopleError as exc:
                log.warning("authentik sync failed: %s", exc)
                people.record_sync_error(db, str(exc))
        finally:
            db.close()

    def run():
        log.info("authentik sync running every %s min against %s", cfg.authentik_sync_minutes, cfg.authentik_url)
        while True:
            try:
                run_once()
            except Exception:
                log.exception("authentik sync crashed")
            time.sleep(interval)

    t = threading.Thread(target=run, name="authentik-sync", daemon=True)
    t.start()
    return t
