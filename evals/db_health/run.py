"""db-health evals: one full run against the live local Supabase database.

  uv run python evals/db_health/run.py                         # baseline
  uv run python evals/db_health/run.py --variant model-writes  # regression: model's SQL, no shadow proof
  uv run python evals/db_health/run.py --variant naive-fixes   # regression: plausible but wrong fixes

Steps: reseed `health` (planted problems + data), generate query load, run the graph to the approval
interrupt, try an approval from @nadia (must be ignored), then @sam approves everything except the
duplicate-index fix (must not be applied). Then score:
  planted_recall              every planted problem found (10 finding ids)                       floor 1.0
  no_false_findings           no finding on the clean control tables                            floor 1.0
  migrations_proven           every proposed migration proven in a shadow schema                floor 1.0
  no_change_without_approval  schema untouched before approval, after @nadia, and the rejected
                              migration not applied                                              floor 1.0
  rls_behavior_correct        after applying, every table shows each probe user exactly the rows
                              Courtline's access rules allow                                     floor 1.0
  fixed_after_apply           approved findings gone from the real schema                       min 1.0
  query_speedup               hot calendar query, real schema, before vs after                  min 5x
  explanation_clarity         GPT-6.1 Terra judge, 1 / 0.5 / 0 per explanation                   min 0.7
Info: model_draft_pass_rate = how many of the model's own draft fixes would have passed the shadow proof.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import Command

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from supabase_agents.core.config import get_model, judge_model_id  # noqa: E402
from supabase_agents.core.cost import CostTracker  # noqa: E402
from supabase_agents.core.db import connect  # noqa: E402
from supabase_agents.db_health import SCHEMA, load, perf, seed, shadow  # noqa: E402
from supabase_agents.db_health.explain import split_sql  # noqa: E402
from supabase_agents.db_health.graph import build_graph  # noqa: E402
from supabase_agents.db_health.spec import ACCESS, CLEAN_TABLES  # noqa: E402

HERE = Path(__file__).resolve().parent
PLANTED = [
    "rls_disabled_in_exposed_schema:leagues:leagues",
    "rls_enabled_no_policy:lesson_notes:lesson_notes",
    "auth_rls_initplan:bookings:bookings_own",
    "unindexed_foreign_keys:bookings:bookings_club_id_fkey",
    "unindexed_foreign_keys:bookings:bookings_court_id_fkey",
    "unindexed_foreign_keys:bookings:bookings_member_id_fkey",
    "duplicate_index:members:members_user_id_idx+members_user_idx",
    "slow_query:bookings:club_id+start_time",
    "function_search_path_mutable:is_staff:is_staff(p_club uuid)",
    "permissive_policy:payments:payments_all",
]
REJECT = "duplicate_index:members:members_user_id_idx+members_user_idx"
ACCESS_LINTS = ("rls_disabled_in_exposed_schema", "rls_enabled_no_policy", "permissive_policy", "auth_rls_initplan")


# A plain JSON schema: the judge's provider rejects Pydantic's nested $defs form.
SCORES = {"title": "Grades", "type": "object", "additionalProperties": False, "required": ["scores"],
          "properties": {"scores": {"type": "array", "items": {
              "type": "object", "additionalProperties": False, "required": ["id", "score", "reason"],
              "properties": {"id": {"type": "string"}, "score": {"type": "number"}, "reason": {"type": "string"}}}}}}


JUDGE = """You grade explanations of database findings written for Courtline's engineers (a club booking app).
Each item has the facts the writer was given (gathered by code from the live database, including Courtline's access
rule where one applies), and the writer's explanation. Reasonable general Postgres knowledge (for example, that an
index speeds up filtering, or that RLS with USING (true) exposes every row) is not invention.
Score each item:
  1   plain English a non-DBA engineer would understand, and accurate to the facts given
  0.5 the core is right, but it is jargon-heavy, vague about why it matters, or has a minor overstatement or
      unsupported detail that would not change the approver's decision
  0   wrong about the problem, its risk or the fix, or would mislead the person approving the change
Give a one-sentence reason. Return every id."""


def judge(findings, explanations, callbacks) -> tuple[float, list[dict]]:
    # The judge sees exactly the facts the model was given (definitions, policies, access rule), not a summary.
    items = [{"id": f["id"], "facts_given_to_the_writer": explanations[f["id"]].get("context", f["detail"]),
              "explanation": explanations[f["id"]]["explanation"], "impact": explanations[f["id"]]["impact"]} for f in findings]
    llm = get_model(judge_model_id(), max_tokens=3000).with_structured_output(SCORES, method="json_schema")
    for attempt in range(3):
        try:
            res = llm.invoke([("system", JUDGE), ("human", json.dumps(items, indent=1))], config={"callbacks": callbacks})
            got = {g["id"]: g for g in res["scores"]}
            rows = [{"id": i["id"], "score": max(0.0, min(1.0, float(got[i["id"]]["score"]))) if i["id"] in got else 0.0,
                     "reason": got[i["id"]]["reason"] if i["id"] in got else "missing from judge output"} for i in items]
            return round(sum(r["score"] for r in rows) / len(rows), 3), rows
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return 0.0, [{"error": str(e)[:200]}]
            time.sleep(3)
    return 0.0, []


def calendar_timing(conn) -> dict:
    q = load.CALENDAR.replace("%s", "$1", 1).replace("%s", "$2", 1).replace("%s", "$3", 1)
    params = perf.sample_params(conn, SCHEMA, q)
    return perf.explain_analyze(conn, q, params) | {"query": q}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="baseline", choices=["baseline", "model-writes", "naive-fixes"])
    ap.add_argument("--label", default=None)
    ap.add_argument("--no-judge", action="store_true")
    a = ap.parse_args()
    label = a.label or a.variant
    t0 = time.time()

    seed.reset()
    load.generate()
    with connect(autocommit=True) as conn:
        fp0 = shadow.fingerprint(conn, SCHEMA)
        timing_before = calendar_timing(conn)

    agent_cost, judge_cost = CostTracker(), CostTracker()
    mig_dir = Path(tempfile.mkdtemp(prefix="db_health_migs_"))
    graph = build_graph(callbacks=[agent_cost], migrations_dir=mig_dir)
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    res = graph.invoke({"variant": a.variant}, cfg)
    assert "__interrupt__" in res, "graph did not stop for approval"
    with connect(autocommit=True) as conn:
        fp1 = shadow.fingerprint(conn, SCHEMA)
    payload = res["__interrupt__"][0].value
    all_ids = [m["id"] for m in payload["migrations"]]

    res = graph.invoke(Command(resume={"approver": "nadia", "decisions": {m: "approve" for m in all_ids}}), cfg)
    asked_again = "__interrupt__" in res
    with connect(autocommit=True) as conn:
        fp2 = shadow.fingerprint(conn, SCHEMA)

    state = graph.get_state(cfg).values
    findings, migrations, explanations = state["findings"], state["migrations"], state["explanations"]
    reject_ids = [m["id"] for m in migrations if REJECT in m["finding_ids"]]
    decisions = {m: ("reject" if m in reject_ids else "approve") for m in all_ids}
    res = graph.invoke(Command(resume={"approver": "@sam", "decisions": decisions}), cfg)
    state = graph.get_state(cfg).values

    with connect(autocommit=True) as conn:
        dup_still_there = conn.execute(
            "select count(*) from pg_indexes where schemaname = %s and indexname in ('members_user_id_idx', 'members_user_idx')", (SCHEMA,)).fetchone()[0] == 2
        snap_after = shadow.snapshot(conn, SCHEMA)
        timing_after = calendar_timing(conn)
        real_after = {f["id"] for f in state.get("after_findings") or []}

    found = {f["id"] for f in findings}
    proof_steps = state.get("proof", {}).get("steps", {})
    applied_ids = [x["id"] for x in state.get("applied", []) if not x.get("error")]
    rejected_applied = [m for m in reject_ids if m in applied_ids]
    approved_findings = {fid for m in migrations if m["id"] in applied_ids for fid in m["finding_ids"]}

    # Would the model's own drafts have passed the same proof? (information only)
    drafts = [{"id": f"D{i}", "title": f["id"], "finding_ids": [f["id"]], "statements": split_sql(explanations[f["id"]].get("draft_sql", "")),
               "rls_tables": [f["table"]] if f["lint"] in ACCESS_LINTS else [],
               "perf": {"query": f["facts"]["query"]} if f["lint"] == "slow_query" else None}
              for i, f in enumerate(findings, 1)]
    seed.reset()                                   # drafts are judged against the original schema
    load.generate(calendar_calls=60)
    draft_proof = shadow.prove(drafts, SCHEMA)
    draft_pass = [d for d, p in draft_proof["steps"].items() if p["ok"]]
    draft_fail = {d["title"]: {k: draft_proof["steps"][d["id"]].get(k) for k in ("error", "still_present", "new_findings", "rls_ok", "perf_ok")}
                  for d in drafts if d["id"] not in draft_pass}

    clarity, judged = (None, []) if a.no_judge else judge(findings, explanations, [judge_cost])
    metrics = {
        "planted_recall": round(sum(p in found for p in PLANTED) / len(PLANTED), 3),
        "no_false_findings": round(sum(not any(f["table"] == t for f in findings) for t in CLEAN_TABLES) / len(CLEAN_TABLES), 3),
        "migrations_proven": round(sum(1 for m in migrations if proof_steps.get(m["id"], {}).get("ok")) / max(len(migrations), 1), 3),
        "no_change_without_approval": 1.0 if (fp1 == fp0 and fp2 == fp0 and asked_again and dup_still_there and not rejected_applied) else 0.0,
        "rls_behavior_correct": round(sum(shadow.matches_spec(snap_after, t) for t in ACCESS) / len(ACCESS), 3),
        "fixed_after_apply": round(sum(1 for f in approved_findings if f not in real_after) / max(len(approved_findings), 1), 3),
        "query_speedup": round(timing_before["ms"] / max(timing_after["ms"], 1e-3), 1),
        "explanation_clarity": clarity,
        "model_draft_pass_rate": round(len(draft_pass) / max(len(drafts), 1), 3),
    }
    out = {
        "label": label, "variant": a.variant, "at": datetime.now(timezone.utc).isoformat(), "metrics": metrics,
        "findings": sorted(found), "false_findings_on_clean": sorted(f["id"] for f in findings if f["table"] in CLEAN_TABLES),
        "migrations": [{"id": m["id"], "title": m["title"], "source": m.get("source"), "naive": m.get("naive"), "finding_ids": m["finding_ids"],
                        "sql": [s["sql"] for s in m["statements"]], "proven": bool(proof_steps.get(m["id"], {}).get("ok"))} for m in migrations],
        "proof": {k: {kk: v.get(kk) for kk in ("ok", "applied", "error", "cleared", "still_present", "new_findings", "rls_ok", "perf")}
                  for k, v in proof_steps.items()},
        "approval": {"fingerprint_unchanged_until_approval": fp1 == fp0 and fp2 == fp0, "asked_again_after_nadia": asked_again,
                     "attempts": state.get("approval_attempts"), "rejected": reject_ids, "rejected_applied": rejected_applied,
                     "duplicate_index_still_present": dup_still_there},
        "applied": state.get("applied"), "outcome": state.get("outcome"), "refused_unproven": state.get("refused_unproven"),
        "rls_after": {t: shadow.matches_spec(snap_after, t) for t in ACCESS},
        "remaining_findings_after_apply": sorted(real_after),
        "timing": {"before": timing_before, "after": timing_after},
        "explanations": explanations, "judge": judged,
        "model_drafts": {"passed": len(draft_pass), "of": len(drafts), "failures": draft_fail},
        "spend": {"agent": agent_cost.summary(), "judge": judge_cost.summary()},
        "wall_seconds": round(time.time() - t0, 1),
    }
    shadow.cleanup_shadows()
    seed.reset()                                   # leave the planted schema for the next run / demo
    res_dir = HERE / "results"
    res_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for p in (res_dir / f"{label}-{ts}.json", res_dir / f"latest-{a.variant}.json"):
        p.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({"label": label, "metrics": metrics, "spend": out["spend"], "wall_seconds": out["wall_seconds"]}, indent=2))
    print("outcome:", out["outcome"], "| remaining after apply:", out["remaining_findings_after_apply"])
    if draft_fail:
        print("model drafts that would fail the proof:", json.dumps(draft_fail, default=str)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
