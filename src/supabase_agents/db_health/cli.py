"""db-health CLI.

  uv run python -m supabase_agents.db_health.cli seed            # (re)create schema `health` + fictional data + query load
  uv run python -m supabase_agents.db_health.cli lint            # findings only (code, no model)
  uv run python -m supabase_agents.db_health.cli run             # explain, plan, prove, then ask @sam to approve
  uv run python -m supabase_agents.db_health.cli run --auto sam  # approve every proven migration as sam (demo)
"""

from __future__ import annotations

import argparse
import json
import uuid

from langgraph.types import Command

from ..core.cost import CostTracker
from ..core.db import connect
from . import SCHEMA, lints, load, perf, seed, shadow
from .graph import build_graph


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="db-health")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    sub.add_parser("lint")
    r = sub.add_parser("run")
    r.add_argument("--variant", default="baseline", choices=["baseline", "model-writes", "naive-fixes"])
    r.add_argument("--auto", help="approve every migration as this user without prompting")
    a = ap.parse_args(argv)

    if a.cmd == "seed":
        seed.reset()
        print("seeded schema health;", load.generate())
        return 0
    if a.cmd == "lint":
        with connect() as conn:
            for f in lints.run(conn, SCHEMA) + perf.slow_query_findings(conn, SCHEMA):
                print(f"{f['level']:5} {f['id']}\n      {f['detail']}")
        return 0

    cost = CostTracker()
    graph = build_graph(callbacks=[cost])
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}}
    res = graph.invoke({"variant": a.variant}, cfg)
    while "__interrupt__" in res:
        payload = res["__interrupt__"][0].value
        for m in payload["migrations"]:
            print(f"\n{m['id']}  {'PROVEN' if m['proven'] else 'NOT PROVEN'}  {m['title']}")
            for w in m["why"]:
                print(f"   why: {w}")
            print("   " + m["sql"].replace("\n", "\n   "))
        if a.auto:
            ans = {"approver": a.auto, "decisions": {m["id"]: "approve" for m in payload["migrations"]}}
        else:
            who = input("\nApprover (@sam): ").strip()
            ids = input("Approve which migrations (comma separated, 'all' or blank for none): ").strip()
            chosen = [m["id"] for m in payload["migrations"]] if ids == "all" else [x.strip() for x in ids.split(",") if x.strip()]
            ans = {"approver": who, "decisions": {mid: "approve" for mid in chosen}}
        res = graph.invoke(Command(resume=ans), cfg)
    print("\n" + res.get("outcome", ""))
    for x in res.get("applied", []):
        print(" ", x)
    print("remaining findings:", [f["id"] for f in res.get("after_findings", [])])
    print("spend:", json.dumps(cost.summary()))
    shadow.cleanup_shadows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
