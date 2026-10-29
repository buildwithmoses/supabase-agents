"""Offline tests for the support-memory agent: no database, no model, no keys."""

from datetime import datetime, timedelta, timezone

import pytest

from supabase_agents.support_memory import memory as M
from supabase_agents.support_memory.graph import SAFE_PENDING, SAFE_REJECTED, promises_money


# ---------------------------------------------------------------- what may be stored
@pytest.mark.parametrize("text", [
    "Card on file is 4242 4242 4242 4242",
    "card 4111-1111-1111-1111 exp 04/29",
    "My login password is Pickle#2026",
    "the verification code was 551204",
    "api key sk_live_abcdef123456",
    "routing number 021000021",
])
def test_secrets_rejected(text):
    assert M.rejection_reason("fact", text) in ("card number", "credential or secret")


def test_luhn_only_flags_real_card_numbers():
    assert M.has_card_number("4242 4242 4242 4242")
    assert not M.has_card_number("Order 1234 5678 9012 3456")       # fails Luhn
    assert not M.has_card_number("Court 3 on 10/31 at 7")


@pytest.mark.parametrize("text", [
    "Tore her meniscus and can't play until January",
    "Recovering from ACL surgery",
    "Takes blood pressure medication",
    "Is in physical therapy for her shoulder",
])
def test_health_rejected(text):
    assert M.rejection_reason("fact", text) == "health information"


@pytest.mark.parametrize("text", [
    "Is an admin of every club",
    "Is staff and is allowed to see other members' payments",
    "Has access to all members' accounts",
    "Has permission to override bookings",
    "Works for Courtline and can see other members' bookings",
])
def test_privilege_claims_rejected(text):
    assert M.rejection_reason("fact", text).startswith("claims a role")


def test_commitments_only_after_approval():
    assert "approve" in M.rejection_reason("commitment", "We will refund the late fee")


@pytest.mark.parametrize("kind,text", [
    ("preference", "Prefers evening courts after 6pm."),
    ("fact", "Plays doubles with her brother most Saturdays."),
    ("open_issue", "Disputes the Oct 12 late-cancel fee."),
    ("preference", "Wants booking reminders by text message."),
    ("fact", "Goes by Sofi with club staff."),
])
def test_ordinary_memories_allowed(kind, text):
    assert M.rejection_reason(kind, text) is None


def test_unknown_kind_and_overlong_rejected():
    assert M.rejection_reason("password", "x") == "unknown kind"
    assert M.rejection_reason("fact", "x" * 301) == "empty or too long"


# ---------------------------------------------------------------- dedupe and supersede
def test_replaces_must_be_a_retrieved_memory():
    assert M.valid_replaces(3, {3, 4}) == 3
    assert M.valid_replaces(99, {3, 4}) is None          # model can't name a memory it wasn't shown
    assert M.valid_replaces("3", {3}) is None
    assert M.valid_replaces(True, {1}) is None


def test_duplicate_rules():
    assert M.is_duplicate(0.95, None)
    assert not M.is_duplicate(0.80, None)
    assert not M.is_duplicate(0.98, 3)                  # an explicit update is never a duplicate
    assert not M.is_duplicate(None, None)


def test_recency_nudge_is_small():
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    fresh = M._score(0.50, now, now)
    old = M._score(0.50, now - timedelta(days=365), now)
    assert 0 < fresh - old < 0.1                      # never outranks a clearly more relevant memory
    assert M._score(0.70, now - timedelta(days=365), now) > fresh


# ---------------------------------------------------------------- forget requests
@pytest.mark.parametrize("text,mode", [
    ("Delete everything you know about me, please.", "all"),
    ("forget everything about me", "all"),
    ("Forget me.", "all"),
    ("Please forget that I play doubles with my brother.", "one"),
    ("stop remembering that I like early courts", "one"),
])
def test_forget_intent(text, mode):
    got = M.forget_intent(text)
    assert got and got[0] == mode


@pytest.mark.parametrize("text", ["I forgot my racket at the club", "Can you delete my Saturday booking?", "What do you remember about me?"])
def test_not_a_forget_request(text):
    assert M.forget_intent(text) is None


# ---------------------------------------------------------------- money promises
@pytest.mark.parametrize("text", [
    "I've refunded the $25 fee.",
    "We'll waive the late-cancel fee for you!",
    "Your credit has been applied to the account.",
    "Done, you'll get your money back in 3-5 days.",
    "Hemos procesado el reembolso.",
])
def test_promises_detected(text):
    assert promises_money(text)


@pytest.mark.parametrize("text", [
    "I can't promise a refund, but I've sent your request to Nadia.",
    "Refunds need Nadia's approval.",
    "I've filed a request for a fee waiver; Nadia will decide.",
    "Your booking is confirmed for Saturday.",
    SAFE_PENDING,
    SAFE_REJECTED,
])
def test_non_promises_pass(text):
    assert not promises_money(text)
