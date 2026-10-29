"""Run the Firebase -> Supabase migration assistant.

    uv run python -m supabase_agents.firebase_migration.cli plan            # analyze, prove, report; stops at approval
    uv run python -m supabase_agents.firebase_migration.cli plan --approve sam   # ...then resume as @sam (applies)
    uv run python -m supabase_agents.firebase_migration.cli plan --reject
    options: --export PATH --rules PATH --tests PATH --variant baseline|model-rls|no-proof
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from langgraph.types import Command

from .graph import OUT_DIR, build_graph, new_state, render_report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan"])
    ap.add_argument("--export"); ap.add_argument("--rules"); ap.add_argument("--tests")
    ap.add_argument("--variant", default="baseline")
    g2 = ap.add_mutually_exclusive_group()
    g2.add_argument("--approve", metavar="APPROVER")
    g2.add_argument("--reject", action="store_true")
    a = ap.parse_args(argv)
    graph = build_graph()
    st = new_state(a.variant, a.export, a.rules, a.tests)
    cfg = {"configurable": {"thread_id": st["run_id"]}}
    out = graph.invoke(st, cfg)
    pending = out.get("__interrupt__")
    snap = graph.get_state(cfg).values
    rp = OUT_DIR / f"report-{st['run_id']}.md"
    rp.write_text(render_report(snap))
    print(snap["report"]["summary"]); print(f"\nreport: {rp}\nsql:    {snap['sql_path']}")
    if pending:
        print("\nWAITING FOR APPROVAL:", json.dumps(pending[0].value, default=str)[:300])
        if a.approve or a.reject:
            res = graph.invoke(Command(resume={"approved": bool(a.approve), "approver": a.approve or ""}), cfg)
            print("\nresult:", json.dumps(res.get("applied"), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
