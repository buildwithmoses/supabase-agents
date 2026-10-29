"""Generate realistic query load on `health` so pg_stat_statements has something to rank.

Courtline's app runs these (parameterized, so pg_stat_statements groups them as $1, $2 ...):
  bookings for a club's calendar week  <- the slow one (no index on club_id, start_time)
  a member's own memberships, payments by club and status, courts by club
Only `health` queries are reset first; other schemas' stats are left alone.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from ..core.db import connect
from . import SCHEMA

CALENDAR = (f"select id, court_id, member_id, start_time, end_time, status from {SCHEMA}.bookings "
            "where club_id = %s and start_time >= %s and start_time < %s order by start_time")


def reset_stats(conn) -> int:
    rows = conn.execute(r"select queryid from extensions.pg_stat_statements where query ~ '\mhealth\.'").fetchall()
    for (qid,) in rows:
        conn.execute("select extensions.pg_stat_statements_reset(0, 0, %s)", (qid,))
    return len(rows)


def generate(calendar_calls: int = 120, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    with connect(autocommit=True) as conn:
        reset_stats(conn)
        clubs = [r[0] for r in conn.execute(f"select id from {SCHEMA}.clubs").fetchall()]
        users = [r[0] for r in conn.execute(f"select user_id from {SCHEMA}.members limit 400").fetchall()]
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        for _ in range(calendar_calls):
            wk = start + timedelta(weeks=rnd.randrange(40))
            conn.execute(CALENDAR, (rnd.choice(clubs), wk, wk + timedelta(days=7))).fetchall()
        for _ in range(300):
            conn.execute(f"select id, club_id, role from {SCHEMA}.members where user_id = %s", (rnd.choice(users),)).fetchall()
        for _ in range(150):
            conn.execute(f"select id, amount_cents from {SCHEMA}.payments where club_id = %s and status = %s",
                         (rnd.choice(clubs), rnd.choice(["paid", "failed"]))).fetchall()
        for _ in range(150):
            conn.execute(f"select id, name from {SCHEMA}.courts where club_id = %s", (rnd.choice(clubs),)).fetchall()
    return {"calendar_calls": calendar_calls, "other_calls": 600}
