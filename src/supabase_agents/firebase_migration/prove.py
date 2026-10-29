"""Prove a migration in a throwaway schema before anyone approves it.

1. Row counts: every source document is either a row or a logged skip with a reason.
2. Referential integrity: no foreign key value points at a missing row (constraints plus an explicit count).
3. Field fidelity: every loaded field read back from Postgres equals the source value (timestamps by instant,
   references by path, Firebase uids by their uuid mapping, logged fixes by their fixed value).
4. Security: every rules test case is re-run against RLS as the matching end user (role authenticated or anon,
   JWT claims set like Supabase does) and must give Firebase's expected allow/deny. Tests on rules the
   translator flagged must DENY (fail closed) and the rule must be in the flag list.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime

import psycopg

from ..core.db import connect
from .export import coll_key_of_path, is_ref, is_ts
from .infer import convert_rows, parse_iso, singular, ts_to_dt, uid_to_uuid


def run_sql(sql: str) -> None:
    with connect() as conn:
        conn.execute(sql)
        conn.commit()


def drop_schema(name: str) -> None:
    run_sql(f"drop schema if exists {name} cascade;")


def counts(S: str, schema: dict, docs: list[dict], converted: dict) -> dict:
    by_coll: dict = {}
    for d in docs:
        by_coll.setdefault(d["coll"], 0)
        by_coll[d["coll"]] += 1
    skips: dict = {}
    for s in converted["skips"]:
        skips.setdefault(coll_key_of_path(s["path"]), []).append(s)
    out, ok = {}, True
    with connect() as conn:
        for ck, n in by_coll.items():
            t = schema["tables"][ck]["name"]
            loaded = conn.execute(f"select count(*) from {S}.{t}").fetchone()[0]
            sk = skips.get(ck, [])
            explained = all(s.get("reason") for s in sk)
            rec = loaded + len(sk) == n and explained
            ok &= rec
            out[t] = {"source_docs": n, "rows": loaded, "skipped": len(sk), "reconciled": rec}
    return {"ok": ok, "tables": out}


def integrity(S: str, schema: dict) -> dict:
    checks, bad = [], 0
    tables = schema["tables"]
    with connect() as conn:
        n_fk = conn.execute("select count(*) from pg_constraint c join pg_namespace n on n.oid = c.connamespace "
                            "where n.nspname = %s and c.contype = 'f'", (S,)).fetchone()[0]
        for ck, t in tables.items():
            for c in t["columns"]:
                if c.get("fk"):
                    target = tables[c["fk"]["coll"]]["name"]
                    q = (f"select count(*) from {S}.{t['name']} x where x.{c['name']} is not null and not exists "
                         f"(select 1 from {S}.{target} y where y.{c['fk']['col']} = x.{c['name']})")
                    orphans = conn.execute(q).fetchone()[0]
                    bad += orphans
                    checks.append({"fk": f"{t['name']}.{c['name']} -> {target}.{c['fk']['col']}", "orphans": orphans})
            for j in t["join"]:
                target = tables[j["target"]]["name"]
                for col, tbl in ((f"{singular(t['name'])}_path", t["name"]), (f"{singular(target)}_path", target)):
                    orphans = conn.execute(f"select count(*) from {S}.{j['name']} x where not exists "
                                           f"(select 1 from {S}.{tbl} y where y._path = x.{col})").fetchone()[0]
                    bad += orphans
                    checks.append({"fk": f"{j['name']}.{col} -> {tbl}._path", "orphans": orphans})
    return {"ok": bad == 0, "orphans": bad, "fk_constraints": n_fk, "checks": checks}


def _same(src, db, col: dict, fixed: dict | None) -> bool:
    if fixed is not None:
        to = fixed["to"]
        if to is None:
            return db is None
        if isinstance(db, datetime):
            return abs(db.timestamp() - (parse_iso(to).timestamp() if isinstance(to, str) else to)) < 1e-6
        return _num_eq(db, to)
    kind, typ = col.get("kind"), col["type"]
    if src is None:
        return db is None
    if kind == "ref":
        return db == src["path"]
    if kind == "uid":
        return str(db) == uid_to_uuid(src)
    if kind == "flatten":
        return (src or {}).get(col["subkey"]) == db
    if typ == "timestamptz":
        return isinstance(db, datetime) and abs(db.timestamp() - ts_to_dt(src).timestamp()) < 1e-6
    if typ in ("bigint", "double precision"):
        return _num_eq(db, src)
    if typ == "jsonb":
        return json.dumps(db, sort_keys=True) == json.dumps(src, sort_keys=True)
    if typ.endswith("[]"):
        return list(db or []) == list(src)
    return db == src


def _num_eq(a, b) -> bool:
    try:
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def fidelity(S: str, schema: dict, docs: list[dict], converted: dict) -> dict:
    skipped = {s["path"] for s in converted["skips"]}
    fixes = {}
    for f in converted["fixes"]:
        fixes.setdefault(f["where"], []).append(f)
    checked = matched = n_fixed = 0
    mismatches = []
    with connect() as conn:
        conn.row_factory = psycopg.rows.dict_row
        rows = {}
        for ck, t in schema["tables"].items():
            for r in conn.execute(f"select * from {S}.{t['name']}").fetchall():
                rows[r["_path"]] = r
            for j in t["join"]:
                tc = f"{singular(schema['tables'][j['target']]['name'])}_path"
                oc = f"{singular(t['name'])}_path"
                for r in conn.execute(f"select {oc} o, {tc} t from {S}.{j['name']} order by {oc}, position").fetchall():
                    rows.setdefault(("join", j["name"], r["o"]), []).append(r["t"])
    for d in docs:
        if d["path"] in skipped:
            continue
        t = schema["tables"][d["coll"]]
        row = rows.get(d["path"])
        if row is None:
            mismatches.append({"path": d["path"], "field": "*", "why": "row missing"}); checked += 1; continue
        for c in t["columns"]:
            src = c.get("source")
            if not src:
                continue
            where = f"{d['path']}.{src}"
            fx = fixes.get(where)
            if fx and c.get("kind") != "flatten":
                n_fixed += 1
            ok = _same(d["fields"].get(src), row[c["name"]], c, fx[0] if fx and c.get("kind") not in ("flatten",) else None)
            checked += 1; matched += ok
            if not ok:
                mismatches.append({"path": d["path"], "field": src, "db": str(row[c["name"]])[:80]})
        for j in t["join"]:
            want = [e["path"] for e in d["fields"].get(j["field"]) or []]
            dropped = {f["from"] for f in fixes.get(f"{d['path']}.{j['field']}", [])}
            want = [w for w in want if w not in dropped]
            got = rows.get(("join", j["name"], d["path"]), [])
            checked += 1; ok = want == got; matched += ok
            if not ok:
                mismatches.append({"path": d["path"], "field": j["field"], "db": str(got)[:80]})
    return {"fidelity": round(matched / checked, 4) if checked else 0.0, "fields_checked": checked,
            "fields_from_logged_fixes": n_fixed, "mismatches": mismatches[:20]}


# ---------------------------------------------------------------- rules tests against RLS

def _set_identity(conn, t: dict) -> None:
    if t.get("auth"):
        claims = {"sub": uid_to_uuid(t["auth"]), "role": "authenticated", **(t.get("token") or {})}
        conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
        conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (claims["sub"],))
        conn.execute("set local role authenticated")
    else:
        conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps({"role": "anon"}),))
        conn.execute("set local role anon")


def _row_for(test: dict, schema: dict, known: dict) -> tuple[dict, dict]:
    path = test["path"]
    ck = coll_key_of_path(path)
    t = schema["tables"][ck]
    parts = path.split("/")
    parents = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 2, 2)]
    doc = {"coll": ck, "path": path, "id": parts[-1], "parents": parents, "fields": test.get("data") or {}}
    conv = convert_rows([doc], schema, known_paths=known)
    if conv["skips"]:
        raise ValueError(f"test data would be skipped: {conv['skips']}")
    return t, conv["rows"][ck][0]


def run_rules_tests(S: str, schema: dict, tests: list[dict], known: dict) -> list[dict]:
    out = []
    types = {}
    for ck, t in schema["tables"].items():
        types[t["name"]] = {c["name"]: c["type"] for c in t["columns"]}
    for test in tests:
        result, detail = None, ""
        try:
            with connect() as conn:
                with conn.transaction(force_rollback=True):
                    conn.execute("set local statement_timeout = 5000")
                    _set_identity(conn, test)
                    ck = coll_key_of_path(test["path"])
                    t = schema["tables"][ck]
                    tn = f"{S}.{t['name']}"
                    try:
                        if test["op"] == "get":
                            hit = conn.execute(f"select 1 from {tn} where _path = %s", (test["path"],)).fetchone()
                            result = "allow" if hit else "deny"
                        elif test["op"] == "delete":
                            cur = conn.execute(f"delete from {tn} where _path = %s", (test["path"],))
                            result = "allow" if cur.rowcount == 1 else "deny"
                        elif test["op"] == "create":
                            _, row = _row_for(test, schema, known)
                            cols = [c for c in row]
                            conn.execute(f"insert into {tn} ({', '.join(cols)}) values ({', '.join('%s' for _ in cols)})",
                                         [_adapt(row[c], types[t['name']][c]) for c in cols])
                            result = "allow"
                        elif test["op"] == "update":
                            _, row = _row_for(test, schema, known)
                            srcs = set((test.get("data") or {}).keys())
                            cols = [c["name"] for c in t["columns"] if c.get("source") in srcs]
                            sets = ", ".join(f"{c} = %s" for c in cols)
                            cur = conn.execute(f"update {tn} set {sets} where _path = %s",
                                               [_adapt(row[c], types[t['name']][c]) for c in cols] + [test["path"]])
                            result = "allow" if cur.rowcount == 1 else "deny"
                    except psycopg.errors.InsufficientPrivilege as e:
                        result, detail = "deny", str(e).splitlines()[0][:120]
        except Exception as e:  # noqa: BLE001
            result, detail = "error", f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
        out.append({"id": test["id"], "op": test["op"], "path": test["path"], "expected": test["expected"],
                    "got": result, "match": result == test["expected"], "flagged_rule": test.get("flagged_rule", False), "detail": detail})
    return out


def _adapt(v, typ):
    if typ == "jsonb" and v is not None:
        return psycopg.types.json.Jsonb(v)
    if typ == "uuid" and v is not None:
        return uuid.UUID(str(v))
    return v


def known_paths(docs: list[dict], converted: dict) -> dict:
    skipped = {s["path"] for s in converted["skips"]}
    out: dict = {}
    for d in docs:
        if d["path"] not in skipped:
            out.setdefault(d["coll"], set()).add(d["path"])
    return out


def prove(S: str, sql: str, schema: dict, docs: list[dict], converted: dict, tests: list[dict], flags: list[dict]) -> dict:
    drop_schema(S)
    try:
        run_sql(sql.replace("{S}", S))
        c = counts(S, schema, docs, converted)
        i = integrity(S, schema)
        f = fidelity(S, schema, docs, converted)
        rt = run_rules_tests(S, schema, tests, known_paths(docs, converted))
    finally:
        drop_schema(S)
    flagged_keys = {fl.get("key") for fl in flags}
    plain = [r for r in rt if not r["flagged_rule"]]
    flagged = [r for r in rt if r["flagged_rule"]]
    test_key = {t["id"]: t.get("flag_key") for t in tests}
    for r in flagged:
        r["fail_closed"] = r["got"] == "deny" and test_key.get(r["id"]) in flagged_keys
    return {
        "counts": c, "integrity": i, "fidelity": f, "rules_tests": rt,
        "rules_match": round(sum(r["match"] for r in plain) / len(plain), 4) if plain else 0.0,
        "flagged_fail_closed": round(sum(r["fail_closed"] for r in flagged) / len(flagged), 4) if flagged else 1.0,
        "errors": [r for r in rt if r["got"] == "error"],
        "passed": c["ok"] and i["ok"] and all(r["match"] for r in plain) and all(r["fail_closed"] for r in flagged),
    }
