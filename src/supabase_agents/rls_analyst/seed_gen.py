"""Writes supabase/migrations/20261029120100_analyst_seed.sql: deterministic fictional Courtline data.

    uv run python -m supabase_agents.rls_analyst.seed_gen
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..core.config import PROJECT_ROOT
from .users import CLUBS, NAMED, uid

OUT = PROJECT_ROOT / "supabase" / "migrations" / "20261029120100_analyst_seed.sql"
FIRST = ["Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Gray", "Harper", "Indigo", "Jules", "Kai", "Logan",
         "Marlow", "Nico", "Oakley", "Parker", "Quinn", "Reese", "Sage", "Tatum", "Umi", "Vale", "Wren", "Xen", "Yael", "Zion"]
LAST = ["Abara", "Brandt", "Castillo", "Dunmore", "Esposito", "Faulk", "Garrow", "Hollis", "Iverson", "Jansen", "Kowal",
        "Lindgren", "Marsh", "Nakamura", "Ostrow", "Pruitt", "Quade", "Rourke", "Sandoval", "Thorne", "Underhill", "Vance"]
TIERS = {"basic": 3500, "premium": 7500, "junior": 2000}


def q(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def ins(table: str, cols: list[str], rows: list[tuple]) -> str:
    if not rows:
        return ""
    vals = ",\n".join("(" + ", ".join(q(v) for v in r) + ")" for r in rows)
    return f"insert into analyst.{table} ({', '.join(cols)}) values\n{vals};\n\n"


def build() -> str:
    rng = random.Random(42)
    clubs = [(cid, n, c, s) for cid, (n, c, s) in CLUBS.items()]
    profiles, memberships, contacts = [], [], []
    for handle, (name, clubs_roles) in NAMED.items():
        profiles.append((uid(handle), name))
        for cid, role in clubs_roles:
            memberships.append((uid(handle), cid, role, "premium" if role == "staff" else "basic", "2025-03-01"))
            contacts.append((uid(handle), cid, f"{handle}@{['', 'riverside', 'peachtree', 'midtown', 'lakeside'][cid]}.example",
                             f"404-555-01{10 + len(contacts):02d}"))
    n = 0
    for cid in CLUBS:
        for i in range(22):
            n += 1
            handle = f"m{cid}_{i}"
            name = f"{FIRST[(n * 7) % len(FIRST)]} {LAST[(n * 5 + cid) % len(LAST)]}"
            profiles.append((uid(handle), name))
            role = "staff" if i < 2 else "player"
            tier = "premium" if role == "staff" else rng.choice(["basic", "basic", "premium", "junior"])
            joined = date(2024, 1, 1) + timedelta(days=rng.randint(0, 600))
            memberships.append((uid(handle), cid, role, tier, joined.isoformat()))
            slug = name.lower().replace(" ", ".")
            contacts.append((uid(handle), cid, f"{slug}@mail{cid}.example", f"{['', '404', '678', '470', '770'][cid]}-555-{1000 + n:04d}"))
    courts, cid_courts = [], {}
    court_id = 0
    for cid, (_, _, focus) in CLUBS.items():
        for k in range(4 if cid in (1, 3) else 6):
            court_id += 1
            sport = focus if k < 3 else ("pickleball" if focus == "tennis" else "tennis")
            courts.append((court_id, cid, f"Court {k + 1}", sport, rng.choice(["hard", "clay", "sport court"]), k == 0))
            cid_courts.setdefault(cid, []).append(court_id)
    members_by_club: dict[int, list[str]] = {}
    for u, cid, role, tier, joined in memberships:
        members_by_club.setdefault(cid, []).append(u)
    bookings, payments = [], []
    bid = pid = 0
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for day in range(91):  # Aug 1 .. Oct 30, 2026
        for cid in CLUBS:
            for _ in range(rng.randint(2, 6 if cid in (1, 2) else 4)):
                bid += 1
                u = rng.choice(members_by_club[cid])
                ct = rng.choice(cid_courts[cid])
                hour = rng.randint(7, 20)
                when = start + timedelta(days=day, hours=hour)
                dur = rng.choice([60, 60, 90, 120])
                price = {60: 2400, 90: 3400, 120: 4400}[dur] + (600 if ct == cid_courts[cid][0] else 0)
                status = rng.choices(["confirmed", "cancelled", "no_show"], [86, 9, 5])[0]
                bookings.append((bid, cid, ct, u, when.isoformat(), dur, price, status))
                if status != "cancelled":
                    pid += 1
                    payments.append((pid, cid, u, bid, "booking", price, (when - timedelta(hours=2)).isoformat()))
    tier_of = {(u, cid): tier for u, cid, role, tier, joined in memberships}
    for month in (8, 9, 10):
        for (u, cid), tier in sorted(tier_of.items()):
            pid += 1
            payments.append((pid, cid, u, None, "membership", TIERS[tier], datetime(2026, month, 1, 9, tzinfo=timezone.utc).isoformat()))
    leagues, entries = [], []
    lid = 0
    for cid, (cname, _, focus) in CLUBS.items():
        for lname in ("Fall Singles", "Fall Doubles"):
            lid += 1
            leagues.append((lid, cid, f"{lname} {focus.title()}", focus, "Fall 2026"))
            for u in rng.sample(members_by_club[cid], 8):
                w = rng.randint(0, 9)
                entries.append((lid, u, cid, w, rng.randint(0, 9)))
                pid += 1
                payments.append((pid, cid, u, None, "league", 4000, datetime(2026, 9, 5, 12, tzinfo=timezone.utc).isoformat()))
    sql = "-- Generated by supabase_agents.rls_analyst.seed_gen (fictional data, seed 42). Do not edit by hand.\n\n"
    sql += ins("clubs", ["id", "name", "city", "sport_focus"], clubs)
    sql += ins("profiles", ["user_id", "display_name"], profiles)
    sql += ins("memberships", ["user_id", "club_id", "role", "tier", "joined_on"], memberships)
    sql += ins("contacts", ["user_id", "club_id", "email", "phone"], contacts)
    sql += ins("courts", ["id", "club_id", "name", "sport", "surface", "indoor"], courts)
    sql += ins("bookings", ["id", "club_id", "court_id", "user_id", "starts_at", "duration_min", "price_cents", "status"], bookings)
    sql += ins("payments", ["id", "club_id", "user_id", "booking_id", "kind", "amount_cents", "paid_at"], payments)
    sql += ins("leagues", ["id", "club_id", "name", "sport", "season"], leagues)
    sql += ins("league_entries", ["league_id", "user_id", "club_id", "wins", "losses"], entries)
    return sql


if __name__ == "__main__":
    OUT.write_text(build())
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")
