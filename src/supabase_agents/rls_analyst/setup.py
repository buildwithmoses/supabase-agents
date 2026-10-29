"""Apply this agent's migrations to the local Supabase Postgres (only the `analyst` schema).

    uv run python -m supabase_agents.rls_analyst.setup

`supabase db reset` would also apply them, but it resets every schema, and the sibling agents share the
database, so this applies just the analyst files in order.
"""

from __future__ import annotations

from ..core.config import PROJECT_ROOT
from ..core.db import connect

FILES = sorted((PROJECT_ROOT / "supabase" / "migrations").glob("*_analyst_*.sql"))


def apply() -> None:
    with connect() as conn:
        for f in FILES:
            conn.execute(f.read_text())
            print(f"applied {f.name}")
        conn.commit()
        n = conn.execute("select (select count(*) from analyst.bookings), (select count(*) from analyst.payments), "
                         "(select count(*) from analyst.profiles)").fetchone()
        print(f"analyst: {n[0]} bookings, {n[1]} payments, {n[2]} people")


if __name__ == "__main__":
    apply()
