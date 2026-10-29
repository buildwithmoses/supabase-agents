"""Offline tests for db-health: no database, no model, no keys."""

from __future__ import annotations

from supabase_agents.db_health import fixes, lints, perf
from supabase_agents.db_health.explain import split_sql
from supabase_agents.db_health.fixes import naive_plan, plan, retarget, to_sql, wrap_auth_calls
from supabase_agents.db_health.spec import ACCESS, CLEAN_TABLES, policy_expr

CAL = ("select id, court_id, member_id, start_time, end_time, status from health.bookings "
       "where club_id = $1 and start_time >= $2 and start_time < $3 order by start_time")


def F(lint, table, obj, facts=None):
    return {"id": f"{lint}:{table}:{obj}", "lint": lint, "level": "WARN", "table": table, "object": obj, "detail": "", "facts": facts or {}}


FINDINGS = [
    F("slow_query", "bookings", "club_id+start_time", {"query": CAL, "index_columns": ["club_id", "start_time"]}),
    F("unindexed_foreign_keys", "bookings", "bookings_club_id_fkey", {"columns": ["club_id"]}),
    F("unindexed_foreign_keys", "bookings", "bookings_court_id_fkey", {"columns": ["court_id"]}),
    F("duplicate_index", "members", "members_user_id_idx+members_user_idx", {"indexes": ["members_user_id_idx", "members_user_idx"]}),
    F("auth_rls_initplan", "bookings", "bookings_own",
      {"qual": "(member_id IN ( SELECT m.id FROM health.members m WHERE (m.user_id = auth.uid())))"}),
    F("function_search_path_mutable", "is_staff", "is_staff(p_club uuid)", {"args": "p_club uuid"}),
    F("rls_disabled_in_exposed_schema", "leagues", "leagues"),
    F("rls_enabled_no_policy", "lesson_notes", "lesson_notes"),
    F("permissive_policy", "payments", "payments_all", {"qual": "true"}),
]


def test_needs_initplan_detects_bare_auth_calls_only():
    assert lints.needs_initplan("(user_id = auth.uid())")
    assert not lints.needs_initplan("(user_id = ( SELECT auth.uid() AS uid))")
    assert not lints.needs_initplan("(user_id = (select auth.uid()))")
    assert lints.needs_initplan("((select auth.uid()) = owner) or (auth.jwt() ->> 'role' = 'admin')")
    assert not lints.needs_initplan(None)


def test_wrap_auth_calls_is_idempotent():
    once = wrap_auth_calls("(m.user_id = auth.uid()) and auth.role() = 'authenticated'")
    assert once == "(m.user_id = (select auth.uid())) and (select auth.role()) = 'authenticated'"
    assert wrap_auth_calls(once) == once
    assert not lints.needs_initplan(once)


def test_where_columns_reads_equality_then_range_and_params():
    eq, rng, params = perf.where_columns(CAL)
    assert eq == ["club_id"] and rng == ["start_time"]
    assert params == {"1": "club_id", "2": "start_time", "3": "start_time"}


def test_plan_merges_fk_into_the_hot_query_index():
    migs = plan(FINDINGS, "health")
    first = migs[0]
    assert first["statements"][0]["sql"] == ("create index concurrently if not exists bookings_club_id_start_time_idx "
                                             "on health.bookings (club_id, start_time);")
    assert "unindexed_foreign_keys:bookings:bookings_club_id_fkey" in first["finding_ids"]
    assert first["perf"]["query"] == CAL
    assert not any("bookings_club_id_idx" in s["sql"] for m in migs for s in m["statements"])
    assert all(s["concurrent"] for s in first["statements"])


def test_plan_templates():
    by = {fid: m for m in plan(FINDINGS, "health") for fid in m["finding_ids"]}
    sql = lambda fid: " ".join(s["sql"] for s in by[fid]["statements"])  # noqa: E731
    assert sql("duplicate_index:members:members_user_id_idx+members_user_idx") == "drop index concurrently if exists health.members_user_idx;"
    assert "(m.user_id = (select auth.uid()))" in sql("auth_rls_initplan:bookings:bookings_own")
    assert sql("function_search_path_mutable:is_staff:is_staff(p_club uuid)") == "alter function health.is_staff(p_club uuid) set search_path = '';"
    leagues = by["rls_disabled_in_exposed_schema:leagues:leagues"]
    assert [s["sql"].split(" on ")[0] for s in leagues["statements"]] == ["create policy leagues_select", "alter table health.leagues enable row level security;"]
    assert leagues["rls_tables"] == ["leagues"]
    assert policy_expr("payments", "health") in sql("permissive_policy:payments:payments_all")
    assert by["permissive_policy:payments:payments_all"]["rls_tables"] == ["payments"]
    assert by["auth_rls_initplan:bookings:bookings_own"]["rls_tables"] == []   # must not change who sees what


def test_spec_policies_never_call_auth_per_row():
    for t in ACCESS:
        assert not lints.needs_initplan(policy_expr(t, "health")), t
    assert set(CLEAN_TABLES) <= set(ACCESS)


def test_retarget_only_touches_qualified_names():
    s = "alter policy p on health.payments using (club_id in (select club_id from health.members)); -- health check"
    out = retarget(s, "health", "health_shadow_1")
    assert "health_shadow_1.payments" in out and "health_shadow_1.members" in out and out.endswith("-- health check")
    assert retarget("select 1 from health_shadow_9.x", "health", "zz") == "select 1 from health_shadow_9.x"


def test_to_sql_marks_concurrent_migrations():
    m = plan(FINDINGS, "health")[0]
    text = to_sql(m, header="approved by @sam")
    assert text.startswith("-- M1:") and "outside a transaction block" in text and "approved by @sam" in text


def test_naive_plan_changes_exactly_three_fixes():
    naive = {fid: m for m in naive_plan(FINDINGS, "health") for fid in m["finding_ids"]}
    good = {fid: m for m in plan(FINDINGS, "health") for fid in m["finding_ids"]}
    changed = sorted({m["id"] for fid, m in naive.items() if m["statements"] != good[fid]["statements"]})
    assert len(changed) == 3
    assert "member_id = (select auth.uid())" in naive["auth_rls_initplan:bookings:bookings_own"]["statements"][0]["sql"]


def test_split_sql_for_model_drafts():
    stmts = split_sql("CREATE INDEX CONCURRENTLY a ON health.b (c);\nALTER TABLE health.b ENABLE ROW LEVEL SECURITY;\n-- note")
    assert [s["concurrent"] for s in stmts] == [True, False]
    assert stmts[1]["sql"] == "ALTER TABLE health.b ENABLE ROW LEVEL SECURITY;"


def test_fix_order_puts_indexes_before_policies():
    order = [m["finding_ids"][0].split(":")[0] for m in plan(FINDINGS, "health")]
    assert order.index("slow_query") < order.index("auth_rls_initplan") < order.index("permissive_policy")
    assert fixes.ORDER[0] == "slow_query"
