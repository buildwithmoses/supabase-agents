"""Eval for the Firebase -> Supabase migration assistant.

Runs the real graph three times against the local Supabase stack:
  1. resume with a rejection           -> schema `migration` must be untouched
  2. resume "approved" by someone else -> untouched
  3. resume approved by @sam           -> applied, and the applied schema must match the proof
Scores the proof and labels in fixtures/firebase/expected.json, and has GPT-6.1 Terra judge the report.

    uv run python evals/firebase_migration/run.py [--variant baseline|model-rls|no-proof] [--label x]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import Command
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from supabase_agents.core.config import data_dir  # noqa: E402
from supabase_agents.core.cost import CostTracker  # noqa: E402
from supabase_agents.core.db import connect  # noqa: E402
from supabase_agents.firebase_migration.graph import TARGET_SCHEMA, build_graph, grounding, new_state, render_report  # noqa: E402
from supabase_agents.firebase_migration.llm import structured  # noqa: E402

HERE = Path(__file__).resolve().parent


def fingerprint() -> dict:
    with connect() as conn:
        tables = [r[0] for r in conn.execute("select table_name from information_schema.tables where table_schema = %s order by 1", (TARGET_SCHEMA,)).fetchall()]
        meta = []
        if "_migration_meta" in tables:
            meta = [r[0] for r in conn.execute(f"select run_id from {TARGET_SCHEMA}._migration_meta").fetchall()]
        counts = {t: conn.execute(f"select count(*) from {TARGET_SCHEMA}.{t}").fetchone()[0] for t in tables}
    return {"tables": tables, "meta": meta, "counts": counts}


def run_once(variant: str, resume: dict) -> dict:
    g = build_graph()
    st = new_state(variant)
    cfg = {"configurable": {"thread_id": st["run_id"]}}
    t0 = time.time()
    out = g.invoke(st, cfg)
    if not out.get("__interrupt__"):
        raise RuntimeError("graph did not stop for approval")
    before = fingerprint()
    g.invoke(Command(resume=resume), cfg)
    after = fingerprint()
    snap = g.get_state(cfg).values
    return {"state": snap, "before": before, "after": after, "seconds": round(time.time() - t0, 1)}


class Judgement(BaseModel):
    accuracy: float = Field(description="0-1: every claim in the report is supported by the facts")
    completeness: float = Field(description="0-1: skips, value fixes, flagged rules, proof results and a usable cutover checklist are all there")
    clarity: float = Field(description="0-1: a database owner could approve or reject from this alone")
    issues: list[str]


def judge(report_md: str, facts: dict, tracker) -> dict:
    j = structured(Judgement, "You grade a database migration report for the engineer who must approve it. Grade strictly against the facts. "
                   "Wrong numbers or claims the facts don't support lower accuracy.",
                   f"FACTS (everything the report was built from):\n{json.dumps(facts, indent=1, default=str)}\n\nREPORT:\n{report_md}", judge=True, callbacks=[tracker])
    d = j.model_dump()
    d["score"] = round((d["accuracy"] + d["completeness"] + d["clarity"]) / 3, 3)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--label")
    ap.add_argument("--no-judge", action="store_true")
    a = ap.parse_args()
    label = a.label or a.variant
    exp = json.loads(data_dir("firebase", "expected.json").read_text())
    runs = {"reject": run_once(a.variant, {"approved": False, "approver": "sam"}),
            "wrong_approver": run_once(a.variant, {"approved": True, "approver": "dana"}),
            "approve": run_once(a.variant, {"approved": True, "approver": "sam"})}
    st = runs["approve"]["state"]
    proof = st.get("proof")
    conv, tr = st["converted"], st["translation"]

    untouched = [runs[k]["before"] == runs[k]["after"] for k in ("reject", "wrong_approver")]
    applied = st.get("applied", {})
    after = runs["approve"]["after"]
    applied_ok = bool(applied.get("applied")) and applied.get("run_id") in after["meta"]
    if proof and applied_ok:
        trial_rows = {t: v["rows"] for t, v in proof["counts"]["tables"].items()}
        applied_ok = all(after["counts"].get(t) == n for t, n in trial_rows.items())
    should_apply = bool(proof and proof["passed"])
    approval_respected = float(all(untouched) and (applied_ok if should_apply else not applied.get("applied")))

    skip_map = {s["path"]: s["reason"] for s in conv["skips"]}
    fix_where = {f["where"] for f in conv["fixes"]}
    labels_ok = skip_map == exp["must_skip"] and all(w in fix_where for w in exp["must_fix"])
    flag_keys = {f.get("key") for f in tr["flags"]}

    m = {}
    if proof:
        m["rules_match"] = proof["rules_match"]
        m["counts_reconciled"] = float(proof["counts"]["ok"] and labels_ok)
        m["referential_integrity"] = float(proof["integrity"]["ok"] and proof["integrity"]["fk_constraints"] > 0)
        m["unsupported_flagged"] = float(flag_keys >= set(exp["flagged_rules"]) and proof["flagged_fail_closed"] == 1.0)
        m["field_fidelity"] = proof["fidelity"]["fidelity"]
    m["approval_respected"] = approval_respected
    schema = st["schema"]["tables"]
    jsonb = {f"{t['name'] if False else ck.split('/')[-1]}.{c['source']}" for ck, t in schema.items() for c in t["columns"] if c["type"] == "jsonb"}
    flat = {f"{ck.split('/')[-1]}.{c['source']}" for ck, t in schema.items() for c in t["columns"] if c.get("kind") == "flatten"}
    joins = {f"{ck.split('/')[-1]}.{j['field']}" for ck, t in schema.items() for j in t["join"]}
    m["schema_decisions"] = float(jsonb == set(exp["jsonb_fields"]) and flat == set(exp["flattened_maps"]) and joins == set(exp["join_tables"]))

    tracker = CostTracker()
    report_md = render_report(st)
    if not a.no_judge:
        jd = judge(report_md, grounding(st), tracker)
        m["report_quality"] = jd["score"]
    else:
        jd = None
    out = {"label": label, "variant": a.variant, "at": datetime.now(timezone.utc).isoformat(), "metrics": m,
           "proof": None if not proof else {k: proof[k] for k in ("counts", "rules_match", "flagged_fail_closed", "passed")} |
                    {"integrity": {k: proof["integrity"][k] for k in ("ok", "orphans", "fk_constraints")},
                     "fidelity": {k: proof["fidelity"][k] for k in ("fidelity", "fields_checked", "fields_from_logged_fixes", "mismatches")},
                     "rules_tests": proof["rules_tests"]},
           "flags": tr["flags"], "skips": conv["skips"], "fixes": conv["fixes"], "applied": applied,
           "approval_runs": {k: {"untouched": runs[k]["before"] == runs[k]["after"], "applied": runs[k]["state"].get("applied"),
                                 "seconds": runs[k]["seconds"]} for k in runs},
           "review": st.get("review"), "summary_mode": st["report"]["summary_mode"], "judge": jd,
           "spend": {"agent_runs": [runs[k]["state"].get("cost") for k in runs], "judge": tracker.summary()},
           "sql_path": st["sql_path"]}
    (HERE / "results").mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    for name in (f"{label}-{ts}.json", f"latest-{a.variant}.json"):
        (HERE / "results" / name).write_text(json.dumps(out, indent=2, default=str))
    (HERE / "results" / f"report-{label}.md").write_text(report_md)
    print(json.dumps({"label": label, "metrics": m, "spend": out["spend"]}, indent=2, default=str))
    for r in (proof or {}).get("rules_tests", []):
        if not r["match"] and not r["flagged_rule"]:
            print(f"  MISMATCH {r['id']}: expected {r['expected']}, got {r['got']} {r['detail']}")
        if r["flagged_rule"] and not r.get("fail_closed"):
            print(f"  NOT FAIL-CLOSED {r['id']}: got {r['got']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
