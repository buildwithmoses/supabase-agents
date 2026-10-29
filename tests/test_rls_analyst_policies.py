"""Proves the analyst RLS policies themselves, per role, against the local Supabase Postgres.
Skipped when the database isn't running (the guard tests in test_rls_analyst_guard.py run offline)."""

import pytest

from supabase_agents.rls_analyst.users import ASKERS

try:
    from supabase_agents.core.db import connect, user_tx
    with connect(connect_timeout=2) as _c:
        _c.execute("select 1 from analyst.clubs limit 1")
    DB = True
except Exception:  # noqa: BLE001
    DB = False

pytestmark = pytest.mark.skipif(not DB, reason="local Supabase with the analyst schema is not running")


def visible(handle, sql):
    with user_tx(ASKERS[handle]) as conn:
        return conn.execute(sql).fetchall()


def admin(sql, *args):
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def clubs_of(rows):
    return sorted({r[0] for r in rows})


def test_player_sees_only_own_bookings_and_payments():
    me = ASKERS["pat"]
    for t in ("bookings", "payments"):
        rows = visible("pat", f"select club_id, user_id::text from analyst.{t}")
        assert rows and all(u == me for _, u in rows)
        assert len(rows) == admin(f"select count(*) from analyst.{t} where user_id = %s", me)[0][0]


def test_staff_sees_whole_club_and_nothing_else():
    for handle, club in (("alex", 1), ("bianca", 2), ("morgan", 3)):
        for t in ("bookings", "payments", "memberships", "contacts"):
            rows = visible(handle, f"select club_id from analyst.{t}")
            assert clubs_of(rows) == [club], (handle, t)
            assert len(rows) == admin(f"select count(*) from analyst.{t} where club_id = %s", club)[0][0]


def test_two_club_player_sees_own_rows_in_both_clubs_only():
    me = ASKERS["jordan"]
    rows = visible("jordan", "select club_id, user_id::text from analyst.bookings")
    assert all(u == me for _, u in rows)
    assert clubs_of(visible("jordan", "select club_id from analyst.courts")) == [1, 2]


def test_contacts_only_own_for_players():
    rows = visible("pat", "select user_id::text from analyst.contacts")
    assert rows == [(ASKERS["pat"],)]


def test_public_club_data_scoped_to_membership():
    assert clubs_of(visible("pat", "select club_id from analyst.courts")) == [1]
    assert clubs_of(visible("pat", "select club_id from analyst.leagues")) == [1]
    assert len(visible("pat", "select id from analyst.clubs")) == 4  # club names are public to signed-in users


def test_user_with_no_club_sees_nothing_private():
    for t in ("bookings", "payments", "memberships", "contacts", "courts", "leagues"):
        assert visible("casey", f"select 1 from analyst.{t}") == []


def test_anon_has_no_access():
    with connect() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("set local role anon")
            with pytest.raises(Exception):
                conn.execute("select 1 from analyst.bookings")


def test_authenticated_cannot_write():
    import psycopg
    with connect() as conn:
        with conn.transaction(force_rollback=True):
            from supabase_agents.core.db import as_user
            as_user(conn, ASKERS["alex"])
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("update analyst.payments set amount_cents = 0")


def test_executor_rejects_stacked_statements():
    # core.db logs in as the postgres superuser, so a stacked RESET ROLE inside the user's transaction would
    # switch back to postgres. The executor sends one prepared statement, which Postgres refuses to split.
    from supabase_agents.rls_analyst import executor
    r = executor.run("select count(*) from courts; reset role; select email from analyst.contacts", ASKERS["pat"], "user")
    assert r.error and "multiple commands" in r.error
    assert r.rows == []


def test_executor_runs_as_the_asker():
    from supabase_agents.rls_analyst import executor
    r = executor.run("select count(*) from contacts", ASKERS["pat"], "user")
    assert r.identity == {"current_user": "authenticated", "auth_uid": ASKERS["pat"]}
    assert r.rows == [[1]]
