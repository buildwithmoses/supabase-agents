"""The model's job: explain each finding in plain English and draft a fix. Code decides what is applied.

Each finding goes to Claude Haiku 5.5 with only the facts code gathered about it (definitions, stats, plan,
Courtline's access rule). The draft SQL is kept for comparison; in the baseline the applied SQL comes from
fixes.py and is proven in a shadow schema. The `model-writes` regression applies the draft instead.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from ..core.config import get_model
from .spec import ACCESS, policy_expr

SYSTEM = """You are a Postgres and Supabase expert writing for Courtline's engineers (a tennis and pickleball club booking app).
You get one database finding with the facts a script gathered. Write:
- explanation: 2-3 short plain-English sentences: what is wrong and why it matters for Courtline. No jargon without a gloss.
- impact: one sentence on who or what is affected (players, club staff, query speed, security).
- fix_summary: one sentence describing the fix.
- draft_sql: the exact Postgres statements that fix it, schema-qualified with `health.`, ending with semicolons.
- rollback_sql: statements that undo the fix.
Use only the facts given. Do not invent numbers, tables, columns, app screens or features. If an access rule is given, a policy must
implement exactly that rule.
Be precise about Postgres behavior; do not overstate:
- An unindexed foreign key makes Postgres scan the referencing table when a referenced row is DELETED or its KEY changes,
  and makes filters/joins on that column slower. Ordinary updates to other columns don't trigger that scan.
- A duplicate index is maintained on inserts, deletes and updates that touch the indexed column; it wastes space and write work.
- For a SECURITY DEFINER function without a fixed search_path: if the definition schema-qualifies every name, say the
  current risk is low and that pinning search_path is a safeguard against future edits and Supabase's recommendation.
- For a slow query, describe it by the facts (table, filter columns, calls, mean time), not by guessed app features."""


class Explanation(BaseModel):
    explanation: str = Field(description="2-3 plain-English sentences")
    impact: str
    fix_summary: str
    draft_sql: str
    rollback_sql: str


def context(conn, f: dict, schema: str) -> dict:
    t = f["table"]
    ctx: dict = {"finding": {k: f[k] for k in ("id", "lint", "level", "table", "object", "detail")}, "facts": f["facts"]}
    if f["lint"] == "function_search_path_mutable":
        ctx["function_definition"] = conn.execute(
            "select pg_get_functiondef(p.oid) from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = %s and p.proname = %s",
            (schema, t)).fetchone()[0]
        return ctx
    ctx["columns"] = [f"{c} {d}" for c, d in conn.execute(
        "select column_name, data_type from information_schema.columns where table_schema = %s and table_name = %s order by ordinal_position",
        (schema, t)).fetchall()]
    ctx["indexes"] = [r[0] for r in conn.execute("select indexdef from pg_indexes where schemaname = %s and tablename = %s", (schema, t)).fetchall()]
    ctx["policies"] = [f"{n}: {c} to {r} using ({q})" for n, c, r, q in conn.execute(
        "select policyname, cmd, roles::text, qual from pg_policies where schemaname = %s and tablename = %s", (schema, t)).fetchall()]
    if t in ACCESS and f["lint"] in ("rls_disabled_in_exposed_schema", "rls_enabled_no_policy", "permissive_policy", "auth_rls_initplan"):
        ctx["courtline_access_rule"] = {"visibility": ACCESS[t]["visibility"], "policy_using_expression": policy_expr(t, schema)}
        ctx["helper_functions"] = f"{schema}.is_member(club_id) and {schema}.is_staff(club_id) return true if the signed-in user is a member / staff of that club."
    return ctx


def _call(llm, ctx: dict, callbacks) -> Explanation:
    msg = [("system", SYSTEM), ("human", json.dumps(ctx, default=str, indent=1))]
    for attempt in range(4):
        try:
            return llm.invoke(msg, config={"callbacks": callbacks or []})
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if attempt == 3 or not any(k in name for k in ("RateLimit", "Timeout", "APIConnection", "InternalServer", "ServiceUnavailable", "APIStatus")):
                if attempt == 3:
                    raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def explain_all(conn, findings: list[dict], schema: str, callbacks=None, workers: int = 5) -> dict[str, dict]:
    llm = get_model(max_tokens=1500).with_structured_output(Explanation, method="json_schema")
    ctxs = {f["id"]: context(conn, f, schema) for f in findings}
    with ThreadPoolExecutor(workers) as ex:
        futs = {fid: ex.submit(_call, llm, ctx, callbacks) for fid, ctx in ctxs.items()}
        out = {}
        for fid, fut in futs.items():
            try:
                out[fid] = fut.result().model_dump() | {"context": ctxs[fid]}   # kept so a judge sees the same facts
            except Exception as e:  # noqa: BLE001 - an explanation failure must not block the code path
                out[fid] = {"explanation": f"(model unavailable: {type(e).__name__}) {next(f['detail'] for f in findings if f['id'] == fid)}",
                            "impact": "", "fix_summary": "", "draft_sql": "", "rollback_sql": "", "error": str(e)[:200], "context": ctxs[fid]}
    return out


def split_sql(sql: str) -> list[dict]:
    """Model-drafted SQL -> statements (used only by the model-writes regression and the draft check)."""
    import re

    stmts = [s.strip() for s in re.split(r";\s*(?:\n|$)", sql or "") if s.strip() and not s.strip().startswith("--")]
    return [{"sql": s.rstrip(";") + ";", "concurrent": bool(re.search(r"\bconcurrently\b", s, re.I))} for s in stmts]
