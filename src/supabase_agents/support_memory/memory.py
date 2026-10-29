"""Per-customer memory: what may be stored (code), how it is retrieved (Postgres RLS), how it is forgotten.

Retrieval and writes run inside core.db.user_tx(customer_id), so Postgres row-level security decides which
rows exist. The regression variant `service-role` reads with the owner connection and an app-side filter,
the way many apps do it, with two realistic bugs: it scopes by club instead of customer, and it doesn't
check deleted_at. Its forget only soft-deletes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..core.config import embed
from ..core.db import connect, user_tx, vector_literal

KINDS = ("preference", "fact", "open_issue", "commitment")
DEDUPE_SIM = 0.92          # same thing said again: don't store twice
TOP_K = 6

# ------------------------------------------------------------------ what must never be stored
_CARD = re.compile(r"(?:\d[ -]?){13,19}")
_SECRET = re.compile(
    r"\b(password|passcode|passwd|pin\b|cvv|cvc|security code|one[- ]time code|otp|verification code|api[ _-]?key|"
    r"secret|token|ssn|social security|routing number|account number|bank account|login|username)\b"
    r"|\bexp(?:iry|iration)?\s*\d{1,2}\s*/\s*\d{2,4}\b|\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{6,}", re.I)
_HEALTH = re.compile(
    r"\b(injur\w*|surgery|surgeon|torn|tore|sprain\w*|fractur\w*|broke (my|his|her)|acl|mcl|meniscus|tendon\w*|tendinitis|"
    r"concussion|diagnos\w*|medication|medicine|prescri\w*|pregnan\w*|cancer|diabet\w*|asthma|heart|blood pressure|"
    r"physical therapy|physio|rehab\w*|doctor|hospital|anxiety|depress\w*|therapy)\b", re.I)
_PRIVILEGE = re.compile(
    r"\b(admin\w*|administrator|superuser|owner of|(is|am|as|i'?m|are) (a |an )?(staff|employee)|"
    r"staff (member|access|account|role|permission\w*)|employee of|works (for|at) courtline|permission\w*|privileg\w*|"
    r"access to|allowed to (see|view|access|edit|read|change)|can (see|view|access) (other|all|every)|other members|"
    r"all (the )?(clubs|members|accounts|customers)|every (club|member|account|customer)|role|moderator|"
    r"manager of|override|bypass|verified (by|as) (courtline|staff)|vip status|free (membership|courts) forever)\b", re.I)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            n -= 9 if n > 9 else 0
        total += n
        alt = not alt
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False


def rejection_reason(kind: str, content: str) -> str | None:
    """Why a proposed memory may not be stored, or None if it may."""
    if kind not in KINDS:
        return "unknown kind"
    if kind == "commitment":
        return "commitments are only recorded after a person approves them"
    if not content or len(content) > 300:
        return "empty or too long"
    if has_card_number(content):
        return "card number"
    if _SECRET.search(content):
        return "credential or secret"
    if _HEALTH.search(content):
        return "health information"
    if _PRIVILEGE.search(content):
        return "claims a role or privilege (roles come from the customers table, never from chat)"
    return None


def contains_secret(text: str) -> bool:
    return has_card_number(text) or bool(_SECRET.search(text))


# ------------------------------------------------------------------ retrieval
@dataclass
class Memory:
    id: int
    customer_id: str
    kind: str
    content: str
    created_at: datetime
    sim: float = 0.0
    score: float = 0.0


def _score(sim: float, created_at: datetime, now: datetime) -> float:
    age = max((now - created_at).total_seconds() / 86400, 0)
    return sim + 0.08 * math.exp(-age / 60)       # similarity first, a small nudge for recent memories


def retrieve(customer_id: str, query: str, *, variant: str = "baseline", club_id: str | None = None,
             k: int = TOP_K, now: datetime | None = None) -> list[Memory]:
    now = now or datetime.now(timezone.utc)
    q = vector_literal(embed([query])[0])
    sql_cols = ("select id, customer_id::text, kind, content, created_at, "
                "1 - (embedding operator(extensions.<=>) %s::extensions.vector) as sim from support.memories")
    if variant == "service-role":
        # The bug-prone app-side filter: owner connection (RLS bypassed), scoped by club, no deleted_at check.
        with connect() as c:
            rows = c.execute(sql_cols + " where club_id = %s", (q, club_id)).fetchall()
    else:
        with user_tx(customer_id) as c:      # RLS: only this customer's live memories exist here
            rows = c.execute(sql_cols, (q,)).fetchall()
    mems = [Memory(r[0], r[1], r[2], r[3], r[4], float(r[5])) for r in rows]
    for m in mems:
        m.score = _score(m.sim, m.created_at, now)
    mems.sort(key=lambda m: m.score, reverse=True)
    return mems[:k]


# ------------------------------------------------------------------ storing
def valid_replaces(replaces, known_ids: set[int]) -> int | None:
    """A proposal may only replace a memory this session actually retrieved (so the model can't name an arbitrary id)."""
    return replaces if isinstance(replaces, int) and not isinstance(replaces, bool) and replaces in known_ids else None


def is_duplicate(best_sim: float | None, replaces: int | None) -> bool:
    """Skip a proposal that repeats an existing memory, unless it explicitly updates one: embeddings barely see
    negation ("email, not text" vs "text now, not email" score 0.98), so an update is never a duplicate."""
    return replaces is None and best_sim is not None and best_sim >= DEDUPE_SIM

@dataclass
class StoreResult:
    stored: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    superseded: list[int] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)


def store(customer_id: str, club_id: str, proposals: list[dict], *, source_message_id: int | None,
          known_ids: set[int]) -> StoreResult:
    """Code decides: filter, dedupe, supersede (only memories this session retrieved), insert under RLS."""
    res = StoreResult()
    clean = []
    for p in proposals:
        kind, content = str(p.get("kind", "")).strip(), " ".join(str(p.get("content", "")).split())
        why = rejection_reason(kind, content)
        if why:
            res.rejected.append({"kind": kind, "content": content, "reason": why})
        else:
            clean.append({"kind": kind, "content": content, "replaces": p.get("replaces")})
    if not clean:
        return res
    vecs = embed([p["content"] for p in clean])
    with write_tx(customer_id) as c:
        for p, v in zip(clean, vecs):
            lit = vector_literal(v)
            # A proposal that says which memory it replaces is an update, not a repeat: embeddings barely see
            # negation ("email, not text" vs "text now, not email" score 0.98), so never dedupe against that one.
            replaces = valid_replaces(p.get("replaces"), known_ids)
            dup = c.execute("select id, content, 1 - (embedding operator(extensions.<=>) %s::extensions.vector) as sim "
                            "from support.memories where id is distinct from %s order by sim desc limit 1", (lit, replaces)).fetchone()
            if dup and is_duplicate(float(dup[2]), replaces):
                res.duplicates.append({"content": p["content"], "existing_id": dup[0], "sim": round(float(dup[2]), 3)})
                continue
            new_id = c.execute("insert into support.memories (customer_id, club_id, kind, content, embedding, source_message_id) "
                               "values (%s, %s, %s, %s, %s::extensions.vector, %s) returning id",
                               (customer_id, club_id, p["kind"], p["content"], lit, source_message_id)).fetchone()[0]
            res.stored.append({"id": new_id, "kind": p["kind"], "content": p["content"]})
            old = replaces
            if old is not None:
                n = c.execute("select support.supersede_memory(%s, %s)", (old, new_id)).fetchone()[0]
                if n:
                    res.superseded.append(old)
        if res.superseded:
            c.execute("select support.forget_purge()")
    return res


def write_tx(customer_id: str):
    """A committed read-write transaction as the customer (RLS applies)."""
    from contextlib import contextmanager
    from ..core.db import as_user

    @contextmanager
    def _cm():
        with connect() as conn:
            with conn.transaction():
                conn.execute("set local statement_timeout = 5000")
                as_user(conn, customer_id)
                yield conn
    return _cm()


# ------------------------------------------------------------------ forgetting
FORGET_ALL = re.compile(r"\b(forget (everything|all)( (about|you know about) me)?|delete (everything|all|what) you (know|have|remember|saved)( about me)?|"
                        r"forget (about )?me\b|erase (my|all my) (data|memories|info)|olvida todo|borra todo)", re.I)
FORGET_ONE = re.compile(r"\b(?:please\s+)?(?:forget|stop remembering|delete|remove|don'?t remember)\s+(?:that\s+|the fact that\s+|about\s+)?(?P<what>.+)", re.I)


def forget_intent(text: str) -> tuple[str, str] | None:
    """('all', '') or ('one', what) or None."""
    if FORGET_ALL.search(text):
        return ("all", "")
    m = FORGET_ONE.search(text)
    if m and re.search(r"\b(forget|stop remembering|don'?t remember)\b", text, re.I):
        what = m.group("what").strip(" .!?")
        return ("one", what) if what else None
    return None


def forget(customer_id: str, mode: str, what: str = "", *, variant: str = "baseline", min_sim: float = 0.55) -> list[dict]:
    """Soft-delete (hidden from retrieval at once by the read policy), then purge for good. Returns what was forgotten."""
    with write_tx(customer_id) as c:
        if mode == "all":
            rows = c.execute("select id, content from support.forget_memories(null)").fetchall()
        else:
            v = vector_literal(embed([what])[0])
            cand = c.execute("select id, content, 1 - (embedding operator(extensions.<=>) %s::extensions.vector) as sim "
                             "from support.memories order by sim desc", (v,)).fetchall()
            top = [r for r in cand if r[2] >= min_sim]
            if not top and cand and cand[0][2] >= min_sim - 0.1:
                top = cand[:1]
            ids = [r[0] for r in top[:3]]
            rows = c.execute("select id, content from support.forget_memories(%s)", (ids,)).fetchall() if ids else []
        if variant != "service-role":
            c.execute("select support.forget_purge()")
    return [{"id": r[0], "content": r[1]} for r in rows]
