"""Ask the Courtline help center.

    uv run python -m supabase_agents.docs_qa.cli ingest
    uv run python -m supabase_agents.docs_qa.cli ask "How far ahead can I book?" [--as player|staff] [--variant naive]
"""

from __future__ import annotations

import argparse
import json

from ..core.cost import CostTracker


def main() -> None:
    p = argparse.ArgumentParser(prog="docs_qa")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--as", dest="asker", choices=["player", "staff"], default="player")
    a.add_argument("--variant", choices=["baseline", "naive"], default="baseline")
    args = p.parse_args()
    if args.cmd == "ingest":
        from .ingest import ingest
        print(json.dumps(ingest(), indent=2))
        return
    from .graph import ask
    t = CostTracker()
    r = ask(args.question, args.asker, args.variant, tracker=t)
    r.pop("retrieved_hits", None)
    print(json.dumps({**r, "spend": t.summary()}, indent=2))


if __name__ == "__main__":
    main()
