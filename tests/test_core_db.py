"""The end-user connection can't escalate, even if a query resets or switches roles. Needs `supabase start`."""
import psycopg
import pytest

from supabase_agents.core.db import user_tx


def _db_up() -> bool:
    try:
        with user_tx("00000000-0000-0000-0000-000000000000") as c:
            c.execute("select 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_up(), reason="local Supabase not running")


def test_user_tx_runs_as_authenticated_and_cannot_escalate():
    with user_tx("00000000-0000-0000-0000-000000000001") as c:
        assert c.execute("select current_user").fetchone()[0] == "authenticated"
        assert c.execute("select auth.uid()::text").fetchone()[0] == "00000000-0000-0000-0000-000000000001"
        c.execute("reset role")
        assert c.execute("select current_user").fetchone()[0] == "authenticator"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("set role postgres")
