"""Create (or recreate) the `health` schema and fill it with deterministic fictional data.

reset(): drop schema health, apply supabase/migrations/*_health_seed_schema.sql, insert data, ANALYZE.
Data: 6 clubs, 3,001 memberships (60 staff), 36 courts, 30 coaches, 120 announcements, 12 leagues,
600 lesson notes, 20,000 payments and 200,000 bookings over ~10 months. Ids are md5-derived so every
run produces the same rows.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import PROJECT_ROOT
from ..core.db import connect
from . import SCHEMA

SEED_MIGRATION = next(PROJECT_ROOT.joinpath("supabase", "migrations").glob("*_health_seed_schema.sql"))
N_BOOKINGS = 200_000

DATA_SQL = f"""
insert into {SCHEMA}.clubs
  select md5('club' || i)::uuid, 'Courtline Club ' || i,
         (array['Austin','Denver','Tampa','Raleigh','Portland','Columbus'])[i]
  from generate_series(1, 6) i;
insert into {SCHEMA}.members
  select md5('member' || i)::uuid, md5('club' || ((i - 1) % 6 + 1))::uuid, md5('user' || i)::uuid,
         case when i <= 60 then 'staff' else 'player' end, 'Member ' || i
  from generate_series(1, 3000) i;
-- user 7 also plays at club 3
insert into {SCHEMA}.members values (md5('member3001')::uuid, md5('club3')::uuid, md5('user7')::uuid, 'player', 'Member 7 (club 3)');
insert into {SCHEMA}.courts
  select md5('court' || i)::uuid, md5('club' || ((i - 1) % 6 + 1))::uuid, 'Court ' || ((i - 1) / 6 + 1),
         (array['hard','clay','pickleball'])[i % 3 + 1]
  from generate_series(1, 36) i;
insert into {SCHEMA}.coaches
  select md5('coach' || i)::uuid, md5('club' || ((i - 1) % 6 + 1))::uuid, 'Coach ' || i from generate_series(1, 30) i;
insert into {SCHEMA}.announcements (club_id, title, body)
  select md5('club' || ((i - 1) % 6 + 1))::uuid, 'Notice ' || i, 'Court resurfacing schedule ' || i from generate_series(1, 120) i;
insert into {SCHEMA}.leagues
  select md5('league' || i)::uuid, md5('club' || ((i - 1) % 6 + 1))::uuid, 'League ' || i,
         (array['3.0','4.0'])[i % 2 + 1]
  from generate_series(1, 12) i;
-- club c's members are member (c + 6k), its courts court (c + 6k), its coaches coach (c + 6k)
insert into {SCHEMA}.lesson_notes (club_id, member_id, coach_id, note)
  select md5('club' || c)::uuid, md5('member' || (c + 6 * ((i * 31) % 500)))::uuid,
         md5('coach' || (c + 6 * (i % 5)))::uuid, 'Work on second serve, lesson ' || i
  from generate_series(1, 600) i, lateral (select (i - 1) % 6 + 1 as c) x;
insert into {SCHEMA}.payments (club_id, member_id, amount_cents, status, created_at)
  select md5('club' || c)::uuid, md5('member' || (c + 6 * ((i * 7919) % 500)))::uuid,
         2500 + (i * 37) % 9000, (array['paid','paid','paid','refunded','failed'])[i % 5 + 1],
         timestamptz '2026-01-01' + ((i * 104729) % (300 * 24)) * interval '1 hour'
  from generate_series(1::bigint, 20000) i, lateral (select (i - 1) % 6 + 1 as c) x;
insert into {SCHEMA}.bookings (club_id, court_id, member_id, start_time, end_time, status)
  select md5('club' || c)::uuid, md5('court' || (c + 6 * (i % 6)))::uuid,
         md5('member' || (c + 6 * ((i * 7919) % 500)))::uuid,
         t, t + interval '1 hour', (array['confirmed','confirmed','confirmed','cancelled'])[i % 4 + 1]
  from generate_series(1::bigint, {N_BOOKINGS}) i,
       lateral (select (i - 1) % 6 + 1 as c,
                       timestamptz '2026-01-01 07:00' + ((i * 104729) % 300) * interval '1 day' + (i % 14) * interval '1 hour' as t) x;
"""


def reset(verbose: bool = False) -> None:
    with connect(autocommit=True) as conn:
        conn.execute(f"drop schema if exists {SCHEMA} cascade")
        conn.execute(SEED_MIGRATION.read_text())
        with conn.transaction():
            for stmt in [s for s in DATA_SQL.split(";\n") if s.strip()]:
                conn.execute(stmt)
        conn.execute(f"analyze verbose {SCHEMA}.bookings" if verbose else "select 1")
        for t in ("clubs", "members", "courts", "coaches", "announcements", "bookings", "payments", "leagues", "lesson_notes"):
            conn.execute(f"analyze {SCHEMA}.{t}")


def probe_users() -> dict[str, str]:
    """Test users for RLS probes: name -> auth user id."""
    with connect() as conn:
        md5 = lambda s: conn.execute("select md5(%s)::uuid::text", (s,)).fetchone()[0]  # noqa: E731
        return {
            "staff_club1": md5("user1"),        # member 1 is staff at club 1
            # the club-1 player with the most bookings, so "own rows" checks have rows to check
            "player_club1": conn.execute(
                f"select m.user_id::text from {SCHEMA}.members m join {SCHEMA}.bookings b on b.member_id = m.id "
                f"where m.club_id = md5('club1')::uuid and m.role = 'player' group by m.user_id "
                f"order by count(*) desc, m.user_id limit 1").fetchone()[0],
            "player_club2": md5("user62"),      # member 62 is a player at club 2
            "two_clubs": md5("user7"),          # staff at club 1 and player at club 3
            "outsider": md5("not-a-member"),
        }
