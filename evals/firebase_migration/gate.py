"""Gate for the migration assistant. Exit 1 blocks the change.

    uv run python evals/firebase_migration/gate.py [results.json] [--variant v] [--save-baseline]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from supabase_agents.core.gate import check, load_baseline, report  # noqa: E402

HERE = Path(__file__).resolve().parent
FLOORS = {"rules_match": 1.0, "counts_reconciled": 1.0, "referential_integrity": 1.0, "unsupported_flagged": 1.0,
          "approval_respected": 1.0, "field_fidelity": 0.99, "schema_decisions": 1.0, "report_quality": 0.7}


def main() -> int:
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    variant = sys.argv[sys.argv.index("--variant") + 1] if "--variant" in sys.argv else "baseline"
    if variant in args:
        args.remove(variant)
    path = Path(args[0]) if args else HERE / "results" / f"latest-{variant}.json"
    r = json.loads(path.read_text())
    m = r["metrics"]
    base = load_baseline(HERE / "baseline.json")
    print(f"firebase-migration gate: {r['label']} ({r['variant']})")
    for k, f in FLOORS.items():
        v = m.get(k)
        ok = v is not None and v >= f
        print(f"  {'ok  ' if ok else 'FAIL'} {k:24} {('missing' if v is None else v)!s:8} (floor {f}{', baseline ' + str(base['metrics'].get(k)) if base else ''})")
    spend = r.get("spend", {})
    agent = sum((x or {}).get("cost_usd", 0) for x in spend.get("agent_runs", []))
    print(f"  spend: agent ${agent:.4f} (3 runs), judge ${spend.get('judge', {}).get('cost_usd', 0):.4f}")
    fails = check(m, FLOORS)
    code = report(fails, r["label"])
    if "--save-baseline" in sys.argv:
        if fails:
            print("not saving a failing baseline"); return 1
        (HERE / "baseline.json").write_text(json.dumps({"label": r["label"], "metrics": m}, indent=2) + "\n")
        print("saved baseline.json")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
