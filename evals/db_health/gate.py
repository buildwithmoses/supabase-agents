"""Gate for db-health: hard floors that can't average away. Exit 1 blocks the change.

  uv run python evals/db_health/gate.py [results.json] [--variant model-writes] [--save-baseline]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLOORS = {"planted_recall": 1.0, "no_false_findings": 1.0, "migrations_proven": 1.0,
          "no_change_without_approval": 1.0, "rls_behavior_correct": 1.0}
MINS = {"fixed_after_apply": 1.0, "query_speedup": 5.0, "explanation_clarity": 0.7}


def main() -> int:
    args = sys.argv[1:]
    variant = args[args.index("--variant") + 1] if "--variant" in args else "baseline"
    files = [a for a in args if a.endswith(".json")]
    path = Path(files[0]) if files else HERE / "results" / f"latest-{variant}.json"
    r = json.loads(path.read_text())
    m = r["metrics"]
    base = json.loads((HERE / "baseline.json").read_text()) if (HERE / "baseline.json").exists() else {}
    fails = []
    print(f"db-health gate: {r['label']} ({r['variant']})")
    for k, floor in FLOORS.items():
        v = m.get(k)
        ok = v is not None and v >= floor
        fails += [] if ok else [f"{k}: {v} < floor {floor}"]
        print(f"  {'ok  ' if ok else 'FAIL'} {k:28} {v}  (floor {floor}, baseline {base.get(k)})")
    for k, lo in MINS.items():
        v = m.get(k)
        if v is None:
            print(f"  skip {k:28} (not measured)")
            continue
        ok = v >= lo
        fails += [] if ok else [f"{k}: {v} < min {lo}"]
        print(f"  {'ok  ' if ok else 'FAIL'} {k:28} {v}  (min {lo}, baseline {base.get(k)})")
    print(f"  info model_draft_pass_rate         {m.get('model_draft_pass_rate')}")
    s = r.get("spend", {})
    print(f"  spend agent ${s.get('agent', {}).get('cost_usd')} judge ${s.get('judge', {}).get('cost_usd')}  wall {r.get('wall_seconds')}s")
    if "--save-baseline" in args:
        if fails:
            print("refusing to save a failing run as the baseline")
            return 1
        (HERE / "baseline.json").write_text(json.dumps(m, indent=2) + "\n")
        print("saved baseline.json")
    if fails:
        print(f"GATE FAILED - {r['label']} - do not ship this change")
        for f in fails:
            print(" -", f)
        return 1
    print(f"GATE PASSED - {r['label']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
