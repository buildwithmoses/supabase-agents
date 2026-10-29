"""One gate shape for every agent: hard floors that can't average away, plus
"no metric drops more than X below baseline". Exit code 1 blocks the change."""

from __future__ import annotations

import json
from pathlib import Path


def check(metrics: dict[str, float], floors: dict[str, float] | None = None,
          baseline: dict[str, float] | None = None, max_drop: float = 0.0) -> list[str]:
    fails = []
    for name, floor in (floors or {}).items():
        value = metrics.get(name)
        if value is None:
            fails.append(f"{name}: missing (floor {floor})")
        elif value < floor:
            fails.append(f"{name}: {value:.3f} < floor {floor:.3f}")
    for name, base in (baseline or {}).items():
        value = metrics.get(name)
        if value is not None and value < base - max_drop:
            fails.append(f"{name}: {value:.3f} dropped below baseline {base:.3f}")
    return fails


def report(fails: list[str], label: str = "") -> int:
    if fails:
        print(f"GATE FAILED{' - ' + label if label else ''} - do not ship this change")
        for f in fails:
            print(" -", f)
        return 1
    print(f"GATE PASSED{' - ' + label if label else ''}")
    return 0


def load_baseline(path: str | Path) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None
