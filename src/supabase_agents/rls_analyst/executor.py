"""Run guarded SQL. Baseline mode runs ONLY as the asking user, under RLS.

run(sql_to_run, user_id, mode)  -> Result(columns, rows, identity, error, ms)
    mode "user"     (baseline)   core.db.user_tx: read-only, rolled back, SET LOCAL ROLE authenticated +
                                  request.jwt.claims; auth.uid() = the asker. Identity is read in the same
                                  transaction, so the eval can prove who the query ran as.
    mode "service"  (regression) the postgres superuser, read-only. RLS does not apply.
    Statements are sent with prepare=True (extended protocol), so Postgres itself rejects stacked statements
    such as "select 1; reset role; select ...". This matters: core.db connects as the postgres superuser, so
    inside the user's transaction a stacked RESET ROLE or set_config('role', ...) can switch back to postgres.
    The guard blocks those; single-statement execution is the second lock. In production, connect as a
    non-superuser login (Supabase's `authenticator`) so there is nothing to switch back to.
visibility(sql_to_run, result, user_id) -> "full" | "partial" | "none"
    Runs the same query (without its outer LIMIT) as the user and as postgres, and compares md5 hashes of the
    two full results computed inside Postgres. Only the hashes come back, never the rows: the agent learns
    THAT rows are hidden, not what they are.
asker_profile(user_id)  -> display name and memberships (the asker's own data, for the prompt).
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import time
from dataclasses import dataclass, field

from ..core.db import connect, user_tx

TIMEOUT_MS = 5000


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    identity: dict = field(default_factory=dict)
    error: str | None = None
    ms: int = 0

    def to_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "identity": self.identity, "error": self.error, "ms": self.ms}


def _clean(v):
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    if hasattr(v, "hex") and type(v).__name__ == "UUID":
        return str(v)
    return v


def _digest(rows: list[list]) -> str:
    return hashlib.md5(json.dumps(sorted(json.dumps(r, default=str) for r in rows)).encode()).hexdigest()


def run(sql_to_run: str, user_id: str, mode: str = "user") -> Result:
    t0 = time.time()
    try:
        if mode == "user":
            with user_tx(user_id, statement_timeout_ms=TIMEOUT_MS) as conn:
                conn.execute("set local search_path = analyst")
                who = conn.execute("select current_user::text, auth.uid()::text").fetchone()
                cur = conn.execute(sql_to_run, prepare=True)  # extended protocol: Postgres refuses a second statement
                cols = [d.name for d in cur.description]
                rows = [[_clean(v) for v in r] for r in cur.fetchall()]
        elif mode == "service":
            with connect() as conn, conn.transaction(force_rollback=True):
                conn.execute("set transaction read only")
                conn.execute(f"set local statement_timeout = {TIMEOUT_MS}")
                conn.execute("set local search_path = analyst")
                who = conn.execute("select current_user::text, null::text").fetchone()
                cur = conn.execute(sql_to_run, prepare=True)  # extended protocol: Postgres refuses a second statement
                cols = [d.name for d in cur.description]
                rows = [[_clean(v) for v in r] for r in cur.fetchall()]
        else:
            raise ValueError(mode)
        return Result(cols, rows, {"current_user": who[0], "auth_uid": who[1]}, None, int((time.time() - t0) * 1000))
    except Exception as e:  # noqa: BLE001 - surfaced to the model as an error, never raised to the caller
        return Result(error=str(e).splitlines()[0][:300], ms=int((time.time() - t0) * 1000))


def is_empty(rows: list[list]) -> bool:
    if not rows:
        return True
    return len(rows) == 1 and all(v in (None, 0, 0.0, "") for v in rows[0])


def _unlimited(sql_to_run: str) -> str:
    """The same query without its outer LIMIT, so ties under ORDER BY ... LIMIT can't make two runs differ."""
    import sqlglot
    from sqlglot import exp

    root = sqlglot.parse_one(sql_to_run, read="postgres")
    if isinstance(root, exp.Select):
        if isinstance(root.args.get("from"), exp.From) and isinstance(root.args["from"].this, exp.Subquery) \
                and str(root.args["from"].this.alias) == "q" and root.args.get("limit"):
            root = root.args["from"].this.this  # our own union wrapper
        root = root.copy()
        root.set("limit", None)
    return root.sql(dialect="postgres")


DIGEST_SQL = "select md5(coalesce(string_agg(t::text, '|' order by t::text), '')) from ({q}) as t"


def _db_digest(conn, sql: str) -> str:
    return conn.execute(DIGEST_SQL.format(q=sql)).fetchone()[0]


def visibility(sql_to_run: str, result: Result, user_id: str) -> str:
    """Compare the asker's full result with the privileged full result, as md5 hashes computed inside Postgres.
    Only the hashes come back. "full" = the asker sees everything this query matches."""
    try:
        q = _unlimited(sql_to_run)
        with user_tx(user_id, statement_timeout_ms=TIMEOUT_MS) as conn:
            conn.execute("set local search_path = analyst")
            mine = _db_digest(conn, q)
        with connect() as conn, conn.transaction(force_rollback=True):
            conn.execute("set transaction read only")
            conn.execute(f"set local statement_timeout = {TIMEOUT_MS}")
            conn.execute("set local search_path = analyst")
            priv = _db_digest(conn, q)
    except Exception:  # noqa: BLE001 - if the probe can't run, don't claim anything is hidden
        return "full"
    if mine == priv:
        return "full"
    return "none" if is_empty(result.rows) else "partial"


def asker_profile(user_id: str) -> dict:
    with connect() as conn, conn.transaction(force_rollback=True):
        conn.execute("set transaction read only")
        name = conn.execute("select display_name from analyst.profiles where user_id = %s", (user_id,)).fetchone()
        ms = conn.execute("select m.club_id, c.name, m.role from analyst.memberships m join analyst.clubs c on c.id = m.club_id "
                          "where m.user_id = %s order by m.club_id", (user_id,)).fetchall()
    return {"user_id": user_id, "name": name[0] if name else "unknown user",
            "memberships": [{"club_id": c, "club": n, "role": r} for c, n, r in ms]}
