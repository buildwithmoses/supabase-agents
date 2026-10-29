"""The migration agent as a LangGraph graph.

analyze (code) -> review (model explains decisions, may rename tables; code validates) -> build (code: rows,
RLS, SQL) -> prove (code, throwaway schema) -> report (model summary + code facts and checklist)
-> approval (interrupt: only @sam can approve) -> apply (into schema `migration`, only if proven and approved)

Variants (for the eval): "baseline"; "model-rls" (the model writes the RLS policies from the rules file
instead of the code translator); "no-proof" (skips the trial schema).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from ..core.config import data_dir
from ..core.cost import CostTracker
from . import prove as P
from .export import load, walk
from .infer import convert_rows, infer, uid_to_uuid
from .llm import structured
from .rules import parse_rules, translate
from .schema_info import policy_info
from .sqlgen import migration_sql

APPROVER = "sam"
TARGET_SCHEMA = "migration"
OUT_DIR = Path(__file__).resolve().parents[3] / "evals" / "firebase_migration" / "out"
IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
RESERVED = {"user", "order", "group", "table", "select", "where", "from", "references"}


class State(TypedDict, total=False):
    variant: str
    run_id: str
    export_path: str
    rules_path: str
    tests_path: str
    schema: dict
    review: dict
    translation: dict
    converted: dict
    sql: str
    sql_path: str
    proof: dict | None
    report: dict
    decision: dict
    applied: dict
    cost: dict


class Explanation(BaseModel):
    item: str = Field(description="the item exactly as given, e.g. 'payments.metadata'")
    explanation: str = Field(description="one or two plain sentences: what was decided and why it is safe or what to watch")


class Rename(BaseModel):
    old: str
    new: str
    why: str


class Review(BaseModel):
    explanations: list[Explanation]
    table_renames: list[Rename] = Field(default_factory=list, description="optional clearer snake_case table names; may be empty")


class Summary(BaseModel):
    summary: str = Field(description="<= 120 words for the database owner. Use only numbers present in the facts.")
    risks: list[str] = Field(description="2-4 concrete risks for cutover, each one sentence")


class ModelPolicies(BaseModel):
    helper_functions_sql: list[str] = Field(default_factory=list, description="any CREATE FUNCTION statements the policies need, using {S}")
    policies_sql: list[str] = Field(description="CREATE POLICY statements, one per item, using {S} as the schema name")
    untranslatable: list[str] = Field(default_factory=list, description="rules you could not translate, as 'collection:operation'")


def _cost(state) -> CostTracker:
    t = CostTracker()
    c = state.get("cost") or {}
    t.cost_usd = c.get("cost_usd", 0.0); t.calls = c.get("model_calls", 0)
    return t


def analyze(state: State) -> dict:
    docs = walk(load(state["export_path"]))
    return {"schema": infer(docs)}


def review(state: State) -> dict:
    schema = state["schema"]
    amb = schema["ambiguities"]
    tables = {ck: t["name"] for ck, t in schema["tables"].items()}
    facts = {"tables": sorted(tables.values()), "decisions": [{"item": a["item"], "kind": a["kind"], "detail": a["detail"]} for a in amb if a["kind"] != "nullable"]}
    tracker = _cost(state)
    out = structured(Review, "You review a Firestore-to-Postgres schema that code already decided. Explain each decision for the "
                     "database owner in plain words. Do not change types. You may suggest clearer table names.",
                     json.dumps(facts, indent=1), callbacks=[tracker])
    accepted, rejected = [], []
    names = set(tables.values())
    for r in out.table_renames:
        ok = r.old in names and bool(IDENT.match(r.new)) and r.new not in RESERVED and r.new not in names and r.new != r.old
        (accepted if ok else rejected).append(r.model_dump())
        if ok:
            names.discard(r.old); names.add(r.new)
    for r in accepted:
        for t in schema["tables"].values():
            if t["name"] == r["old"]:
                t["name"] = r["new"]
                for j in t["join"]:
                    j["name"] = r["new"] + j["name"][len(r["old"]):]
    explained = {e.item for e in out.explanations}
    missing = [d["item"] for d in facts["decisions"] if d["item"] not in explained]
    return {"schema": schema, "review": {"explanations": [e.model_dump() for e in out.explanations], "renames_accepted": accepted,
                                         "renames_rejected": rejected, "unexplained": missing},
            "cost": tracker.summary()}


def build(state: State) -> dict:
    schema = state["schema"]
    docs = walk(load(state["export_path"]))
    conv = convert_rows(docs, schema)
    rules_src = Path(state["rules_path"]).read_text()
    tracker = _cost(state)
    if state.get("variant") == "model-rls":
        info = policy_info(schema)
        listing = {i["table"]: {"columns": i["types"], "firebase_field_to_column": i["columns"], "parent_key_column": i["parent_col"],
                                "doc_id_is_user_column": i["id_uid_col"]} for i in info.values()}
        out = structured(ModelPolicies, "Translate these Firestore security rules into Postgres row-level security policies for Supabase. "
                         "Tables already exist in schema {S} with RLS enabled. auth.uid() returns the user's uuid; Firebase uid fields "
                         "were migrated to uuid columns (see firebase_field_to_column). Write CREATE POLICY statements.",
                         f"RULES:\n{rules_src}\n\nTABLES:\n{json.dumps(listing, indent=1)}", callbacks=[tracker], max_tokens=12000)
        translation = {"policies": [{"sql": _wrap(s), "deny": False, "key": None} for s in out.helper_functions_sql + out.policies_sql], "helpers": {},
                       "flags": [{"key": k.strip(), "rule": k, "line": 0, "reason": "model said it could not translate", "expr": ""} for k in out.untranslatable]}
    else:
        translation = translate(parse_rules(rules_src), policy_info(schema))
    header = f"-- Firestore -> Supabase migration, run {state['run_id']} ({state.get('variant', 'baseline')})\n-- Generated by code from {Path(state['export_path']).name} and {Path(state['rules_path']).name}"
    sql = migration_sql(schema, translation, conv, header)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sql_path = OUT_DIR / f"migration-{state['run_id']}.sql"
    sql_path.write_text(sql)
    return {"translation": translation, "converted": _jsonable(conv), "sql": sql, "sql_path": str(sql_path), "cost": tracker.summary()}


def _wrap(stmt: str) -> str:
    """Model-written policies run one by one; a broken one is reported, not fatal."""
    body = stmt.strip().rstrip(";")
    return f"do $fbpol$ begin execute $q${body}$q$; exception when others then raise warning 'model policy failed: %', sqlerrm; end $fbpol$;"


def _jsonable(conv: dict) -> dict:
    def fix(v):
        return v.isoformat() if isinstance(v, datetime) else v
    return {"rows": {k: [{c: fix(v) for c, v in r.items()} for r in rows] for k, rows in conv["rows"].items()},
            "join_rows": conv["join_rows"], "skips": conv["skips"], "fixes": conv["fixes"]}


def prove_node(state: State) -> dict:
    if state.get("variant") == "no-proof":
        return {"proof": None}
    docs = walk(load(state["export_path"]))
    conv = convert_rows(docs, state["schema"])   # fresh python values (datetimes) for the comparisons
    tests = json.loads(Path(state["tests_path"]).read_text())["tests"]
    trial = "migration_trial_" + re.sub(r"[^a-z0-9]", "_", state["run_id"].lower())   # unquoted names are lowercase in Postgres
    proof = P.prove(trial, state["sql"], state["schema"], docs, conv, tests, state["translation"]["flags"])
    proof["trial_schema"] = trial
    return {"proof": proof}


def facts_for_report(state: State) -> dict:
    conv, tr, proof = state["converted"], state["translation"], state.get("proof")
    rows = {k: len(v) for k, v in conv["rows"].items()}
    return {"rows_loaded": sum(rows.values()), "skipped": len(conv["skips"]), "fixed_values": len(conv["fixes"]),
            "tables": len(state["schema"]["tables"]), "join_tables": sum(len(t["join"]) for t in state["schema"]["tables"].values()),
            "policies": sum(1 for p in tr["policies"] if not p["deny"]), "flagged_rules": [f["key"] or f["rule"] for f in tr["flags"]],
            "skips": conv["skips"], "fixes": [{"where": f["where"], "fix": f["fix"]} for f in conv["fixes"]],
            "proof": None if proof is None else {"passed": proof["passed"],
                                                 "regular_rules_tests": sum(1 for x in proof["rules_tests"] if not x["flagged_rule"]),
                                                 "regular_rules_tests_matching_firebase": sum(1 for x in proof["rules_tests"] if not x["flagged_rule"] and x["match"]),
                                                 "flagged_rule_tests": sum(1 for x in proof["rules_tests"] if x["flagged_rule"]),
                                                 "flagged_rule_tests_denied_as_intended": sum(1 for x in proof["rules_tests"] if x["flagged_rule"] and x.get("fail_closed")),
                                                 "fields_checked": proof["fidelity"]["fields_checked"], "fidelity": proof["fidelity"]["fidelity"],
                                                 "orphans": proof["integrity"]["orphans"]}}


def grounding(state: dict) -> dict:
    """Everything the report is built from (what a grader may check claims against)."""
    proof = state.get("proof")
    users = {v for rows in state["converted"]["rows"].values() for r in rows for c, v in r.items() if c in ("user_id", "owner_id", "member_id") and v}
    return {"facts": state["report"]["facts"],
            "tables": [t["name"] for t in state["schema"]["tables"].values()],
            "join_tables": [j["name"] for t in state["schema"]["tables"].values() for j in t["join"]],
            "schema_decisions_made_by_code": state["schema"]["ambiguities"],
            "flagged_rules_with_reasons": state["translation"]["flags"],
            "uid_mapping": "Firebase uids are mapped to uuid columns with uuid5 (fixed namespace) so auth.uid() can be compared to them",
            "distinct_firebase_users_referenced": len(users),
            "join_table_policies": "join tables get a SELECT policy only (readable when the parent row is readable); no write policies",
            "proof_counts": None if not proof else proof["counts"],
            "proof_integrity": None if not proof else {k: proof["integrity"][k] for k in ("ok", "orphans", "fk_constraints")},
            "checklist": state["report"]["checklist"]}


def numbers_ok(text: str, facts: dict) -> list[str]:
    blob = json.dumps(facts)
    bad = []
    for n in re.findall(r"\d+(?:\.\d+)?", text):
        if n not in blob and n not in ("1", "2", "0"):
            bad.append(n)
    return bad


def checklist(state: State) -> list[str]:
    tr, schema = state["translation"], state["schema"]
    uids = set()
    for rows in state["converted"]["rows"].values():
        for r in rows:
            for c, v in r.items():
                if c in ("user_id", "owner_id", "member_id") and v:
                    uids.add(v)
    items = [
        "Freeze writes on the Firebase clubs (deploy read-only rules) at the cutover time and announce a 30-minute window.",
        "Re-export Firestore after the freeze and re-run this migration; the proof must pass again on the fresh export.",
        f"Import Firebase Auth users into Supabase Auth with the same uuid5 ids this migration used ({len(uids)} distinct users referenced), so auth.uid() matches every migrated row.",
    ]
    for f in tr["flags"]:
        items.append(f"Write the policy for flagged rule {f.get('key') or f['rule']} by hand ({f['reason']}). Until then that access is denied.")
    joins = [j["name"] for t in schema["tables"].values() for j in t["join"]]
    if joins:
        items.append(f"Update the app: array fields now live in join tables ({', '.join(joins)}), which are read-only to users until write policies are added.")
    items += ["Point the app's reads and writes at Supabase, keep Firebase read-only for 7 days as the rollback path.",
              "Run the Supabase security and performance advisors on the new schema before opening traffic."]
    return items


def report(state: State) -> dict:
    facts = facts_for_report(state)
    tracker = _cost(state)
    s = structured(Summary, "Write a short migration summary for the database owner who must approve it. Use only the facts given. "
                   "Say plainly whether the proof passed, what was skipped or changed, and which rules need a human.",
                   json.dumps(facts, indent=1), callbacks=[tracker])
    bad = numbers_ok(s.summary + " ".join(s.risks), facts)
    text, mode = s.summary, "model"
    if bad:
        p = facts["proof"]
        text = (f"Proof {'passed' if p and p['passed'] else 'did not run or failed'}. {facts['rows_loaded']} rows loaded into "
                f"{facts['tables']} tables, {facts['skipped']} documents skipped and {facts['fixed_values']} values fixed, all listed below. "
                f"{len(facts['flagged_rules'])} rules need a human: {', '.join(facts['flagged_rules']) or 'none'}.")
        mode = "template (model summary used numbers not in the facts: " + ", ".join(bad) + ")"
    return {"report": {"summary": text, "risks": s.risks, "summary_mode": mode, "facts": facts, "checklist": checklist(state),
                       "explanations": state.get("review", {}).get("explanations", [])}, "cost": tracker.summary()}


def approval(state: State) -> dict:
    proof = state.get("proof")
    decision = interrupt({"ask": f"@{APPROVER}: apply this migration to schema `{TARGET_SCHEMA}`?",
                          "proof_passed": None if proof is None else proof["passed"], "sql_path": state["sql_path"],
                          "flagged_rules": state["report"]["facts"]["flagged_rules"], "summary": state["report"]["summary"]})
    return {"decision": decision if isinstance(decision, dict) else {"approved": False}}


def apply(state: State) -> dict:
    d = state.get("decision") or {}
    proof = state.get("proof")
    if not (d.get("approved") is True and d.get("approver") == APPROVER):
        return {"applied": {"applied": False, "why": "not approved by @" + APPROVER if d.get("approved") else "rejected"}}
    if state.get("variant") != "no-proof" and not (proof and proof["passed"]):
        return {"applied": {"applied": False, "why": "proof did not pass"}}
    P.drop_schema(TARGET_SCHEMA)
    P.run_sql(state["sql"].replace("{S}", TARGET_SCHEMA) +
              f"\ncreate table {TARGET_SCHEMA}._migration_meta (run_id text primary key, approved_by text not null, applied_at timestamptz not null default now());"
              f"\ninsert into {TARGET_SCHEMA}._migration_meta (run_id, approved_by) values ('{state['run_id']}', '{APPROVER}');")
    return {"applied": {"applied": True, "schema": TARGET_SCHEMA, "run_id": state["run_id"], "approved_by": APPROVER,
                        "at": datetime.now(timezone.utc).isoformat()}}


def build_graph(checkpointer=None):
    g = StateGraph(State)
    for name, fn in [("analyze", analyze), ("review", review), ("build", build), ("prove", prove_node), ("report", report),
                     ("approval", approval), ("apply", apply)]:
        g.add_node(name, fn)
    g.add_edge(START, "analyze")
    for a, b in [("analyze", "review"), ("review", "build"), ("build", "prove"), ("prove", "report"), ("report", "approval"),
                 ("approval", "apply"), ("apply", END)]:
        g.add_edge(a, b)
    return g.compile(checkpointer=checkpointer or InMemorySaver())


def new_state(variant: str = "baseline", export_path=None, rules_path=None, tests_path=None) -> State:
    return {"variant": variant, "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4],
            "export_path": str(export_path or data_dir("firebase", "export.json")),
            "rules_path": str(rules_path or data_dir("firebase", "firestore.rules")),
            "tests_path": str(tests_path or data_dir("firebase", "rules_tests.json"))}


def render_report(state: dict) -> str:
    r, f = state["report"], state["report"]["facts"]
    proof = state.get("proof")
    lines = [f"# Firestore -> Supabase migration report ({state['run_id']})", "", r["summary"], "",
             f"Summary written by: {r['summary_mode']}", "", "## Proof (trial schema)", ""]
    if proof:
        lines += ["| Table | Source docs | Rows | Skipped | Reconciled |", "|---|---|---|---|---|"]
        lines += [f"| {t} | {v['source_docs']} | {v['rows']} | {v['skipped']} | {'yes' if v['reconciled'] else 'NO'} |" for t, v in proof["counts"]["tables"].items()]
        lines += ["", f"- Referential integrity: {proof['integrity']['orphans']} orphans across {proof['integrity']['fk_constraints']} foreign keys",
                  f"- Field fidelity: {proof['fidelity']['fidelity']} over {proof['fidelity']['fields_checked']} fields",
                  f"- Rules tests: {proof['rules_match']} match Firebase on {sum(1 for x in proof['rules_tests'] if not x['flagged_rule'])} tests; "
                  f"flagged rules fail closed: {proof['flagged_fail_closed']}", ""]
    else:
        lines += ["Not run.", ""]
    lines += ["## Skipped documents", ""] + [f"- `{s['path']}`: {s['reason']} ({s['detail']})" for s in f["skips"]]
    lines += ["", "## Changed values", ""] + [f"- `{x['where']}`: {x['fix']}" for x in f["fixes"]]
    lines += ["", "## Rules that need a human", ""] + ([f"- {k}" for k in f["flagged_rules"]] or ["- none"])
    lines += ["", "## Decisions explained", ""] + [f"- **{e['item']}**: {e['explanation']}" for e in r["explanations"]]
    lines += ["", "## Risks", ""] + [f"- {x}" for x in r["risks"]]
    lines += ["", "## Cutover checklist", ""] + [f"{i}. {x}" for i, x in enumerate(r["checklist"], 1)]
    lines += ["", f"Migration SQL: `{state['sql_path']}`", ""]
    return "\n".join(lines)
