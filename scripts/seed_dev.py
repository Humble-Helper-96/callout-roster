#!/usr/bin/env python3
"""Fill the default unit with fake people and teams for local work.

    AUTH_MODE=dev python -m scripts.seed_dev

Stands in for the Authentik sync: inserts made-up people (fictional 555-01xx
numbers) as if they had been pulled from the Metro SWAT group, then places them
on Command, Blue and Red. DEV_USERNAME gets position D-14 so the dev login has
a row to respond on. Safe to re-run.
"""
from __future__ import annotations

import os

from app.config import load_config
from app.db import DB_FILENAME, connect, init_db
from app.people import upsert_person
from app.roster import import_csv
from app.units import ensure_default_unit, get_unit_by_slug

PEOPLE = [  # username, name, phone
    ("u10001", "Harlan Mbeki", "555-0101"),
    ("u10002", "Priya Okafor", "555-0102"),
    ("u10003", "Tomas Lindqvist", "555-0103"),
    ("u10010", "Dana Whitfield", "555-0110"),
    ("u10011", "Marcus Reyes", "555-0111"),
    ("u10012", "Keiko Tanaka", "555-0112"),
    ("u10013", "Evan Sorensen", "555-0113"),
    ("{dev}", "Dev User", "555-0114"),
    ("u10020", "Aisha Rahman", "555-0120"),
    ("u10021", "Grant Holloway", "555-0121"),
    ("u10022", "Lena Vasquez", "555-0122"),
    ("u10023", "Noah Petersen", "555-0123"),
    ("u10024", "Samir Haddad", "555-0124"),
    ("u10025", "Jordan Blake", "555-0125"),
    ("u10030", "Casey Morgan", "555-0130"),
    ("u10031", "Unplaced Person", "555-0131"),
]

CSV = """position,team,username,role,sort_order,active
D-1,SWAT Command,u10001,Team Leader,0,yes
D-2,SWAT Command,u10002,Assistant Team Leader,1,yes
D-3,SWAT Command,u10003,,2,yes
D-10,SWAT Blue Team,u10010,Team Leader,0,yes
D-11,SWAT Blue Team,u10011,Assistant Team Leader,1,yes
D-12,SWAT Blue Team,u10012,Breacher,2,yes
D-13,SWAT Blue Team,u10013,Sniper,3,yes
D-14,SWAT Blue Team,{dev},Medic,4,yes
D-15,SWAT Blue Team,,Entry,5,yes
D-20,SWAT Red Team,u10020,Team Leader,0,yes
D-21,SWAT Red Team,u10021,Assistant Team Leader,1,yes
D-22,SWAT Red Team,u10022,Breacher,2,yes
D-23,SWAT Red Team,u10023,Sniper,3,yes
D-24,SWAT Red Team,u10024,Medic,4,yes
D-25,SWAT Red Team,u10025,Entry,5,no
N-1,CNT,u10030,Negotiator,0,yes
"""


def main() -> None:
    cfg = load_config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    path = os.path.join(cfg.data_dir, DB_FILENAME)
    init_db(path)
    db = connect(path)
    try:
        ensure_default_unit(db, cfg)
        unit = get_unit_by_slug(db, cfg.default_unit_slug)
        groups = [unit["member_group"]]
        for username, name, phone in PEOPLE:
            username = username.format(dev=cfg.dev_username)
            upsert_person(db, username=username, display_name=name, phone=phone, source="authentik",
                          groups=groups)
        summary = import_csv(db, unit["id"], CSV.format(dev=cfg.dev_username), mode="merge")
    finally:
        db.close()
    print(f"seeded unit '{unit['name']}' in {path}: {len(PEOPLE)} people, "
          f"{summary['added']} positions added, {summary['updated']} updated")


if __name__ == "__main__":
    main()
