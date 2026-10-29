"""The fictional people the evals ask questions as. UUIDs are deterministic (uuid5)."""

from __future__ import annotations

import uuid

NS = uuid.UUID("6f1d2c3a-0000-4000-8000-c0a71e000000")


def uid(handle: str) -> str:
    return str(uuid.uuid5(NS, handle))


CLUBS = {
    1: ("Riverside Racquet Club", "Atlanta", "tennis"),
    2: ("Peachtree Pickleball", "Decatur", "pickleball"),
    3: ("Midtown Tennis Center", "Atlanta", "tennis"),
    4: ("Lakeside Paddle", "Marietta", "pickleball"),
}

# Named people the eval asks as: handle -> (display name, [(club_id, role)])
NAMED = {
    "pat":    ("Pat Rivera",    [(1, "player")]),
    "alex":   ("Alex Chen",     [(1, "staff")]),
    "bianca": ("Bianca Okafor", [(2, "staff")]),
    "jordan": ("Jordan Lee",    [(1, "player"), (2, "player")]),
    "casey":  ("Casey Doe",     []),
    "morgan": ("Morgan Ellis",  [(3, "staff")]),
}

ASKERS = {h: uid(h) for h in NAMED}
