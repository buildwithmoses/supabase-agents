"""Code-written fixes: each finding becomes migration SQL from a template, never from the model.

A migration is {id, title, finding_ids, statements: [{sql, concurrent}], rls_tables, perf}:
  rls_tables  tables whose RLS behavior this migration is ALLOWED to change (it must then match the access
              spec); every other table's visible rows must stay identical.
  perf        the slow query to re-time, if the migration is a performance fix.
Index builds use CREATE INDEX CONCURRENTLY so a live bookings table isn't locked; they can't run inside a
transaction, so the applier runs them on their own.
One index can fix two findings: (club_id, start_time) for the slow query also covers the club_id foreign key.
"""

from __future__ import annotations

import re

from .lints import AUTH_CALL
from .spec import policy_expr

ORDER = ["slow_query", "unindexed_foreign_keys", "duplicate_index", "auth_rls_initplan", "function_search_path_mutable",
         "rls_disabled_in_exposed_schema", "rls_enabled_no_policy", "permissive_policy", "policy_exists_rls_disabled"]


def wrap_auth_calls(expr: str) -> str:
    """auth.uid() -> (select auth.uid()), leaving calls that are already wrapped alone."""
    out, i = [], 0
    for m in AUTH_CALL.finditer(expr):
        before = expr[:m.start()].rstrip()
        out.append(expr[i:m.start()])
        out.append(m.group(0) if re.search(r"select\s*$", before, re.I) else f"(select {m.group(0)})")
        i = m.end()
    out.append(expr[i:])
    return "".join(out)


def _stmt(sql: str, concurrent: bool = False) -> dict:
    return {"sql": sql.strip().rstrip(";") + ";", "concurrent": concurrent}


def _index_name(table: str, cols: list[str]) -> str:
    return f"{table}_{'_'.join(cols)}_idx"[:63]


def plan(findings: list[dict], schema: str) -> list[dict]:
    """Turn findings into ordered migrations. Findings with no safe template are returned as manual review."""
    migs: list[dict] = []
    covered: dict[tuple, str] = {}       # (table, first column) -> migration id of an index leading with it
    by_lint = sorted(findings, key=lambda f: (ORDER.index(f["lint"]) if f["lint"] in ORDER else 99, f["id"]))
    n = 0

    def add(title, fids, stmts, rls_tables=(), perf=None):
        nonlocal n
        n += 1
        m = {"id": f"M{n}", "title": title, "finding_ids": list(fids), "statements": stmts,
             "rls_tables": list(rls_tables), "perf": perf}
        migs.append(m)
        return m

    for f in by_lint:
        lint, t, facts = f["lint"], f["table"], f["facts"]
        q = f"{schema}.{t}"
        if lint == "slow_query":
            cols = facts["index_columns"]
            m = add(f"Index {t} on ({', '.join(cols)}) for the hot query", [f["id"]],
                    [_stmt(f"create index concurrently if not exists {_index_name(t, cols)} on {q} ({', '.join(cols)})", True)],
                    perf={"query": facts["query"]})
            covered[(t, cols[0])] = m["id"]
        elif lint == "unindexed_foreign_keys":
            cols = facts["columns"]
            mid = covered.get((t, cols[0])) if len(cols) == 1 else None
            if mid:
                next(m for m in migs if m["id"] == mid)["finding_ids"].append(f["id"])
                continue
            add(f"Index foreign key {t}({', '.join(cols)})", [f["id"]],
                [_stmt(f"create index concurrently if not exists {_index_name(t, cols)} on {q} ({', '.join(cols)})", True)])
        elif lint == "duplicate_index":
            keep, *drop = facts["indexes"]           # sorted by name: keep the first, drop the copies (stable in any copy)
            add(f"Drop duplicate index{'es' if len(drop) > 1 else ''} {', '.join(drop)} (keep {keep})", [f["id"]],
                [_stmt(f"drop index concurrently if exists {schema}.{d}", True) for d in drop])
        elif lint == "auth_rls_initplan":
            stmts = []
            if facts.get("qual"):
                stmts.append(_stmt(f"alter policy {f['object']} on {q} using ({wrap_auth_calls(facts['qual'])})"))
            if facts.get("with_check"):
                stmts.append(_stmt(f"alter policy {f['object']} on {q} with check ({wrap_auth_calls(facts['with_check'])})"))
            add(f"Evaluate auth.uid() once per query in policy {f['object']}", [f["id"]], stmts)
        elif lint == "function_search_path_mutable":
            add(f"Pin search_path on {f['object']}", [f["id"]],
                [_stmt(f"alter function {schema}.{t}({facts['args']}) set search_path = ''")])
        elif lint == "rls_disabled_in_exposed_schema":
            add(f"Turn on RLS for {t} with Courtline's access rule", [f["id"]],
                [_stmt(f"create policy {t}_select on {q} for select to authenticated using ({policy_expr(t, schema)})"),
                 _stmt(f"alter table {q} enable row level security")], rls_tables=[t])
        elif lint == "rls_enabled_no_policy":
            add(f"Add the missing read policy on {t}", [f["id"]],
                [_stmt(f"create policy {t}_select on {q} for select to authenticated using ({policy_expr(t, schema)})")], rls_tables=[t])
        elif lint == "permissive_policy":
            add(f"Restrict policy {f['object']} on {t} to Courtline's rule", [f["id"]],
                [_stmt(f"alter policy {f['object']} on {q} using ({policy_expr(t, schema)})")], rls_tables=[t])
        else:
            add(f"Manual review: {f['id']}", [f["id"]], [], )
            migs[-1]["manual"] = True
    return migs


def to_sql(m: dict, header: str = "") -> str:
    lines = [f"-- {m['id']}: {m['title']}", f"-- fixes: {', '.join(m['finding_ids'])}"]
    if header:
        lines.append(f"-- {header}")
    if any(s["concurrent"] for s in m["statements"]):
        lines.append("-- CONCURRENTLY statements must run outside a transaction block.")
    lines += [s["sql"] for s in m["statements"]]
    return "\n".join(lines) + "\n"


def retarget(sql: str, src: str, dst: str) -> str:
    """Point SQL written for schema `src` at schema `dst` (qualified names only)."""
    return re.sub(rf"\b{re.escape(src)}\.", f"{dst}.", sql)


# ---- regression: plausible-looking fixes that are wrong ------------------------------------------------------
# The kind of fix a hurried engineer (or a model) writes. The shadow proof must catch every one.
def naive_plan(findings: list[dict], schema: str) -> list[dict]:
    migs = plan(findings, schema)
    for m in migs:
        fids = " ".join(m["finding_ids"])
        if "auth_rls_initplan:bookings" in fids:      # compares member_id to the user's id: players lose their bookings
            m["statements"] = [_stmt(f"alter policy bookings_own on {schema}.bookings using (member_id = (select auth.uid()))")]
            m["naive"] = "member_id compared with the auth user id"
        elif "permissive_policy:payments" in fids:     # "club members" instead of "own or staff": players see the whole club
            m["statements"] = [_stmt(f"alter policy payments_all on {schema}.payments using "
                                     f"(club_id in (select club_id from {schema}.members where user_id = auth.uid()))")]
            m["naive"] = "every club member sees every club payment, and auth.uid() runs per row"
        elif "rls_disabled_in_exposed_schema:leagues" in fids:   # turns RLS on and forgets the policy
            m["statements"] = [_stmt(f"alter table {schema}.leagues enable row level security")]
            m["rls_tables"] = ["leagues"]
            m["naive"] = "RLS enabled with no policy: members can no longer see leagues"
    return migs
