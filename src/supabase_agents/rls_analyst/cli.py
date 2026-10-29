"""Ask the RLS analyst a question as one of the fictional users.

    uv run python -m supabase_agents.rls_analyst.cli --as alex "What was our booking revenue in October?"
    uv run python -m supabase_agents.rls_analyst.cli --as pat "How much have I paid this fall?" --show-sql
Users: pat (player, club 1), alex (staff, club 1), bianca (staff, club 2), jordan (player, clubs 1 and 2),
morgan (staff, club 3), casey (no club).
"""

from __future__ import annotations

import argparse
import json

from ..core.config import load_env
from ..core.cost import CostTracker
from .graph import VARIANTS, ask
from .users import ASKERS


def main() -> None:
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--as", dest="who", choices=sorted(ASKERS), default="alex")
    p.add_argument("--variant", choices=VARIANTS, default="baseline")
    p.add_argument("--show-sql", action="store_true")
    a = p.parse_args()
    cost = CostTracker()
    s = ask(a.question, ASKERS[a.who], variant=a.variant, callbacks=[cost])
    print(s["answer"])
    if a.show_sql:
        print("\n" + json.dumps({"outcome": s["outcome"], "sql": s.get("decision", {}).get("sql_to_run"),
                                 "ran_as": [t.get("identity") for t in s["trace"] if t["step"] == "execute"]}, indent=2))
    print(f"\n[{cost.summary()}]")


if __name__ == "__main__":
    main()
