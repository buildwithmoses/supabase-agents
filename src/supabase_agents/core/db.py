"""Postgres helpers for the local Supabase stack.

connect()                      a psycopg connection as the database owner (setup, migrations, privileged checks)
connect_user()                 a connection as Supabase's `authenticator` login role: no table privileges of its own,
                               NOINHERIT, and it can only SET ROLE to anon/authenticated. A query that runs
                               RESET ROLE or set_config('role', ...) lands back on authenticator, not a superuser.
as_user(conn, user_id, role)   run the rest of a transaction as a Supabase end user: SET LOCAL ROLE authenticated
                               and request.jwt.claims, so auth.uid() and every RLS policy apply exactly as they
                               would through the Supabase API. This is how agents read on a user's behalf.
read_only(conn)                SET TRANSACTION READ ONLY for the current transaction
vector_literal(v)              '[0.1,0.2,...]' for pgvector parameters
"""

from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg

from .config import db_url, user_db_url


def connect(**kw) -> psycopg.Connection:
    return psycopg.connect(db_url(), **kw)


def connect_user(**kw) -> psycopg.Connection:
    return psycopg.connect(user_db_url(), **kw)


def as_user(conn: psycopg.Connection, user_id: str, role: str = "authenticated", extra_claims: dict | None = None) -> None:
    claims = {"sub": user_id, "role": role, **(extra_claims or {})}
    conn.execute("select set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
    conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (user_id,))
    conn.execute(f"set local role {role}")


def read_only(conn: psycopg.Connection) -> None:
    conn.execute("set transaction read only")


@contextmanager
def user_tx(user_id: str, *, readonly: bool = True, statement_timeout_ms: int = 5000):
    """A transaction that runs as an end user under RLS, read-only by default, always rolled back.
    It logs in as `authenticator` (like PostgREST does), never as the superuser, so even SQL that tries to
    reset or switch roles can't get more than the end user's privileges."""
    with connect_user() as conn:
        with conn.transaction(force_rollback=True):
            if readonly:
                read_only(conn)
            conn.execute(f"set local statement_timeout = {int(statement_timeout_ms)}")
            as_user(conn, user_id)
            yield conn


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
