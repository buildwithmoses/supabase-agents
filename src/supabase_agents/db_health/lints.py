"""Database lints, modeled on Supabase's Splinter checks of the same names, scoped to one schema.

Every lint returns findings with a stable id `<lint>:<table>:<object>` that doesn't contain the schema name,
so a finding in `health` and the same finding in a shadow copy compare equal.

Splinter-style lints implemented: unindexed_foreign_keys, auth_rls_initplan, no_primary_key,
multiple_permissive_policies, policy_exists_rls_disabled, rls_enabled_no_policy, duplicate_index,
security_definer_view, function_search_path_mutable, rls_disabled_in_exposed_schema (Splinter's
rls_disabled_in_public, applied to a schema that is granted to API roles).
Custom: permissive_policy (a policy whose USING is `true` on a table the access spec says is not public).
Not implemented: unused_index (meaningless on a freshly seeded database).
"""

from __future__ import annotations

import re

from .spec import ACCESS

LEVELS = {
    "rls_disabled_in_exposed_schema": "ERROR", "permissive_policy": "ERROR", "policy_exists_rls_disabled": "ERROR",
    "security_definer_view": "ERROR", "rls_enabled_no_policy": "INFO", "auth_rls_initplan": "WARN",
    "unindexed_foreign_keys": "INFO", "duplicate_index": "WARN", "function_search_path_mutable": "WARN",
    "no_primary_key": "INFO", "multiple_permissive_policies": "WARN", "slow_query": "WARN",
}

AUTH_CALL = re.compile(r"auth\.(uid|jwt|role)\(\)", re.I)
WRAPPED_AUTH = re.compile(r"select\s+auth\.(uid|jwt|role)\(\)", re.I)


def needs_initplan(expr: str | None) -> bool:
    """True if an RLS expression calls auth.uid()/jwt()/role() without wrapping it in a scalar subquery."""
    if not expr:
        return False
    return len(AUTH_CALL.findall(expr)) > len(WRAPPED_AUTH.findall(expr))


Q_FK = """
select t.relname, c.conname, array_agg(a.attname order by k.n) as cols
from pg_constraint c
join pg_class t on t.oid = c.conrelid
join pg_namespace n on n.oid = t.relnamespace
cross join lateral unnest(c.conkey) with ordinality k(attnum, n)
join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.attnum
where c.contype = 'f' and n.nspname = %(s)s
  and not exists (
    select 1 from pg_index i
    where i.indrelid = c.conrelid
      and (select array_agg(x order by x) from unnest((i.indkey::int2[])[0:cardinality(c.conkey) - 1]) x)
        = (select array_agg(x order by x) from unnest(c.conkey) x))
group by t.relname, c.conname
"""

Q_POLICIES = """
select c.relname, p.polname, p.polpermissive, p.polcmd,
       array(select coalesce(r.rolname, 'public') from unnest(p.polroles) rid left join pg_roles r on r.oid = rid) as roles,
       pg_get_expr(p.polqual, p.polrelid) as qual, pg_get_expr(p.polwithcheck, p.polrelid) as with_check,
       c.relrowsecurity
from pg_policy p join pg_class c on c.oid = p.polrelid join pg_namespace n on n.oid = c.relnamespace
where n.nspname = %(s)s
"""

Q_TABLES = """
select c.relname, c.relrowsecurity,
       exists (select 1 from pg_constraint k where k.conrelid = c.oid and k.contype = 'p') as has_pk,
       exists (select 1 from pg_policy p where p.polrelid = c.oid) as has_policy,
       has_table_privilege('authenticated', c.oid, 'select') or has_table_privilege('anon', c.oid, 'select') as exposed
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = %(s)s and c.relkind in ('r', 'p')
"""

Q_DUP_INDEX = """
select t.relname, array_agg(ic.relname order by ic.relname) as indexes
from pg_index i
join pg_class ic on ic.oid = i.indexrelid
join pg_class t on t.oid = i.indrelid
join pg_namespace n on n.oid = t.relnamespace
where n.nspname = %(s)s
group by t.relname, i.indkey::text, i.indclass::text, coalesce(pg_get_expr(i.indexprs, i.indrelid), ''),
         coalesce(pg_get_expr(i.indpred, i.indrelid), ''), i.indisunique
having count(*) > 1
"""

Q_FUNCS = """
select p.proname, pg_get_function_identity_arguments(p.oid) as args, p.prosecdef, p.proconfig
from pg_proc p join pg_namespace n on n.oid = p.pronamespace
where n.nspname = %(s)s and p.prokind = 'f'
  and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) cfg where cfg like 'search_path=%%')
"""

Q_SECDEF_VIEWS = """
select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = %(s)s and c.relkind = 'v'
  and not coalesce((select bool_or(o = 'security_invoker=true' or o = 'security_invoker=on') from unnest(c.reloptions) o), false)
"""


def _f(lint, table, obj, detail, facts=None):
    return {"id": f"{lint}:{table}:{obj}", "lint": lint, "level": LEVELS[lint], "table": table, "object": obj,
            "detail": detail, "facts": facts or {}}


def run(conn, schema: str) -> list[dict]:
    p = {"s": schema}
    out: list[dict] = []
    for table, con, cols in conn.execute(Q_FK, p).fetchall():
        out.append(_f("unindexed_foreign_keys", table, con, f"Foreign key {con} on {table}({', '.join(cols)}) has no covering index.",
                      {"columns": list(cols), "constraint": con}))

    tables = {r[0]: {"rls": r[1], "has_pk": r[2], "has_policy": r[3], "exposed": r[4]} for r in conn.execute(Q_TABLES, p).fetchall()}
    for t, info in tables.items():
        if not info["has_pk"]:
            out.append(_f("no_primary_key", t, t, f"Table {t} has no primary key."))
        if info["exposed"] and not info["rls"]:
            out.append(_f("rls_disabled_in_exposed_schema", t, t,
                          f"Table {t} is readable by API roles but row level security is disabled, so every signed-in user can read every row.",
                          {"spec_visibility": ACCESS.get(t, {}).get("visibility")}))
        if info["rls"] and not info["has_policy"]:
            out.append(_f("rls_enabled_no_policy", t, t, f"Table {t} has RLS enabled but no policies, so no API user can read any row.",
                          {"spec_visibility": ACCESS.get(t, {}).get("visibility")}))

    pols = conn.execute(Q_POLICIES, p).fetchall()
    permissive_by: dict[tuple, list[str]] = {}
    for table, name, permissive, cmd, roles, qual, wcheck, rls in pols:
        if needs_initplan(qual) or needs_initplan(wcheck):
            out.append(_f("auth_rls_initplan", table, name,
                          f"Policy {name} on {table} calls auth functions once per row instead of once per query.",
                          {"qual": qual, "with_check": wcheck}))
        if not rls:
            out.append(_f("policy_exists_rls_disabled", table, name, f"Policy {name} exists on {table} but RLS is disabled, so it does nothing."))
        if permissive and (qual or "").strip().lower() == "true" and cmd in ("r", "*") \
                and ACCESS.get(table, {}).get("visibility", "public") != "public":
            out.append(_f("permissive_policy", table, name,
                          f"Policy {name} on {table} uses USING (true), so every signed-in user sees every row, but Courtline's rule for {table} is '{ACCESS[table]['visibility']}'.",
                          {"qual": qual, "roles": list(roles), "spec_visibility": ACCESS[table]["visibility"]}))
        if permissive:
            for role in roles:
                permissive_by.setdefault((table, role, cmd), []).append(name)
    for (table, role, cmd), names in permissive_by.items():
        if len(names) > 1:
            out.append(_f("multiple_permissive_policies", table, f"{role}:{cmd}", f"Table {table} has {len(names)} permissive policies for {role}: {', '.join(names)}."))

    for table, idxs in conn.execute(Q_DUP_INDEX, p).fetchall():
        out.append(_f("duplicate_index", table, "+".join(idxs), f"Indexes {', '.join(idxs)} on {table} are identical.", {"indexes": list(idxs)}))
    for name, args, secdef, _cfg in conn.execute(Q_FUNCS, p).fetchall():
        out.append(_f("function_search_path_mutable", name, f"{name}({args})",
                      f"Function {name}({args}) has no fixed search_path{' and runs as SECURITY DEFINER' if secdef else ''}.",
                      {"args": args, "security_definer": secdef}))
    for (v,) in conn.execute(Q_SECDEF_VIEWS, p).fetchall():
        out.append(_f("security_definer_view", v, v, f"View {v} runs with its owner's privileges."))
    return sorted(out, key=lambda f: f["id"])
