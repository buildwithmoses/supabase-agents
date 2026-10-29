"""Prove migrations in a shadow copy of the schema before anyone approves them.

clone(conn, src, dst)   copy tables (with their original index and constraint names), data, foreign keys,
                        functions, RLS flags, policies and grants from `src` into a new schema `dst`
snapshot(conn, schema)  for every table and every probe user: the rows that user can see under RLS, and the
                        rows Courtline's access spec says they SHOULD see (count + md5 of sorted ids)
fingerprint(conn, s)    hash of indexes, policies, RLS flags and function settings: "did anything change?"
prove(migrations)       clone, then apply each migration in order and check:
                          applied without error
                          its findings are gone, and no new finding appeared
                          RLS: tables it may change now match the spec; every other table is unchanged
                          perf migrations: the slow query is no longer a sequential scan and runs faster
                        then drop the shadow schema.
"""

from __future__ import annotations

import os
import re
import time

from ..core.db import as_user, connect
from . import SHADOW_PREFIX, lints, perf
from .fixes import retarget
from .seed import probe_users
from .spec import ACCESS, expected_sql

TABLE_ORDER = ["clubs", "members", "courts", "coaches", "announcements", "bookings", "payments", "leagues", "lesson_notes"]


def _q(sql: str, src: str, dst: str) -> str:
    return retarget(sql, src, dst)


def clone(conn, src: str, dst: str) -> None:
    conn.execute(f"drop schema if exists {dst} cascade")
    conn.execute(f"create schema {dst}")
    conn.execute(f"grant usage on schema {dst} to authenticated, anon")
    tables = [r[0] for r in conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname=%s and c.relkind='r'", (src,)).fetchall()]
    tables.sort(key=lambda t: TABLE_ORDER.index(t) if t in TABLE_ORDER else 99)
    for t in tables:
        conn.execute(f"create table {dst}.{t} (like {src}.{t} including defaults including identity including generated including comments)")
        for name, cdef in conn.execute(
                "select conname, pg_get_constraintdef(c.oid) from pg_constraint c join pg_class t on t.oid=c.conrelid "
                "join pg_namespace n on n.oid=t.relnamespace where n.nspname=%s and t.relname=%s and c.contype in ('p','u','c') order by c.contype desc",
                (src, t)).fetchall():
            conn.execute(f"alter table {dst}.{t} add constraint {name} {_q(cdef, src, dst)}")
        conn.execute(f"insert into {dst}.{t} overriding system value select * from {src}.{t}")
        for (idef,) in conn.execute(
                "select pg_get_indexdef(i.indexrelid) from pg_index i join pg_class t on t.oid=i.indrelid join pg_namespace n on n.oid=t.relnamespace "
                "where n.nspname=%s and t.relname=%s and not exists (select 1 from pg_constraint k where k.conindid=i.indexrelid) "
                "order by i.indexrelid", (src, t)).fetchall():
            conn.execute(_q(idef, src, dst))
    for t in tables:
        for name, cdef in conn.execute(
                "select conname, pg_get_constraintdef(c.oid) from pg_constraint c join pg_class t on t.oid=c.conrelid "
                "join pg_namespace n on n.oid=t.relnamespace where n.nspname=%s and t.relname=%s and c.contype='f'", (src, t)).fetchall():
            conn.execute(f"alter table {dst}.{t} add constraint {name} {_q(cdef, src, dst)}")
    for (fdef,) in conn.execute(
            "select pg_get_functiondef(p.oid) from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname=%s and p.prokind='f'",
            (src,)).fetchall():
        conn.execute(_q(fdef, src, dst))
    for t, rls, name, permissive, cmd, roles, qual, wcheck in conn.execute(
            """select c.relname, c.relrowsecurity, p.polname, p.polpermissive, p.polcmd,
                      array(select coalesce(r.rolname, 'public') from unnest(p.polroles) rid left join pg_roles r on r.oid = rid),
                      pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
               from pg_class c join pg_namespace n on n.oid = c.relnamespace left join pg_policy p on p.polrelid = c.oid
               where n.nspname = %s and c.relkind = 'r'""", (src,)).fetchall():
        if name:
            cmdname = {"r": "select", "a": "insert", "w": "update", "d": "delete", "*": "all"}[cmd]
            sql = (f"create policy {name} on {dst}.{t} as {'permissive' if permissive else 'restrictive'} for {cmdname} "
                   f"to {', '.join(roles)}" + (f" using ({_q(qual, src, dst)})" if qual else "")
                   + (f" with check ({_q(wcheck, src, dst)})" if wcheck else ""))
            conn.execute(sql)
    for t, rls in conn.execute("select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace "
                               "where n.nspname=%s and c.relkind='r'", (src,)).fetchall():
        if rls:
            conn.execute(f"alter table {dst}.{t} enable row level security")
    for grantee, t, priv in conn.execute(
            "select grantee, table_name, privilege_type from information_schema.role_table_grants "
            "where table_schema=%s and grantee in ('authenticated','anon','service_role')", (src,)).fetchall():
        conn.execute(f"grant {priv} on {dst}.{t} to {grantee}")
    for t in tables:
        conn.execute(f"analyze {dst}.{t}")


def _digest(conn, sql: str, params=None) -> tuple[int, str]:
    r = conn.execute(f"select count(*), coalesce(md5(string_agg(x.id, ',' order by x.id)), '') from ({sql}) x(id)", params).fetchone()
    return int(r[0]), r[1]


def snapshot(conn, schema: str, users: dict[str, str] | None = None) -> dict:
    """{table: {probe: {"actual": [n, md5], "expected": [n, md5]}}}"""
    users = users or probe_users()
    out: dict = {}
    for t in ACCESS:
        out[t] = {}
        for probe, uid in users.items():
            exp_ = _digest(conn, expected_sql(t, schema), {"uid": uid})
            with conn.transaction(force_rollback=True):
                as_user(conn, uid)
                act = _digest(conn, f"select id::text from {schema}.{t}")
            out[t][probe] = {"actual": list(act), "expected": list(exp_)}
    return out


def matches_spec(snap: dict, table: str) -> bool:
    return all(v["actual"] == v["expected"] for v in snap[table].values())


def unchanged(before: dict, after: dict, table: str) -> bool:
    return all(before[table][p]["actual"] == after[table][p]["actual"] for p in before[table])


def fingerprint(conn, schema: str) -> str:
    return conn.execute(
        """select md5(coalesce(string_agg(x, '|' order by x), '')) from (
             select 'i:' || indexname || ':' || indexdef from pg_indexes where schemaname = %(s)s
             union all select 'p:' || tablename || ':' || policyname || ':' || coalesce(qual, '') || ':' || coalesce(with_check, '') from pg_policies where schemaname = %(s)s
             union all select 'r:' || c.relname || ':' || c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = %(s)s and c.relkind = 'r'
             union all select 'f:' || p.proname || ':' || coalesce(array_to_string(p.proconfig, ','), '') from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = %(s)s
           ) q(x)""", {"s": schema}).fetchone()[0]


def findings(conn, schema: str) -> list[dict]:
    return lints.run(conn, schema)


def apply_statements(conn, statements: list[dict], src: str, dst: str) -> str | None:
    """Run a migration's statements against `dst`: CONCURRENTLY ones alone, the rest together in one transaction."""
    batch: list[str] = []

    def flush():
        if batch:
            with conn.transaction():
                for s in batch:
                    conn.execute(s)
            batch.clear()
    try:
        for st in statements:
            sql = retarget(st["sql"], src, dst)
            if st.get("concurrent") or re.search(r"\bconcurrently\b", sql, re.I):
                flush()
                conn.execute(sql)
            else:
                batch.append(sql)
        flush()
        return None
    except Exception as e:  # noqa: BLE001 - reported, not raised: a failed proof is a result
        batch.clear()
        return f"{type(e).__name__}: {str(e).strip().splitlines()[0][:300]}"


def prove(migrations: list[dict], src: str, slow_query_ids: set[str] | None = None, keep: bool = False) -> dict:
    """Prove migrations in order in a fresh shadow schema. Returns {"shadow", "steps": {mid: proof}, "ok": bool}."""
    dst = f"{SHADOW_PREFIX}{os.getpid()}_{int(time.time() * 1000) % 10_000_000}"
    users = probe_users()
    t0 = time.time()
    steps: dict[str, dict] = {}
    with connect(autocommit=True) as conn:
        clone(conn, src, dst)
        try:
            base_ids = {f["id"] for f in findings(conn, dst)}
            real_ids = {f["id"] for f in findings(conn, src)}
            clone_faithful = base_ids == real_ids
            snap = snapshot(conn, dst, users)
            current = set(base_ids)
            for m in migrations:
                proof: dict = {"migration": m["id"], "clone_faithful": clone_faithful}
                perf_before = None
                if m.get("perf"):
                    params = perf.sample_params(conn, dst, retarget(m["perf"]["query"], src, dst))
                    perf_before = perf.explain_analyze(conn, retarget(m["perf"]["query"], src, dst), params)
                err = apply_statements(conn, m["statements"], src, dst)
                proof["applied"] = err is None
                proof["error"] = err
                after_ids = {f["id"] for f in findings(conn, dst)}
                targeted = set(m["finding_ids"])
                proof["cleared"] = sorted(targeted - after_ids)
                proof["still_present"] = sorted(targeted & after_ids)
                proof["new_findings"] = sorted(after_ids - current)
                new_snap = snapshot(conn, dst, users)
                rls = {}
                for t in ACCESS:
                    rls[t] = matches_spec(new_snap, t) if t in m.get("rls_tables", []) else unchanged(snap, new_snap, t)
                proof["rls"] = rls
                proof["rls_ok"] = all(rls.values())
                if m.get("perf"):
                    after = perf.explain_analyze(conn, retarget(m["perf"]["query"], src, dst), params)
                    proof["perf"] = {"before": perf_before, "after": after,
                                     "speedup": round(perf_before["ms"] / max(after["ms"], 1e-3), 1)}
                    proof["perf_ok"] = "Seq Scan" not in (after["scans"] or []) and proof["perf"]["speedup"] >= 2
                else:
                    proof["perf_ok"] = True
                proof["ok"] = (proof["applied"] and not proof["still_present"] and not proof["new_findings"]
                               and proof["rls_ok"] and proof["perf_ok"] and clone_faithful and bool(m["statements"]))
                steps[m["id"]] = proof
                current, snap = after_ids, new_snap
            final_spec = {t: matches_spec(snap, t) for t in ACCESS}
        finally:
            if not keep:
                conn.execute(f"drop schema if exists {dst} cascade")
    return {"shadow": dst, "steps": steps, "ok": all(p["ok"] for p in steps.values()) and bool(steps),
            "final_spec_match": final_spec, "seconds": round(time.time() - t0, 1)}


def cleanup_shadows() -> int:
    with connect(autocommit=True) as conn:
        names = [r[0] for r in conn.execute("select nspname from pg_namespace where nspname like %s", (SHADOW_PREFIX + "%",)).fetchall()]
        for n in names:
            conn.execute(f"drop schema if exists {n} cascade")
    return len(names)
