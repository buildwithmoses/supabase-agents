"""Performance facts: pg_stat_statements, EXPLAIN (GENERIC_PLAN) and EXPLAIN ANALYZE.

slow_query findings come from code, not the model:
  a parameterized query on the schema with >= MIN_CALLS calls and mean time >= MIN_MEAN_MS
  whose generic plan sequentially scans a table of >= MIN_ROWS rows with a filter.
The suggested index is (equality columns..., then one range column), read from the WHERE clause with sqlglot.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import sqlglot
from sqlglot import exp

MIN_CALLS = 50
MIN_MEAN_MS = 2.0
MIN_ROWS = 10_000


def top_queries(conn, schema: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        rf"""select queryid, calls, mean_exec_time, total_exec_time, rows, query
            from extensions.pg_stat_statements
            where query ~ %s and query !~* '^\s*(explain|analyze|create|alter|drop|insert|copy|grant|select extensions)'
            order by total_exec_time desc limit %s""",
        (rf"\m{schema}\.", limit)).fetchall()
    return [{"queryid": str(q), "calls": c, "mean_ms": round(m, 3), "total_ms": round(t, 1), "rows": r, "query": txt}
            for q, c, m, t, r, txt in rows]


def generic_plan(conn, sql: str) -> dict:
    with conn.transaction(force_rollback=True):
        return conn.execute(f"explain (generic_plan, format json) {sql}").fetchone()[0][0]["Plan"]


def walk(plan: dict):
    yield plan
    for p in plan.get("Plans", []) or []:
        yield from walk(p)


def seq_scans(plan: dict) -> list[dict]:
    return [{"relation": n.get("Relation Name"), "schema": n.get("Schema"), "filter": n.get("Filter"), "plan_rows": n.get("Plan Rows")}
            for n in walk(plan) if n.get("Node Type") == "Seq Scan"]


def where_columns(sql: str) -> tuple[list[str], list[str], dict[str, str]]:
    """(equality columns, range columns, {param: column}) from a query's WHERE clause."""
    tree = sqlglot.parse_one(sql, read="postgres")
    where = tree.args.get("where")
    eq, rng, params = [], [], {}
    if where is None:
        return eq, rng, params
    for node in where.find_all(exp.EQ, exp.GTE, exp.GT, exp.LTE, exp.LT):
        col, other = node.this, node.expression
        if not isinstance(col, exp.Column):
            col, other = other, col
        if not isinstance(col, exp.Column):
            continue
        name = col.name
        if isinstance(other, (exp.Parameter, exp.Placeholder)):
            params[other.sql(dialect="postgres").lstrip("$@")] = name
        if isinstance(node, exp.EQ):
            if name not in eq:
                eq.append(name)
        elif name not in rng and name not in eq:
            rng.append(name)
    return eq, rng, params


def table_rows(conn, schema: str, table: str) -> int:
    r = conn.execute("select reltuples::bigint from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = %s and c.relname = %s",
                     (schema, table)).fetchone()
    return int(r[0]) if r else 0


def slow_query_findings(conn, schema: str) -> list[dict]:
    out = []
    for q in top_queries(conn, schema):
        if q["calls"] < MIN_CALLS or q["mean_ms"] < MIN_MEAN_MS:
            continue
        try:
            plan = generic_plan(conn, q["query"])
        except Exception:
            continue
        for scan in seq_scans(plan):
            if not scan["filter"] or (scan["schema"] and scan["schema"] != schema):
                continue
            rows = table_rows(conn, schema, scan["relation"])
            if rows < MIN_ROWS:
                continue
            eq, rng, _ = where_columns(q["query"])
            cols = eq + rng[:1]
            if not cols:
                continue
            table = scan["relation"]
            out.append({
                "id": f"slow_query:{table}:{'+'.join(cols)}", "lint": "slow_query", "level": "WARN", "table": table,
                "object": "+".join(cols),
                "detail": f"A query run {q['calls']} times averages {q['mean_ms']} ms because it reads all {rows:,} rows of {table} (sequential scan) to filter on {', '.join(cols)}.",
                "facts": {"query": q["query"], "calls": q["calls"], "mean_ms": q["mean_ms"], "total_ms": q["total_ms"],
                          "table_rows": rows, "filter": scan["filter"], "index_columns": cols, "queryid": q["queryid"]},
            })
    return out


def sample_params(conn, schema: str, sql: str) -> list:
    """Concrete values for $1..$n from a real row: equality -> that row's value, range -> a 7-day window."""
    eq, rng, params = where_columns(sql)
    table = sqlglot.parse_one(sql, read="postgres").find(exp.Table).name
    cols = sorted(set(params.values()))
    row = conn.execute(f"select {', '.join(cols)} from {schema}.{table} order by 1, 2 limit 1 offset 5000" if len(cols) > 1
                       else f"select {cols[0]} from {schema}.{table} limit 1 offset 5000").fetchone()
    val = dict(zip(cols, row))
    tree = sqlglot.parse_one(sql, read="postgres")
    out: dict[int, object] = {}
    for node in tree.args["where"].find_all(exp.EQ, exp.GTE, exp.GT, exp.LTE, exp.LT):
        col, other = (node.this, node.expression) if isinstance(node.this, exp.Column) else (node.expression, node.this)
        if not isinstance(other, (exp.Parameter, exp.Placeholder)):
            continue
        n = int(other.sql(dialect="postgres").lstrip("$@"))
        v = val[col.name]
        if isinstance(v, datetime) and isinstance(node, (exp.LT, exp.LTE)):
            v = v + timedelta(days=7)
        out[n] = v
    return [out[i] for i in sorted(out)]


def explain_analyze(conn, schema_sql: str, params: list, repeats: int = 5) -> dict:
    """Best-of-N execution time and the top scan type for a query, with concrete parameters."""
    sql = re.sub(r"\$(\d+)", "%s", schema_sql)
    best, node = None, None
    for _ in range(repeats):
        with conn.transaction(force_rollback=True):
            plan = conn.execute(f"explain (analyze, format json) {sql}", params).fetchone()[0][0]
        t = plan["Execution Time"]
        if best is None or t < best:
            best = t
            node = [n.get("Node Type") for n in walk(plan["Plan"]) if "Scan" in (n.get("Node Type") or "")]
    return {"ms": round(best, 3), "scans": node}


def as_json(x) -> str:
    return json.dumps(x, default=str)
