"""LangGraph flow: gather -> explain -> plan -> prove -> approve (interrupt) -> apply.

variant 'baseline'     code writes every migration and proves it in a shadow schema; only proven migrations
                       can be approved; an approved subset is re-proven on its own before it touches `health`.
variant 'model-writes' regression: the model's draft SQL is the migration, with no shadow proof.
variant 'naive-fixes'  regression: plausible but wrong fixes (fixes.naive_plan), proven like the baseline.
Only @sam can approve. Anyone else's answer is recorded and ignored, and the graph asks again (up to 3 times).
"""

from __future__ import annotations

import operator
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..core.config import PROJECT_ROOT
from ..core.db import connect
from . import APPROVER, SCHEMA, lints, perf, shadow
from .explain import explain_all, split_sql
from .fixes import naive_plan
from .fixes import plan as plan_fixes
from .fixes import to_sql

MAX_ASKS = 3


class State(TypedDict, total=False):
    variant: str
    findings: list[dict]
    explanations: dict[str, dict]
    migrations: list[dict]
    proof: dict
    approval_attempts: Annotated[list[dict], operator.add]
    approved: list[str] | None
    rejected: list[str]
    refused_unproven: list[str]
    applied: list[dict]
    reproof: dict
    after_findings: list[dict]
    outcome: str


def build_graph(callbacks=None, migrations_dir: Path | None = None, checkpointer=None):
    mig_dir = Path(migrations_dir or PROJECT_ROOT / "supabase" / "migrations")

    def gather(state: State) -> dict:
        with connect() as conn:
            fs = lints.run(conn, SCHEMA) + perf.slow_query_findings(conn, SCHEMA)
        return {"findings": fs}

    def explain(state: State) -> dict:
        with connect() as conn:
            return {"explanations": explain_all(conn, state["findings"], SCHEMA, callbacks=callbacks)}

    def plan(state: State) -> dict:
        if state.get("variant") == "model-writes":
            migs = []
            for i, f in enumerate(state["findings"], 1):
                ex = state["explanations"][f["id"]]
                migs.append({"id": f"M{i}", "title": ex.get("fix_summary") or f["id"], "finding_ids": [f["id"]],
                             "statements": split_sql(ex.get("draft_sql", "")), "rls_tables": [], "perf": None, "source": "model"})
            return {"migrations": migs}
        migs = (naive_plan if state.get("variant") == "naive-fixes" else plan_fixes)(state["findings"], SCHEMA)
        for m in migs:
            m["source"] = "code"
        return {"migrations": migs}

    def prove(state: State) -> dict:
        if state.get("variant") == "model-writes":
            return {"proof": {"skipped": True, "steps": {}, "ok": False}}
        return {"proof": shadow.prove(state["migrations"], SCHEMA)}

    def approve(state: State) -> dict:
        steps = state["proof"].get("steps", {})
        ex = state["explanations"]
        payload = {
            "approver_required": APPROVER,
            "migrations": [{
                "id": m["id"], "title": m["title"], "sql": to_sql(m), "source": m.get("source"),
                "proven": bool(steps.get(m["id"], {}).get("ok")),
                "proof": {k: steps.get(m["id"], {}).get(k) for k in ("applied", "cleared", "new_findings", "rls_ok", "perf")},
                "why": [ex.get(fid, {}).get("explanation", "") for fid in m["finding_ids"]],
            } for m in state["migrations"]],
        }
        ans: Any = interrupt(payload) or {}
        who = str(ans.get("approver", "")).strip().lower().lstrip("@")
        decisions = ans.get("decisions") or {}
        attempt = {"approver": who, "decisions": decisions, "at": datetime.now(timezone.utc).isoformat()}
        if who != APPROVER:
            return {"approval_attempts": [{**attempt, "ignored": f"only @{APPROVER} can approve database changes"}], "approved": None}
        ids = {m["id"] for m in state["migrations"]}
        approved = [mid for mid, d in decisions.items() if d == "approve" and mid in ids]
        refused = []
        if state.get("variant") != "model-writes":
            refused = [mid for mid in approved if not steps.get(mid, {}).get("ok")]
            approved = [mid for mid in approved if mid not in refused]
        rejected = sorted(ids - set(approved))
        return {"approval_attempts": [attempt], "approved": approved, "rejected": rejected, "refused_unproven": refused}

    def route_after_approve(state: State) -> str:
        if state.get("approved") is None:
            return "approve" if len(state.get("approval_attempts", [])) < MAX_ASKS else "no_decision"
        return "apply" if state["approved"] else "no_decision"

    def no_decision(state: State) -> dict:
        return {"outcome": "nothing applied (no valid approval)" if state.get("approved") is None else "nothing applied (all rejected)", "applied": []}

    def apply(state: State) -> dict:
        chosen = [m for m in state["migrations"] if m["id"] in state["approved"]]
        out: dict = {"applied": []}
        if state.get("variant") != "model-writes":
            if len(chosen) != len(state["migrations"]):      # prove exactly what will be applied
                out["reproof"] = shadow.prove(chosen, SCHEMA)
                if not out["reproof"]["ok"]:
                    return {**out, "outcome": "approved subset failed its own proof; nothing applied"}
        mig_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        with connect(autocommit=True) as conn:
            for i, m in enumerate(chosen):
                err = shadow.apply_statements(conn, m["statements"], SCHEMA, SCHEMA)
                rec = {"id": m["id"], "title": m["title"], "error": err}
                if err is None:
                    slug = re.sub(r"[^a-z0-9]+", "_", m["title"].lower()).strip("_")[:40]
                    path = mig_dir / f"{stamp}{i:02d}_health_{m['id'].lower()}_{slug}.sql"
                    path.write_text(to_sql(m, header=f"approved by @{APPROVER}, applied {stamp} UTC"))
                    rec["file"] = str(path.relative_to(PROJECT_ROOT) if path.is_relative_to(PROJECT_ROOT) else path)
                out["applied"].append(rec)
            after = lints.run(conn, SCHEMA) + perf.slow_query_findings(conn, SCHEMA)
        errors = [a for a in out["applied"] if a["error"]]
        out["after_findings"] = after
        out["outcome"] = f"applied {len(out['applied']) - len(errors)} of {len(chosen)} approved migrations" + (f", {len(errors)} failed" if errors else "")
        return out

    g = StateGraph(State)
    for name, fn in [("gather", gather), ("explain", explain), ("plan", plan), ("prove", prove), ("approve", approve),
                     ("apply", apply), ("no_decision", no_decision)]:
        g.add_node(name, fn)
    g.add_edge(START, "gather")
    g.add_edge("gather", "explain")
    g.add_edge("explain", "plan")
    g.add_edge("plan", "prove")
    g.add_edge("prove", "approve")
    g.add_conditional_edges("approve", route_after_approve, ["approve", "apply", "no_decision"])
    g.add_edge("apply", END)
    g.add_edge("no_decision", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
