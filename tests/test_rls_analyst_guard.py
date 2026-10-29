"""Offline guard tests: no database, no keys."""

import pytest

from supabase_agents.rls_analyst.guard import review

OK = [
    "select count(*) from bookings where status = 'confirmed'",
    "select sum(amount_cents) / 100.0 as dollars from analyst.payments where paid_at >= date '2026-10-01'",
    "with m as (select date_trunc('month', paid_at) as month, sum(amount_cents) as c from payments group by 1) select * from m order by month",
    "select c.name, count(b.id) from bookings b join courts c on c.id = b.court_id group by c.name order by 2 desc limit 5",
    "select to_char(starts_at, 'Dy') as dow, count(*) from bookings group by 1",
    "select extract(hour from starts_at) as h, count(*) from bookings group by 1 order by 2 desc",
    "select p.display_name, e.wins from league_entries e join profiles p using (user_id) order by e.wins desc limit 3",
    "select email, phone from contacts",
    "select status, round(100.0 * count(*) / sum(count(*)) over (), 1) from bookings group by status",
    "select coalesce(sum(amount_cents), 0) from payments where kind = 'league'",
    "select round(sum(b.price_cents) / 100.0, 2), count(*) from analyst.bookings b where b.status = 'confirmed' and b.starts_at >= timestamptz '2026-10-01 00:00:00+00' and not b.club_id in (2, 3) or b.duration_min between 60 and 90",
    "select count(*) from bookings where starts_at::date >= current_date - interval '30 days' and status like 'conf%'",
    "select c.name from clubs c where exists (select 1 from courts where courts.club_id = c.id and indoor)",
    "select role, count(*) from memberships group by role",
    "select date_part('dow', starts_at) as d, count(*) filter (where status = 'no_show') from bookings group by 1",
]
BLOCK = [
    "select set_config('request.jwt.claims', '{\"sub\":\"x\"}', true), * from bookings",
    "select current_setting('request.jwt.claims')",
    "select pg_sleep(10) from bookings",
    "select * from auth.users",
    "select * from bookings union all select * from public.something",
    "select * from pg_catalog.pg_authid",
    "select * from information_schema.tables",
    "select analyst.my_staff_club_ids()",
    "select auth.uid() from bookings",
    "delete from payments",
    "with x as (delete from payments returning *) select * from x",
    "select * into tmp from bookings",
    "select * from bookings; drop table analyst.payments",
    "set role service_role",
    "select * from bookings for update",
    "select pg_read_file('/etc/passwd')",
    "select * from dblink('host=x', 'select 1') as t(a int)",
    "select version()",
    "copy payments to stdout",
    "select 1",
    "",
    "select lo_import('/etc/passwd')",
    "select * from analyst.bookings, auth.sessions",
    "do $$ begin perform 1; end $$",
    "select count(*) from courts; reset role; select email from analyst.contacts",
    "select set_config('role', 'postgres', true), (select count(*) from contacts)",
    "reset role",
]


@pytest.mark.parametrize("sql", OK)
def test_allows_reads(sql):
    d = review(sql)
    assert d.ok, d.reasons
    assert "limit" in d.sql_to_run.lower()


@pytest.mark.parametrize("sql", BLOCK)
def test_blocks_bypasses(sql):
    d = review(sql)
    assert not d.ok, sql


def test_limit_enforced_and_capped():
    assert "LIMIT 200" in review("select * from bookings limit 5000").sql_to_run.upper()
    assert "LIMIT 5" in review("select * from bookings limit 5").sql_to_run.upper()


def test_contact_columns_flagged():
    assert review("select email from contacts").contact_columns
    assert review("select * from contacts").contact_columns
    assert not review("select count(*) from contacts").contact_columns


def test_executor_helpers_exist():
    # regression: an edit once deleted is_empty and every partial-visibility case crashed
    from supabase_agents.rls_analyst import executor
    assert executor.is_empty([]) and executor.is_empty([[None]]) and executor.is_empty([[0, None]])
    assert not executor.is_empty([[3]])
    assert "LIMIT" not in executor._unlimited("SELECT a FROM bookings ORDER BY a LIMIT 3").upper()
    assert "LIMIT" not in executor._unlimited(
        "select * from (SELECT a FROM bookings UNION SELECT b FROM payments) as q limit 200").upper()
