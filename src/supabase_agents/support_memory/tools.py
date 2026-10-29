"""Tools the support agent can call. Every one runs inside user_tx(customer_id): role `authenticated`, read-only,
RLS on. The tool code never filters by customer; Postgres does. Asking about another member returns nothing."""

from __future__ import annotations

from langchain_core.tools import tool

from ..core.db import user_tx


def _fmt_money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def make_tools(customer_id: str):
    @tool
    def get_my_bookings() -> list[dict]:
        """The customer's court bookings (past and upcoming): court, sport, start time (UTC) and status."""
        with user_tx(customer_id) as c:
            rows = c.execute("select court, sport, starts_at, status from support.bookings order by starts_at").fetchall()
        return [{"court": r[0], "sport": r[1], "starts_at_utc": r[2].isoformat(), "status": r[3]} for r in rows]

    @tool
    def get_my_payments() -> list[dict]:
        """The customer's payments: kind, amount, status and date."""
        with user_tx(customer_id) as c:
            rows = c.execute("select kind, amount_cents, status, created_at from support.payments order by created_at").fetchall()
        return [{"kind": r[0], "amount": _fmt_money(r[1]), "status": r[2], "date": r[3].date().isoformat()} for r in rows]

    @tool
    def get_club_leagues() -> list[dict]:
        """Leagues at the customer's own club: name, night, fee and spots left."""
        with user_tx(customer_id) as c:
            rows = c.execute("select name, night, fee_cents, spots_left from support.leagues order by night").fetchall()
        return [{"league": r[0], "night": r[1], "fee": _fmt_money(r[2]), "spots_left": r[3]} for r in rows]

    @tool
    def find_member(name: str) -> list[dict]:
        """Look up a club member by name. Members can only ever see their own record."""
        with user_tx(customer_id) as c:
            rows = c.execute("select name, role from support.customers where name ilike %s", (f"%{name}%",)).fetchall()
        return [{"name": r[0], "role": r[1]} for r in rows]

    @tool
    def request_money_action(kind: str, amount_dollars: float, reason: str) -> dict:
        """Ask the support lead (Nadia) to approve a refund, credit or fee waiver. kind: refund | credit | fee_waiver.
        This only files a request; nothing is paid or waived until Nadia approves."""
        return {"status": "queued_for_nadia", "kind": kind, "amount_dollars": amount_dollars, "reason": reason}

    return [get_my_bookings, get_my_payments, get_club_leagues, find_member, request_money_action]
